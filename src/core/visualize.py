from typing import List, Tuple, Union

import numpy as np
import torch
import trimesh
import wandb
from scipy.spatial import cKDTree

from src.data.util import enforce_trimesh


def create_parallel_gripper_mesh(
    color: List[int] = [0, 0, 255],
    cylinder_sections: int = 12,
    gripper_width: float = 0.082,
    gripper_height: float = 0.11217,
    finger_thickness: float = 0.002,
    base_height: float = 0.066,
) -> trimesh.Trimesh:
    """Creates a 3D mesh representing a parallel-jaw gripper."""
    half_width = gripper_width / 2

    # Create basic components
    left_finger = trimesh.creation.cylinder(
        radius=finger_thickness,
        sections=cylinder_sections,
        segment=[
            [half_width, 0, base_height],
            [half_width, 0, gripper_height],
        ],
    )

    right_finger = trimesh.creation.cylinder(
        radius=finger_thickness,
        sections=cylinder_sections,
        segment=[
            [-half_width, 0, base_height],
            [-half_width, 0, gripper_height],
        ],
    )

    base_cylinder = trimesh.creation.cylinder(
        radius=finger_thickness,
        sections=cylinder_sections,
        segment=[[0, 0, 0], [0, 0, base_height]],
    )

    connector = trimesh.creation.cylinder(
        radius=finger_thickness,
        sections=cylinder_sections,
        segment=[[-half_width, 0, base_height], [half_width, 0, base_height]],
    )

    # Combine components
    gripper_mesh = trimesh.util.concatenate(
        [base_cylinder, connector, right_finger, left_finger]
    )
    gripper_mesh.visual.face_colors = color

    return gripper_mesh


