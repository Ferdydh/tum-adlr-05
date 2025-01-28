from scipy.spatial.transform import Rotation
from typing import Tuple, Optional  
import torch
from einops import rearrange
from torch import Tensor, vmap
from geomstats.geometry.special_orthogonal import SpecialOrthogonal

from src.models.velocity_mlp import VelocityNetwork


def rotmat_to_rotvec(matrix):
    """
    Convert rotation matrices to rotation vectors (axis-angle representation).
    This combines the previous quaternion conversion and vector conversion steps.

    Args:
        matrix: Batch of 3x3 rotation matrices
    Returns:
        Batch of 3D rotation vectors (axis-angle representation)
    """
    if len(matrix.shape) != 3 or matrix.shape[-1] != 3 or matrix.shape[-2] != 3:
        raise ValueError("Input has to be a batch of 3x3 Tensors.")

    # Step 1: Convert rotation matrix to quaternion
    matrix = matrix.to(torch.float64)
    num_rots = matrix.shape[0]

    # Calculate diagonal and trace for quaternion conversion
    matrix_diag = torch.diagonal(matrix, dim1=-2, dim2=-1)
    matrix_trace = torch.sum(matrix_diag, dim=-1, keepdim=True)
    decision = torch.cat((matrix_diag, matrix_trace), dim=-1)
    choice = torch.argmax(decision, dim=-1)

    # Initialize quaternion output
    quat = torch.zeros((num_rots, 4), dtype=matrix.dtype, device=matrix.device)

    # Handle case where choice is not trace (not 3)
    not_three_mask = choice != 3
    i = choice[not_three_mask]
    j = (i + 1) % 3
    k = (j + 1) % 3

    quat[not_three_mask, i] = (
        1 - decision[not_three_mask, 3] + 2 * matrix[not_three_mask, i, i]
    ).to(torch.float64)
    quat[not_three_mask, j] = (
        matrix[not_three_mask, j, i] + matrix[not_three_mask, i, j]
    ).to(torch.float64)
    quat[not_three_mask, k] = (
        matrix[not_three_mask, k, i] + matrix[not_three_mask, i, k]
    ).to(torch.float64)
    quat[not_three_mask, 3] = (
        matrix[not_three_mask, k, j] - matrix[not_three_mask, j, k]
    ).to(torch.float64)

    # Handle case where choice is trace (3)
    three_mask = ~not_three_mask
    quat[three_mask, 0] = (matrix[three_mask, 2, 1] - matrix[three_mask, 1, 2]).to(
        torch.float64
    )
    quat[three_mask, 1] = (matrix[three_mask, 0, 2] - matrix[three_mask, 2, 0]).to(
        torch.float64
    )
    quat[three_mask, 2] = (matrix[three_mask, 1, 0] - matrix[three_mask, 0, 1]).to(
        torch.float64
    )
    quat[three_mask, 3] = (1 + decision[three_mask, 3]).to(torch.float64)

    # Normalize quaternion
    quat = quat / torch.norm(quat, dim=-1, keepdim=True)

    # Step 2: Convert quaternion to rotation vector
    quat = torch.where(quat[..., 3:4] < 0, -quat, quat)
    angle = 2.0 * torch.atan2(torch.norm(quat[..., :3], dim=-1), quat[..., 3])
    angle2 = angle * angle

    # Handle small and large angles differently
    small_scale = 2 + angle2 / 12 + 7 * angle2 * angle2 / 2880
    large_scale = angle / torch.sin(angle / 2 + 1e-6)
    scale = torch.where(angle <= 1e-3, small_scale, large_scale)

    return scale[..., None] * quat[..., :3]


