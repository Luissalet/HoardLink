"""Convert a 3MF build to a temporary geometry-only preview for Eyes."""

import math

from .eyes_render import EyesError

# 3MF's default unit is millimetres. Convert the entire build (including translations).
METRES = {"micron": 1e-6, "millimeter": .001, "centimeter": .01,
          "meter": 1.0, "inch": .0254, "foot": .3048}


def to_glb(source, target):
    try:
        import trimesh
    except ImportError as exc:
        raise EyesError("3MF needs trimesh / Para abrir 3MF se necesita trimesh") from exc

    try:
        scene = trimesh.load_scene(source, file_type="3mf", process=False)
        unit = scene.metadata.get("units", "millimeter")
        if unit == "millimeters":
            unit = "millimeter"  # default used by the trimesh reader
        if not scene.graph.nodes_geometry or unit not in METRES:
            raise ValueError(f"No build mesh or unsupported unit: {unit}")
        scene.apply_scale(METRES[unit])
        # The source is Z-up; the intermediate GLB is Y-up.
        scene.apply_transform(trimesh.transformations.rotation_matrix(-math.pi / 2, (1, 0, 0)))
        target.write_bytes(scene.export(file_type="glb"))
    except (ImportError, OSError, ValueError, KeyError, StopIteration, IndexError, RuntimeError) as exc:
        raise EyesError(f"Cannot open the 3MF build / No se puede abrir la geometría 3MF: {exc}") from exc
    return unit
