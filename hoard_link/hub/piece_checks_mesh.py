"""Mesh checks based on Vulcan's file measurements."""

from __future__ import annotations

import base64
import math


def check_print(path, requirements, report):
    from vulcan.asset_inspection import UNIT_TO_MM, _edge_counts, _wall_records, load_geometry, sha256_of
    try:
        from vulcan.asset_inspection import load_print_geometry, measure_mesh_edges
    except ImportError:  # Older installed inspectors still serve their existing measurements.
        load_print_geometry, measure_mesh_edges = load_geometry, None

    wall = requirements.get("min_wall_mm")
    volume = requirements.get("build_volume_mm")
    if wall is not None and (not math.isfinite(float(wall)) or float(wall) <= 0):
        raise ValueError("min_wall_mm must be positive")
    if volume is not None and (len(volume) != 3 or any(not math.isfinite(float(axis)) or float(axis) <= 0 for axis in volume)):
        raise ValueError("build_volume_mm needs positive X, Y and Z dimensions")
    _, mesh, unit, _ = load_print_geometry(path)
    to_mm = UNIT_TO_MM[unit or "mm"]
    dimensions = [round(float(axis * to_mm), 4) for axis in mesh.extents]
    if measure_mesh_edges:
        open_edges, nonmanifold, winding, locations = measure_mesh_edges(mesh, to_mm)
        inverted = [int(body[1:]) for body in locations["inverted_bodies"]]
        degenerate = locations["degenerate_triangles"]
        report.measures["degenerate_triangles"] = degenerate
        report.add("degenerate-triangles", degenerate == 0, "Count degenerate triangles.",
                   "Cuenta los triángulos degenerados.", measured=degenerate, expected=0)
    else:
        open_edges, nonmanifold = _edge_counts(mesh.faces)
        winding = bool(mesh.is_winding_consistent)
        inverted = ([index for index, body in enumerate(mesh.split(only_watertight=False), 1) if body.volume < 0]
                    if open_edges == nonmanifold == 0 and winding else [])
    watertight = open_edges == nonmanifold == 0
    report.measures.update(engine="vulcan.asset_inspection", sha256=sha256_of(path),
                           triangles=len(mesh.faces), watertight=watertight, open_edges=open_edges,
                           nonmanifold_edges=nonmanifold, winding_consistent=winding, bbox_mm=dimensions,
                           inverted_bodies=inverted, unit=unit or "mm", assumed_unit=unit is None)
    report.add("watertight", watertight, "Check whether all edges join two faces.",
               "Comprueba si todas las aristas unen dos caras.", measured=open_edges)
    report.add("nonmanifold-edges", nonmanifold == 0,
               "Count edges incident to more than two faces.", "Cuenta las aristas que comparten más de dos caras.",
               measured=nonmanifold, expected=0)
    normals = False if inverted or not winding else True if watertight else None
    report.add("normals", normals, "Check face winding and inverted closed bodies.",
               "Comprueba la orientación de las caras y los cuerpos cerrados invertidos.", details=inverted)
    report.add("build-volume", volume is None or all(float(a) <= float(b) for a, b in zip(dimensions, volume)),
               "Compare X, Y and Z extents with the print volume in file orientation.",
               "Coteja las dimensiones X, Y y Z con el volumen de impresión en la orientación del fichero.",
               measured=dimensions, expected=volume, applies=volume is not None)
    _, _, sample = _wall_records(mesh, to_mm, open_edges, nonmanifold) if wall is not None else ({}, {}, None)
    report.measures["wall_sample"] = sample
    measured = (sample or {}).get("min_mm")
    report.add("wall-thickness", True if wall is None else None if measured is None else measured >= float(wall),
               "Compare the smallest sampled wall thickness with the target.",
               "Coteja el menor grosor de pared muestreado con el objetivo.",
               measured=measured, expected=wall, detail=(sample or {}).get("status"), applies=wall is not None)


def _buffer(document, index, binary, folder):
    uri = document["buffers"][index].get("uri")
    if not uri:
        return binary
    if uri.startswith("data:"):
        return base64.b64decode(uri.split(",", 1)[1])
    from urllib.parse import unquote, urlsplit

    if urlsplit(uri).scheme:
        raise ValueError("External buffer URL cannot be measured")
    return (folder / unquote(uri)).read_bytes()