def sample_location_and_conditional_flow(x0, x1, t):
    """
    Compute conditional flow between two rotation matrices in SO(3) at specified time points.
    This implements a conditional flow matcher for the Special Orthogonal group in 3D.

    Args:
        x0: Starting rotation matrices (batch_size x 3 x 3)
        x1: Target rotation matrices (batch_size x 3 x 3)
        t: Time points between 0 and 1 (batch_size)

    Returns:
        xt: Interpolated rotation matrices at time t
        ut: Velocity field at time t (tangent vectors)
    """
    vec_manifold = SpecialOrthogonal(n=3, point_type="vector")

    # Convert rotations to axis-angle representation and compute log map
    rot_x0 = rotmat_to_rotvec(x0)
    rot_x1 = rotmat_to_rotvec(x1)
    log_x1 = vec_manifold.log_not_from_identity(rot_x1, rot_x0)

    # print(f"Max rot_x0: {rot_x0.abs().max().item()}")
    # print(f"Max rot_x1: {rot_x1.abs().max().item()}")
    # print(f"Max log_x1: {log_x1.abs().max().item()}")

    
    # if torch.norm(rot_x0 - rot_x1) < 1e-6:
    #     print("x0 and x1 are too close, potential numerical instability")
    #     log_x1 = torch.zeros_like(log_x1)  # Set tangent vector to zero
    # if torch.norm(rot_x0 + rot_x1) < 1e-6:
    #     print("x0 and x1 are antipodal, potential numerical instability")
    #     log_x1 = torch.zeros_like(log_x1)  # Set tangent vector to zero
    # # Print statements to check for NaNs
    # if torch.isnan(rot_x0).any():
    #     print("NaN detected in rot_x0")
    # if torch.isnan(rot_x1).any():
    #     print("NaN detected in rot_x1")
    # if torch.isnan(log_x1).any():
    #     print("NaN detected in log_x1")
    # Ensure t requires gradient for velocity computation
    t.requires_grad = True

    # Compute interpolated rotation at time t
    xt = vec_manifold.exp_not_from_identity(t.reshape(-1, 1) * log_x1, rot_x0)
    xt = vec_manifold.matrix_from_rotation_vector(xt)
    
    # #print(t.shape)
    # if torch.isnan(xt).any():
    #     print("NaN detected in xt")

    # Compute velocity field using automatic differentiation
    xt_flat = rearrange(xt, "b c d -> b (c d)", c=3, d=3)
    #print(f"Max xt_flat: {xt_flat.abs().max().item()}")

    # if torch.isnan(xt_flat).any():
    #     print("NaN detected in xt_flat")
    #     print("xt_flat values:", xt_flat)
    #     raise ValueError("NaN detected in xt_flat, stopping computation.")

    #torch.autograd.set_detect_anomaly(True)
    def index_time_der(i):
        return torch.autograd.grad(xt_flat, t, i, create_graph=True, retain_graph=True)[
            0
        ]
    #print(log_x1 @ xt )
    xt_dot = vmap(index_time_der, in_dims=1)(
        torch.eye(9).to(xt.device).repeat(xt_flat.shape[0], 1, 1)
    )
    #skew_v = vector_to_skew(log_x1)         # shape (B, 3, 3)
    #skew_v = vec_manifold.matrix_from_rotation_vector(log_x1)
    #print(skew_v.shape,xt.shape)
    #xt_dot_manual = torch.einsum("bij,bjk->bik", xt, skew_v) 
    #xt_dot_manual = torch.einsum("bij,bjk->bik", skew_v, xt)# shape (B, 3, 3)
    ut = rearrange(xt_dot, "(c d) b -> b c d", c=3, d=3)
    # Check if xt_dot and xt_dot_manual are close
    # if torch.allclose(ut, xt_dot_manual, atol=1e-6):
    #     #print("xt_dot and xt_dot_manual are close")
    # else:
    #     #print("xt_dot and xt_dot_manual are not close")
    #     max_diff = torch.max(torch.abs(ut - xt_dot_manual))
        #print(f"Max difference between ut and xt_dot_manual: {max_diff.item()}")
        #print(f"Max value in ut: {torch.max(ut).item()}")
        #print(f"Max value in xt_dot_manual: {torch.max(xt_dot_manual).item()}")
    #print(xt_dot.shape)
    #print((log_x1 @ xt).shape,xt.shape,log_x1.shape)
    # Check if the matrices are close
    # if torch.allclose(xt_dot, log_x1 @ xt, atol=1e-6):
    #     #print("xt_dot and log_x1 @ xt are close")
    # else:
    #     #print("xt_dot and log_x1 @ xt are not close")
    #torch.autograd.set_detect_anomaly(False)


    # #Print statements to check for NaNs
    # if torch.isnan(xt_dot).any():
    #     #print("NaN detected in xt_dot")
    #     #print(f"Max log_x1 @ xt: {(log_x1 @ xt).abs().max().item()}")
        
    #     ##print(xt)
    # if torch.isnan(ut).any():
    #     #print("NaN detected in ut")
    return xt, ut


