from typing import Dict, Optional, Tuple

import pytorch_lightning as pl
import torch
import torch.nn.functional as F
import wandb
from einops import rearrange
from scipy.spatial.transform import Rotation
from torch import Tensor

from src.core.config import ExperimentConfig
from src.core.visualize import check_collision, scene_to_wandb_3d
from src.data.util import GraspData, denormalize_translation
from src.models.flow import sample, sample_location_and_conditional_flow
from src.models.util import get_grasp_from_batch
from src.models.velocity_mlp import VelocityNetwork


class Lightning(pl.LightningModule):
    """Flow Matching model combining SO3 and R3 manifold learning with synchronized time sampling."""

    def __init__(self, config: ExperimentConfig):
        super().__init__()
        self.config = config
        self.model = VelocityNetwork(self.config)

        # TODO use config
        self.save_hyperparameters()

    def compute_loss(
        self,
        so3_inputs: Tensor,
        r3_inputs: Tensor,
        sdf_inputs: Tensor,
        sdf_path: Tuple[str],
        # dataset_mesh_scale: float,
        normalization_scale: float,
        prefix: str = "train",
    ) -> Tuple[Tensor, Dict[str, Tensor]]:
        """Compute combined loss for both manifolds with synchronized time sampling.

        Args:
            so3_inputs: Target SO3 matrices [batch, 3, 3]
            r3_inputs: Target R3 points [batch, 3]
            prefix: Prefix for logging metrics

        Returns:
            Tuple of (total_loss, loss_dict)
        """
        # Sample synchronized time points for both manifolds

        so3_inputs = self.model.duplicate_to_batch_size(
            so3_inputs,
            self.config.data.batch_size,
            self.config.training.duplicate_ratio,
        )
        r3_inputs = self.model.duplicate_to_batch_size(
            r3_inputs, self.config.data.batch_size, self.config.training.duplicate_ratio
        )
        t = torch.rand(r3_inputs.size(0), device=so3_inputs.device)

        # SO3 computation - already in [batch, 3, 3] format
        x0_so3 = torch.tensor(
            Rotation.random(r3_inputs.size(0)).as_matrix(), device=so3_inputs.device
        )  # Shape: [batch, 3, 3]

        # Sample location and flow for SO
        xt_so3, ut_so3 = sample_location_and_conditional_flow(x0_so3, so3_inputs, t)
        # Both xt_so3 and ut_so3 are [batch, 3, 3]

        t_expanded = t.unsqueeze(-1)  # [batch, 1]
        noise = torch.randn_like(r3_inputs)

        # Get predicted flow for R3
        x_t_r3 = (
            1 - (1 - self.config.model.sigma_min) * t_expanded
        ) * noise + t_expanded * r3_inputs

        # Forward pass now expects [batch, 3, 3] format
        vt_so3, predicted_flow = self.model.forward(
            xt_so3, x_t_r3, sdf_inputs, t_expanded, normalization_scale, sdf_path
        )
        # vt_so3 is now directly [batch, 3, 3]

        # Compute SO3 loss using Riemannian metric
        r = torch.transpose(xt_so3, dim0=-2, dim1=-1) @ (vt_so3 - ut_so3)
        norm = -torch.diagonal(r @ r, dim1=-2, dim2=-1).sum(dim=-1) / 2
        so3_loss = torch.mean(norm, dim=-1)

        # Compute noisy sample and optimal flow for R3
        optimal_flow = r3_inputs - (1 - self.config.model.sigma_min) * noise
        r3_loss = F.mse_loss(predicted_flow, optimal_flow)

        # Works better in this setup but we can change later
        total_loss = (
            self.config.training.so3_loss_weight * so3_loss
            + self.config.training.r3_loss_weight * r3_loss
        )

        loss_dict = {
            f"{prefix}/so3_loss": so3_loss,
            f"{prefix}/r3_loss": r3_loss,
            f"{prefix}/loss": total_loss,
        }

        return total_loss, loss_dict

    def training_step(self, batch: Tuple, batch_idx: int) -> Tensor:
        grasp_data = batch

        loss, log_dict = self.compute_loss(
            grasp_data.rotation,
            grasp_data.translation,
            grasp_data.sdf,
            grasp_data.mesh_path,
            grasp_data.normalization_scale,
            "train",
        )

        self.log_dict(
            log_dict,
            prog_bar=True,
            batch_size=self.config.data.batch_size,
        )
        if (batch_idx % self.config.training.sample_interval == 0) and (
            batch_idx // self.config.training.sample_interval >= 1
        ):
            random_idx = torch.randint(
                0, len(self.trainer.train_dataloader.dataset), (1,)
            )
            grasp_data = self.trainer.train_dataloader.dataset[random_idx]

            sdf_input = rearrange(grasp_data.sdf, "... -> 1 1 ...")

            so3_output, r3_output = sample(
                self.model,
                sdf_input,
                grasp_data.translation.device,
                torch.tensor(grasp_data.normalization_scale),
                self.config.training.num_samples_to_log,
                sdf_path=grasp_data.mesh_path,
            )
            scene = self.compute_grasp_scene(grasp_data, (r3_output, so3_output))

            self.logger.experiment.log(
                {
                    f"train/generated_grasp": scene_to_wandb_3d(scene),
                }
            )

        return loss

    def validation_step(self, batch: Tuple, batch_idx: int) -> Dict[str, Tensor]:
        grasp_data = batch
        with torch.enable_grad():
            loss, log_dict = self.compute_loss(
                grasp_data.rotation,
                grasp_data.translation,
                grasp_data.sdf,
                grasp_data.mesh_path,
                grasp_data.normalization_scale,
                "val",
            )

        # Log validation metrics
        self.log_dict(
            log_dict,
            prog_bar=True,
            batch_size=self.config.data.batch_size,
        )

        return log_dict

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(
            self.parameters(),
            lr=self.config.training.learning_rate,
            betas=tuple(self.config.training.adamw_betas),
            eps=self.config.training.epsilon,
            weight_decay=self.config.training.weight_decay,
        )

        total_steps = self.trainer.estimated_stepping_batches

        # Single linear scheduler with warmup
        scheduler = torch.optim.lr_scheduler.OneCycleLR(
            optimizer,
            max_lr=self.config.training.learning_rate,
            total_steps=total_steps,
            pct_start=self.config.training.warmup_ratio,
            anneal_strategy="linear",
            div_factor=3.0,  # initial_lr = max_lr/div_factor
            final_div_factor=float("inf"),  # final_lr = initial_lr/final_div_factor
        )

        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": scheduler,
                "interval": "step",
                "frequency": 1,
                "monitor": self.config.training.checkpoint_metric,
            },
        }

    def on_train_start(self) -> None:
        """Setup logging of initial grasp scenes on training start."""
        train_dataset = self.trainer.train_dataloader.dataset
        val_dataset = self.trainer.val_dataloaders.dataset

        # Get base datasets (handle Subset case)
        train_base = (
            train_dataset.dataset
            if isinstance(train_dataset, torch.utils.data.Subset)
            else train_dataset
        )
        val_base = (
            val_dataset.dataset
            if isinstance(val_dataset, torch.utils.data.Subset)
            else val_dataset
        )

        self.translation_norm_params = train_base.norm_params
        # print(self.translation_norm_params)

        # First get selected_indices from the base dataset if they exist
        base_selected = (
            train_base.selected_indices
            if hasattr(train_base, "selected_indices")
            else None
        )

        # Then get the actual split indices from Subset
        if isinstance(train_dataset, torch.utils.data.Subset):
            train_indices = set(
                train_dataset.indices
            )  # These are indices into the base dataset
            val_indices = set(val_dataset.indices)

            # If base dataset had selected_indices, we need to map through them
            if base_selected is not None:
                train_indices = set(base_selected[i] for i in train_indices)
                val_indices = set(base_selected[i] for i in val_indices)
        else:
            # If not a subset, use selected_indices directly if they exist
            train_indices = (
                set(train_base.selected_indices)
                if hasattr(train_base, "selected_indices")
                else None
            )
            val_indices = (
                set(val_base.selected_indices)
                if hasattr(val_base, "selected_indices")
                else None
            )

        if train_indices is not None and val_indices is not None:
            if train_indices & val_indices:
                print(
                    "Warning: Overlapping indices found between training and validation sets."
                )

        for prefix, dataset in [
            ("train", self.trainer.train_dataloader.dataset),
            ("val", self.trainer.val_dataloaders.dataset),
        ]:
            grasp_data = dataset[0]
            scene = self.compute_grasp_scene(grasp_data)

            gripper_transform = torch.eye(4)
            gripper_transform[:3, :3] = grasp_data.rotation[:3, :3]
            gripper_transform[:3, 3] = denormalize_translation(
                grasp_data.translation, self.translation_norm_params
            ).squeeze()

            gripper_transform = wandb.Table(
                data=gripper_transform.cpu().numpy().tolist(),
                columns=["rot1", "rot2", "rot3", "tr"],
            )

            self.logger.experiment.log(
                {
                    f"{prefix}/original_grasp": scene_to_wandb_3d(scene),
                }
            )

    def duplicate_to_batch_size(self, input: Tensor, batch_size: int):
        current_size = input.size(0)
        if current_size >= batch_size:
            return input

        num_copies = batch_size // current_size
        remainder = batch_size % current_size

        duplicated = input.repeat(num_copies, *(1 for _ in range(len(input.shape) - 1)))
        if remainder > 0:
            duplicated = torch.cat([duplicated, input[:remainder]], dim=0)

        return duplicated

    def compute_grasp_scene(
        self,
        grasp_data: GraspData,
        r3_so3_inputs: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
    ):
        # Get normalized translation and rotation from inputs or grasp_data
        normalized_translation, rotation = (
            r3_so3_inputs
            if r3_so3_inputs is not None
            else (grasp_data.translation, grasp_data.rotation)
        )

        # Denormalize and adjust translation with centroid
        denormalized_translation = denormalize_translation(
            normalized_translation, self.translation_norm_params
        )
        final_translation = denormalized_translation + torch.tensor(
            grasp_data.centroid, device=denormalized_translation.device
        )
        has_collision, scene, min_distance, is_graspable = check_collision(
            rotation,
            final_translation,
            grasp_data.mesh_path,
            grasp_data.dataset_mesh_scale,
        )
        return scene

    def test_step(self, batch, batch_idx: int):
        # print("called")

        grasp_data = get_grasp_from_batch(batch)

        sdf_input = rearrange(grasp_data.sdf, "... -> 1 1 ...")

        so3_output, r3_output = sample(
            self.model,
            sdf_input,
            grasp_data.translation.device,
            torch.tensor(grasp_data.normalization_scale),
            self.config.training.num_samples_to_log,
            sdf_path=grasp_data.mesh_path,
        )
        # print("grasp_data", grasp_data)
        # print("r3_output", r3_output)
        # print("so3_output", so3_output)

        scene = self.compute_grasp_scene(grasp_data, (r3_output, so3_output))

        self.logger.experiment.log(
            {
                f"test/generated_grasp": scene_to_wandb_3d(scene),
            }
        )

    def on_test_start(self):
        test_dataset = self.trainer.test_dataloaders.dataset

        # Get base datasets (handle Subset case)
        base = (
            test_dataset.dataset
            if isinstance(test_dataset, torch.utils.data.Subset)
            else test_dataset
        )
        self.translation_norm_params = base.norm_params

    def predict_step(self, batch, batch_idx, dataloader_idx=0):
        # 'batch' should contain all grasps from one SDF, thanks to SingleSDFSampler.
        # Extract the actual data fields from your collated batch:
        print("batch_idx", batch_idx)
        grasp_data = get_grasp_from_batch(batch)

        # Here, `grasp_data.rotation` is all real rotations for that SDF,
        # `grasp_data.translation` is all real translations, etc.
        real_rotations = grasp_data.rotation
        print(real_rotations.shape)
        real_translations = grasp_data.translation
        real_sdf = grasp_data.sdf
        sdf_path = grasp_data.mesh_path  # Usually all are the same in one batch
        sdf_input = rearrange(real_sdf, "... -> 1 1 ...")
        print(sdf_input.shape, "sdf_input_size")
        # Generate synthetic grasps
        # (Example: sample 2000 predictions)
        so3_samples, r3_samples = sample(
            self.model,
            sdf_input,  # shape [1, ...]
            device=real_rotations.device,
            normalization_scale=torch.tensor(grasp_data.normalization_scale),
            num_samples=2,
            sdf_path=sdf_path,
        )

        # Compare distributions:
        from src.models.wasserstein import wasserstein_distance

        wdist_so3 = wasserstein_distance(so3_samples, real_rotations, space="so3")
        wdist_r3 = wasserstein_distance(r3_samples, real_translations, space="r3")

        self.log_dict(
            {
                "wdist_so3": wdist_so3,
                "wdist_r3": wdist_r3,
                "sdf_path": sdf_path,
            }
        )

        return {
            "sdf_path": sdf_path,
            "wdist_so3": wdist_so3,
            "wdist_r3": wdist_r3,
        }
