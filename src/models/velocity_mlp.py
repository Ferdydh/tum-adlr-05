from typing import Tuple
import torch
import torch.nn as nn
from torch import Tensor
from einops import rearrange
from src.models.sdf_encoder import VoxelSDFEncoder
from src.core.config import ExperimentConfig


class VelocityNetwork(nn.Module):
    """Neural network for predicting the velocity field."""

    def __init__(
        self, config: ExperimentConfig, input_dim: int = 12, hidden_dim: int = 128
    ):
        super().__init__()

        activation = config.model.activation  # nn.GELU
        input_dim = config.model.input_dim  # 12
        hidden_dim = config.model.hidden_dim  # 128
        num_hidden_layers = config.model.num_hidden_layers  # 3
        voxel_output_size = config.model.voxel_output_size  # 256
        self.sdf_encoder = VoxelSDFEncoder().float()
        # Time embedding
        self.time_proj = nn.Sequential(nn.Linear(1, hidden_dim), activation())

        # Input projection
        self.input_proj = nn.Linear(input_dim + voxel_output_size, hidden_dim)
        # TODO Two linear layers back to back equivalent to one
        # Hidden layers
        layers = []
        layers.append(activation())
        for _ in range(num_hidden_layers):
            layers.append(nn.Linear(hidden_dim, hidden_dim))
            layers.append(activation())
        self.hidden_layers = nn.Sequential(*layers)

        # Output projection
        self.final = nn.Linear(hidden_dim, input_dim)

    def forward(
        self, so3_input: Tensor, r3_input: Tensor, sdf_input: Tensor, t: Tensor
    ) -> Tuple[Tensor, Tensor]:
        """Forward pass computing velocities for both SO3 and R3 components.

        Args:
            so3_input: SO3 input tensor [batch, 3, 3]
            r3_input: R3 input tensor [batch, 3]
            sdf_input: SDF input tensor [batch, 48, 48, 48]
            t: Time tensor [batch] or [batch, 1]

        Returns:
            Tuple of (so3_velocity [batch, 3, 3], r3_velocity [batch, 3])
        """
        # Ensure t is 2D [batch, 1]
        if t.dim() == 1:
            t = t.unsqueeze(-1)
        elif t.dim() == 3:
            t = t.squeeze(1)

        sdf_features = self.sdf_encoder(sdf_input)

        sdf_features = sdf_features.repeat(
            so3_input.shape[0] // sdf_features.shape[0] + 1, 1
        )[: so3_input.shape[0]]
        # Flatten SO3 input for processing
        so3_flat = rearrange(so3_input, "b c d -> b (c d)")

        # Combine inputs
        x = torch.cat([sdf_features, so3_flat, r3_input], dim=-1)

        # Process time and state
        t_emb = self.time_proj(t)
        h = self.input_proj(x)

        # Combine time embedding and state
        h = h + t_emb

        # Pass through hidden layers and get combined velocity
        h = self.hidden_layers(h)
        combined_velocity = self.final(h)

        # Split outputs
        so3_velocity_flat = combined_velocity[:, :9]
        r3_velocity = combined_velocity[:, 9:]

        # Reshape SO3 velocity back to matrix form for tangent space projection
        so3_velocity = rearrange(so3_velocity_flat, "b (c d) -> b c d", c=3, d=3)

        # Project to tangent space
        skew_symmetric_part = 0.5 * (so3_velocity - so3_velocity.permute(0, 2, 1))
        so3_velocity = so3_input @ skew_symmetric_part

        return so3_velocity, r3_velocity