@torch.no_grad()
def inference_step(
    model: VelocityNetwork,
    so3_state: Tensor,
    r3_state: Tensor,
    sdf_input: Tensor,
    normalization_scale: Tensor,
    t: Tensor,
    dt: Tensor,
    sdf_path: Optional[Tuple[str]] = None,
) -> Tuple[Tensor, Tensor]:
    """Single step inference.

    Args:
        model: VelocityNetwork model
        so3_state: Current SO3 state [batch, 3, 3]
        r3_state: Current R3 state [batch, 3]
        t: Current time [batch, 1]
        dt: Time step size [1]

    Returns:
        Tuple of (next_so3_state, next_r3_state)
    """
    # Get velocities - model now expects [batch, 3, 3] input
    so3_velocity, r3_velocity = model(so3_state, r3_state, sdf_input, t,normalization_scale, sdf_path)

    # R3 update remains the same
    r3_next = r3_state + dt * r3_velocity

    # SO3 update with exponential map
    # so3_velocity is already in [batch, 3, 3] format
    skew_sym = torch.einsum("...ij,...ik->...jk", so3_state, so3_velocity * dt)
    so3_next = torch.einsum(
        "...ij,...jk->...ik", so3_state, torch.linalg.matrix_exp(skew_sym)
    )

    return so3_next, r3_next


@torch.no_grad()
def sample(
    model: VelocityNetwork,
    sdf_input: Tensor,
    device: torch.device,
    normalization_scale: Tensor,
    num_samples: int = 1,
    steps: int = 200,
    sdf_path: Optional[Tuple[str]] = None,
) -> Tuple[Tensor, Tensor]:
    """Generate samples.

    Args:
        model: VelocityNetwork model
        device: Device to generate samples on
        num_samples: Number of samples to generate
        steps: Number of integration steps

    Returns:
        Tuple of (so3_samples, r3_samples) where:
            so3_samples: [num_samples, 3, 3]
            r3_samples: [num_samples, 3]
    """
    # Initialize random starting points - already in correct shape
    so3_traj = torch.tensor(
        Rotation.random(num_samples).as_matrix(), dtype=torch.float64
    ).to(device)  # Shape: [num_samples, 3, 3]

    r3_traj = torch.randn(num_samples, 3, dtype=torch.float64).to(device)

    # Setup time steps
    t = torch.linspace(0, 1, steps).to(device)
    dt = torch.tensor([1 / steps]).to(device)

    # Generate trajectories
    for t_i in t:
        t_batch = (
            torch.tensor([t_i], dtype=torch.float64).repeat(num_samples).to(device)
        )
        so3_traj, r3_traj = inference_step(
            model, so3_traj, r3_traj, sdf_input,normalization_scale, t_batch, dt,sdf_path
        )

    # No need to reshape SO3 output as it's already in the correct shape
    return so3_traj, r3_traj


def batch_vector_to_skew_symmetric(v: torch.Tensor) -> torch.Tensor:
    """
    Create skew-symmetric matrices from a batch of 3D vectors.

    Args:
        v: A tensor of shape (batch_size, 3)

    Returns:
        A tensor of skew-symmetric matrices of shape (batch_size, 3, 3)
    """
    assert v.shape[-1] == 3, "The last dimension of the input tensor must be 3"
    
    batch_size = v.shape[0]
    
    S = torch.zeros((batch_size, 3, 3), dtype=v.dtype, device=v.device)
    
    S[:, 0, 1] = -v[:, 2]
    S[:, 0, 2] = v[:, 1]
    S[:, 1, 0] = v[:, 2]
    S[:, 1, 2] = -v[:, 0]
    S[:, 2, 0] = -v[:, 1]
    S[:, 2, 1] = v[:, 0]
    
    return S

import torch

def vector_to_skew(vec: torch.Tensor) -> torch.Tensor:
    """
    Convert a batch of 3D vectors into their corresponding
    batch of 3x3 skew-symmetric matrices.

    Args:
        vec: shape (B, 3), i.e. each row is (v_x, v_y, v_z)

    Returns:
        skew: shape (B, 3, 3), where skew[i] is the 3x3
              skew-symmetric matrix for vec[i].
    """
    if vec.ndim != 2 or vec.shape[-1] != 3:
        raise ValueError("Expected vec to have shape (B, 3).")

    vx, vy, vz = vec[:, 0], vec[:, 1], vec[:, 2]
    zero = torch.zeros_like(vx)

    # Construct row by row
    row0 = torch.stack([ zero, -vz,   vy], dim=-1)  # [B, 3]
    row1 = torch.stack([  vz,  zero, -vx], dim=-1)
    row2 = torch.stack([-vy,   vx,   zero], dim=-1)

    # Stack rows into a [B, 3, 3] tensor
    skew = torch.stack([row0, row1, row2], dim=1)
    return skew