def _get_mesh_points(
    mesh: Union[trimesh.Trimesh, trimesh.Scene], num_samples: int = 5000
) -> np.ndarray:
    """Extract points from a mesh or scene, handling both cases appropriately."""
    if isinstance(mesh, trimesh.Scene):
        # Combine points from all geometries in the scene
        points = []
        for geom in mesh.geometry.values():
            if isinstance(geom, trimesh.Trimesh):
                # Sample points from the surface
                sampled = geom.sample(num_samples // len(mesh.geometry))
                points.append(sampled)
                # Add vertices
                points.append(geom.vertices)
        return np.vstack(points)
    else:
        # For single mesh, combine sampled points and vertices
        sampled = mesh.sample(num_samples)
        return np.vstack([sampled, mesh.vertices])


def _check_mesh_collision(
    gripper_mesh: trimesh.Trimesh,
    object_mesh: Union[trimesh.Trimesh, trimesh.Scene],
    voxel_size: float = 0.001,  # Size of voxels for discretization
) -> Tuple[bool, float]:
    """Collision checking using point clouds and voxelization."""
    # Get points from both meshes
    gripper_points = _get_mesh_points(gripper_mesh)
    object_points = _get_mesh_points(object_mesh)

    # Print point clouds size for debugging
    # print(f"Number of gripper points: {len(gripper_points)}")
    # print(f"Number of object points: {len(object_points)}")

    # Discretize points to voxels
    gripper_voxels = np.round(gripper_points / voxel_size) * voxel_size
    object_voxels = np.round(object_points / voxel_size) * voxel_size

    # Remove duplicates after voxelization
    gripper_voxels = np.unique(gripper_voxels, axis=0)
    object_voxels = np.unique(object_voxels, axis=0)

    # First check: Axis-aligned bounding box intersection
    gripper_min = np.min(gripper_voxels, axis=0)
    gripper_max = np.max(gripper_voxels, axis=0)
    object_min = np.min(object_voxels, axis=0)
    object_max = np.max(object_voxels, axis=0)

    bbox_overlap = np.all(gripper_max >= object_min) and np.all(
        object_max >= gripper_min
    )
    if not bbox_overlap:
        return False, np.inf

    # Build KD-trees for efficient nearest neighbor search
    gripper_tree = cKDTree(gripper_voxels)
    object_tree = cKDTree(object_voxels)

    # Find minimum distances between point sets
    distances_g2o, _ = gripper_tree.query(object_voxels, k=1)
    distances_o2g, _ = object_tree.query(gripper_voxels, k=1)

    min_distance = min(np.min(distances_g2o), np.min(distances_o2g))
    # print(f"Minimum distance between point clouds: {min_distance}")

    # Check for overlapping points (considering discretization)
    collision_threshold = voxel_size * 1.5
    return min_distance < collision_threshold, min_distance


def check_collision(
    rotation_matrix: torch.Tensor,
    translation_vector: torch.Tensor,
    object_mesh_path: str,
    mesh_scale: float,
) -> Tuple[bool, trimesh.Scene, float]:
    """Checks for collisions between one or multiple gripper poses and object.

    Args:
        rotation_matrix: Single or batch of rotation matrices with shape (3, 3) or (N, 3, 3)
        translation_vector: Single or batch of translation vectors with shape (3,) or (N, 3)
        object_mesh_path: Path to object mesh file
        mesh_scale: Scale factor for object mesh

    Returns:
        Tuple containing:
        - bool: True if any gripper has collision
        - trimesh.Scene: Scene with object and gripper mesh(es)
        - float: Minimum distance between gripper(s) and object
    """
    # Load and scale object mesh
    object_mesh = trimesh.load(object_mesh_path)

    if torch.is_tensor(mesh_scale):
        mesh_scale = mesh_scale.cpu().numpy()
    object_mesh.apply_scale(mesh_scale)
    object_mesh = enforce_trimesh(object_mesh)

    # Check if rotation matrix is SO3 (3x3) or batched (Nx3x3)
    is_so3 = rotation_matrix.shape == torch.Size([3, 3])
    if is_so3:
        rotation_matrix = rotation_matrix.unsqueeze(0)
        translation_vector = translation_vector.unsqueeze(0)

    # Validate shapes
    assert len(rotation_matrix.shape) == 3 and rotation_matrix.shape[1:] == (3, 3), (
        f"Expected rotation matrix shape (N, 3, 3), got {rotation_matrix.shape}"
    )
    assert len(translation_vector.shape) == 2 and translation_vector.shape[1] == 3, (
        f"Expected translation vector shape (N, 3), got {translation_vector.shape}"
    )
    assert rotation_matrix.shape[0] == translation_vector.shape[0], (
        f"Batch sizes don't match: {rotation_matrix.shape[0]} != {translation_vector.shape[0]}"
    )

    batch_size = rotation_matrix.shape[0]
    gripper_meshes = []
    has_any_collision = False
    min_distance_overall = float("inf")

    # Process each grasp
    for batch_idx in range(batch_size):
        # Create transformation matrix
        gripper_transform = torch.eye(4)
        gripper_transform[:3, :3] = rotation_matrix[batch_idx]
        gripper_transform[:3, 3] = translation_vector[batch_idx]
        gripper_transform = gripper_transform.cpu().numpy()

        # Create and transform gripper mesh
        gripper_mesh = create_parallel_gripper_mesh(color=[0, 255, 0])
        gripper_mesh.apply_transform(gripper_transform)

        # Check collision
        has_collision, min_distance = _check_mesh_collision(gripper_mesh, object_mesh)

        # Update overall collision status and minimum distance
        has_any_collision = has_any_collision or has_collision
        min_distance_overall = min(min_distance_overall, min_distance)

        # Update visualization color
        color = [255, 0, 0] if has_collision else [0, 255, 0]
        gripper_mesh.visual.face_colors = color

        gripper_meshes.append(gripper_mesh)

    # Create visualization
    if isinstance(object_mesh, trimesh.Scene):
        scene = object_mesh
        for gripper_mesh in gripper_meshes:
            scene.add_geometry(gripper_mesh)
    else:
        all_meshes = [object_mesh] + gripper_meshes
        scene = trimesh.Scene(all_meshes)

    return has_any_collision, scene, min_distance_overall


def scene_to_wandb_3d(scene: trimesh.Scene) -> wandb.Object3D:
    scene.export("logs/mesh.glb")
    return wandb.Object3D("logs/mesh.glb")