def _accessor(document, index, buffers):
    import numpy as np

    accessor = document["accessors"][index]
    if "sparse" in accessor or accessor["type"] != "VEC4":
        raise ValueError("Sparse and non-VEC4 skin accessors cannot be measured")
    dtype = np.dtype({5121: "u1", 5123: "<u2", 5126: "<f4"}[accessor["componentType"]])
    view = document["bufferViews"][accessor["bufferView"]]
    if view.get("extensions"):
        raise ValueError("Compressed skin accessors cannot be measured")
    return np.ndarray((accessor["count"], 4), dtype, buffer=buffers[view["buffer"]],
                      offset=view.get("byteOffset", 0) + accessor.get("byteOffset", 0),
                      strides=(view.get("byteStride", 4 * dtype.itemsize), dtype.itemsize))


def _weights(document, binary, folder):
    import numpy as np

    buffers = [_buffer(document, i, binary, folder) for i in range(len(document.get("buffers", [])))]
    nodes = document.get("nodes", [])
    skins = document.get("skins", [])
    rows = []
    for index, node in enumerate(nodes):
        if "mesh" not in node or "skin" not in node:
            continue
        joint_count = len(skins[node["skin"]]["joints"])
        for prim_no, primitive in enumerate(document["meshes"][node["mesh"]]["primitives"]):
            attrs = primitive.get("attributes", {})
            count = document["accessors"][attrs["POSITION"]]["count"]
            sums = np.zeros(count)
            invalid = np.zeros(count, dtype=bool)
            sets = {key.split("_", 1)[1] for key in attrs if key.startswith(("WEIGHTS_", "JOINTS_"))}
            for group in sorted(sets):
                weights = _accessor(document, attrs[f"WEIGHTS_{group}"], buffers)
                joints = _accessor(document, attrs[f"JOINTS_{group}"], buffers)
                invalid |= ((weights < 0) | ~np.isfinite(weights) | ((weights > 0) & (joints >= joint_count))).any(axis=1)
                sums += weights.sum(axis=1)
            rows.append({"node": node.get("name") or index, "primitive": prim_no, "vertices": count,
                         "unweighted": int((sums <= 0).sum()), "invalid_weights": int(invalid.sum())})
    return rows


def check_game(path, requirements, report):
    from vulcan.asset_inspection import gltf_facts, read_gltf

    if path.suffix.lower() not in {".glb", ".gltf"}:
        raise ValueError("Skin checks accept GLB and glTF")
    facts = gltf_facts(path)
    document, binary = read_gltf(path)
    nodes = document.get("nodes", [])
    joints = {index for skin in document.get("skins", []) for index in skin["joints"]}
    bones = sorted(nodes[index].get("name", str(index)) for index in joints)
    named = {node["name"]: node for node in nodes if node.get("name")}
    pivots = {name: {key: named[name][key] for key in ("translation", "rotation", "matrix") if key in named[name]}
              for name in requirements.get("pivots", []) if name in named}
    budget = requirements.get("max_triangles")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < 1):
        raise ValueError("max_triangles must be a positive integer")
    sizes = facts["image_sizes"]
    report.measures.update(engine="vulcan.gltf_facts", triangles=facts["triangles"], texture_sizes=sizes,
                           bones=bones, pivots=pivots)
    report.add("triangles", None if budget is None else facts["triangles"] <= budget,
               "Compare the triangle count with the game budget.", "Coteja los triángulos con el presupuesto del juego.",
               measured=facts["triangles"], maximum=budget)
    bad_sizes = [size for size in sizes if size is not None and any(px <= 0 or px & (px - 1) for px in size)]
    report.add("textures", False if bad_sizes else None if None in sizes else True,
               "Check that both axes of each texture are powers of two.",
               "Comprueba que ambos ejes de cada textura son potencias de dos.", details=bad_sizes)
    required_bones = requirements.get("bones", [])
    # Rigid props and set dressing carry no skin; skin checks apply only when one is present or requested.
    deformable = bool(bones or required_bones or requirements.get("skinned"))
    missing = sorted(set(required_bones) - set(bones))
    report.add("bones", (bool(bones) and not missing) if deformable else True,
               "Check skin joints and required bone names.",
               "Comprueba la presencia de huesos y los nombres requeridos.", details=missing, applies=deformable)
    required_pivots = requirements.get("pivots", [])
    missing = sorted(set(required_pivots) - pivots.keys())
    report.add("pivots", not missing, "Find the nodes declared as pivots.",
               "Localiza los nodos declarados como pivotes.", details=missing, applies=bool(required_pivots))
    rows = _weights(document, binary, path.parent)
    report.measures["skin_primitives"] = rows
    problems = [row for row in rows if row["unweighted"] or row["invalid_weights"]]
    report.add("vertex-weights", (bool(rows) and not problems) if deformable else True,
               "Count vertices without valid influences in skinned primitives.",
               "Cuenta los vértices sin influencias válidas en las primitivas con piel.", details=problems,
               applies=deformable)
