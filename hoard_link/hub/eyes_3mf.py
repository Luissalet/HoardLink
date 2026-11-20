"""Convert a 3MF build to a temporary geometry-only preview for Eyes."""

import math
from xml.etree.ElementTree import ParseError, XMLPullParser
from zipfile import BadZipFile, ZipFile

from .eyes_render import EyesError, FRONT_AXIS_TURNS

# 3MF's default unit is millimetres. Convert the entire build (including translations).
METRES = {"micron": 1e-6, "millimeter": .001, "centimeter": .01,
          "meter": 1.0, "inch": .0254, "foot": .3048}


def declared_front(source):
    """Read optional orientation metadata before the mesh resources."""
    core = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
    try:
        with ZipFile(source) as archive, archive.open("3D/3dmodel.model") as model:
            parser = XMLPullParser(("start", "end"))
            for _ in range(16):
                data = model.read(4096)
                if not data:
                    break
                parser.feed(data)
                for event, node in parser.read_events():
                    if event == "start" and node.tag == core + "resources":
                        return None
                    if event == "end" and node.tag == core + "metadata" and node.get("name") == "hoard:FrontAxis":
                        axis = (node.text or "").strip()
                        return axis if axis in FRONT_AXIS_TURNS else None
    except (BadZipFile, KeyError, OSError, ParseError):
        pass  # An orientation hint is optional; the normal mesh loader reports invalid builds.
    return None


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
