"""Runs INSIDE Blender (``blender -b --factory-startup --python eyes_blender.py -- spec.json``) for the eyes facet.

Opens one model file, optionally stands it up (``up``), turns it (``turn``, degrees counterclockwise seen from
above) and poses its armature (``pose``), then renders the requested anatomical views with a neutral clay
material and/or its own colours. The camera is orthographic by default and the same for every view; the result
reports it in absolute numbers (``resolved.camera``) so a later revision can be shot from exactly the same place.
Never imported by the hub (it needs ``bpy``): ``eyes_render.look_mesh`` writes the spec and reads the
``EYES_RESULT`` line this prints. Works on Blender 4.x and 5.x.
"""

import json
import math
import sys
import traceback

import bpy
from mathutils import Euler, Vector

# Named after the subject's own side: facing -Y, its right hand is at -X.
DIRECTIONS = {
    "front": (0.0, -1.0), "front_right": (-0.7071, -0.7071), "right": (-1.0, 0.0), "back_right": (-0.7071, 0.7071),
    "back": (0.0, 1.0), "back_left": (0.7071, 0.7071), "left": (1.0, 0.0), "front_left": (0.7071, -0.7071),
}
NEUTRAL = (0.78, 0.76, 0.72)
BACKGROUND = (0.20, 0.22, 0.25)


def _import(path):
    lower = path.lower()
    if lower.endswith(".blend"):
        bpy.ops.wm.open_mainfile(filepath=path)
        return
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ops = bpy.ops
    if lower.endswith((".glb", ".gltf")):
        ops.import_scene.gltf(filepath=path)
    elif lower.endswith(".fbx"):
        ops.import_scene.fbx(filepath=path)
    elif lower.endswith(".obj"):
        (ops.wm.obj_import if hasattr(ops.wm, "obj_import") else ops.import_scene.obj)(filepath=path)
    elif lower.endswith(".stl"):
        (ops.wm.stl_import if hasattr(ops.wm, "stl_import") else ops.import_mesh.stl)(filepath=path)
    elif lower.endswith(".ply"):
        (ops.wm.ply_import if hasattr(ops.wm, "ply_import") else ops.import_mesh.ply)(filepath=path)
    elif lower.endswith((".usd", ".usdz", ".usdc")):
        ops.wm.usd_import(filepath=path)
    elif lower.endswith(".dae"):
        ops.wm.collada_import(filepath=path)
    else:
        raise RuntimeError(f"No Blender importer for {path}")


def _scene_root(spec, warnings):
    scene = bpy.context.scene
    roots = [o for o in scene.objects if o.parent is None and o.type not in ("CAMERA", "LIGHT")]
    root = bpy.data.objects.new("EyesRoot", None)
    scene.collection.objects.link(root)
    for obj in roots:
        matrix = obj.matrix_world.copy()
        obj.parent = root
        obj.matrix_world = matrix
    rx = math.radians(90.0) if spec.get("up") == "y" else 0.0
    root.rotation_euler = Euler((rx, 0.0, math.radians(float(spec.get("turn") or 0.0))), "XYZ")
    for obj in scene.objects:
        if obj.type in ("CAMERA", "LIGHT"):
            obj.hide_render = True
    return root


def _pose(spec, warnings):
    pose = spec.get("pose")
    scene = bpy.context.scene
    armatures = [o for o in scene.objects if o.type == "ARMATURE"]
    if not pose:
        return None
    if not armatures:
        warnings.append("pose ignored: the model has no armature")
        return None
    applied = {"bones": {}, "action": None, "frame": None}
    action_name = pose.get("action")
    if action_name:
        action = bpy.data.actions.get(action_name)
        if action is None:
            warnings.append(f"no action named {action_name}; actions: {[a.name for a in bpy.data.actions][:30]}")
        else:
            for arm in armatures:
                if arm.animation_data is None:
                    arm.animation_data_create()
                arm.animation_data.action = action
            applied["action"] = action_name
    if pose.get("frame") is not None:
        scene.frame_set(int(pose["frame"]))
        applied["frame"] = int(pose["frame"])
    for bone_name, angles in (pose.get("bones") or {}).items():
        hit = None
        for arm in armatures:
            hit = arm.pose.bones.get(bone_name)
            if hit:
                break
        if hit is None:
            warnings.append(f"no bone named {bone_name}")
            continue
        hit.rotation_mode = "XYZ"
        hit.rotation_euler = Euler([math.radians(float(a)) for a in angles], "XYZ")
        applied["bones"][bone_name] = [float(a) for a in angles]
    bpy.context.view_layer.update()
    return applied


def _bounds():
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.hide_render:
            continue
        evaluated = obj.evaluated_get(depsgraph)
        points.extend(evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box)
    if not points:
        raise RuntimeError("The file has no mesh to look at")
    low = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    high = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    return low, high


def _facts(low, high):
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    armatures = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    images = [i for i in bpy.data.images if i.source in ("FILE", "PACKED") or i.packed_file]
    bones = [b.name for a in armatures for b in a.data.bones]
    return {
        "meshes": len(meshes), "mesh_names": [o.name for o in meshes][:60],
        "vertices": sum(len(o.data.vertices) for o in meshes), "faces": sum(len(o.data.polygons) for o in meshes),
        "materials": sorted({s.material.name for o in meshes for s in o.material_slots if s.material})[:60],
        "textures": len(images),
        "color_attributes": sorted({c.name for o in meshes for c in getattr(o.data, "color_attributes", [])}),
        "armatures": len(armatures), "bones": bones[:200], "bone_count": len(bones),
        "actions": [a.name for a in bpy.data.actions][:60],
        "dimensions_m": [round(v, 4) for v in (high - low)],
        "bounds_m": [[round(v, 4) for v in low], [round(v, 4) for v in high]],
    }


def _camera_spec(spec, low, high):
    given = dict(spec.get("camera") or {})
    center = given.get("center") or [round(v, 5) for v in (low + high) / 2]
    extent = max((high - low).x, (high - low).y, (high - low).z)
    return {
        "projection": given.get("projection") or "ortho",
        "center": [float(v) for v in center],
        "ortho_scale": float(given.get("ortho_scale") or round(extent * 1.15, 5)),
        "distance": float(given.get("distance") or round(extent * 3.0, 5)),
        "elevation": float(given.get("elevation") if given.get("elevation") is not None else 8.0),
        "lens": float(given.get("lens") or 50.0),
    }


def _place(camera, cam, view):
    center = Vector(cam["center"])
    if view in ("top", "bottom"):
        sign = 1.0 if view == "top" else -1.0
        camera.location = center + Vector((0.0, -0.001, sign * cam["distance"]))
    else:
        dx, dy = DIRECTIONS[view]
        elev = math.radians(cam["elevation"])
        flat = math.cos(elev) * cam["distance"]
        camera.location = center + Vector((dx * flat, dy * flat, math.sin(elev) * cam["distance"]))
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()


def _setup_render(spec, cam):
    scene = bpy.context.scene
    scene.render.resolution_x = scene.render.resolution_y = int(spec.get("size") or 640)
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    if scene.world is None:
        scene.world = bpy.data.worlds.new("EyesWorld")
    scene.world.color = BACKGROUND
    data = bpy.data.cameras.new("EyesCamera")
    data.type = "ORTHO" if cam["projection"] == "ortho" else "PERSP"
    data.ortho_scale = cam["ortho_scale"]
    data.lens = cam["lens"]
    data.clip_start = cam["distance"] / 1000.0
    data.clip_end = cam["distance"] * 10.0
    camera = bpy.data.objects.new("EyesCamera", data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    return camera


def _engine(spec):
    items = {i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
    if spec.get("engine") == "eevee":
        for ident in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
            if ident in items:
                return ident
    return "BLENDER_WORKBENCH"


def _material_mode(scene, mode, clay):
    engine = scene.render.engine
    if engine == "BLENDER_WORKBENCH":
        shading = scene.display.shading
        shading.light = "STUDIO"
        shading.show_cavity = True
        shading.cavity_type = "BOTH"
        if mode == "neutral":
            shading.color_type = "SINGLE"
            shading.single_color = NEUTRAL
        else:
            has_attr = any(getattr(o.data, "color_attributes", None) and len(o.data.color_attributes)
                           for o in scene.objects if o.type == "MESH")
            has_tex = any(i.source in ("FILE", "PACKED") or i.packed_file for i in bpy.data.images)
            kinds = {i.identifier for i in shading.bl_rna.properties["color_type"].enum_items}
            by_vertex = "ATTRIBUTE" if "ATTRIBUTE" in kinds else "VERTEX"   # renamed between Blender versions
            shading.color_type = by_vertex if has_attr and not has_tex else "TEXTURE"
    else:
        bpy.context.view_layer.material_override = clay if mode == "neutral" else None


def main():
    spec = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
    warnings = []
    _import(spec["source"])
    _scene_root(spec, warnings)
    applied_pose = _pose(spec, warnings)
    bpy.context.view_layer.update()
    low, high = _bounds()
    cam = _camera_spec(spec, low, high)
    scene = bpy.context.scene
    scene.render.engine = _engine(spec)
    camera = _setup_render(spec, cam)
    if scene.render.engine != "BLENDER_WORKBENCH":
        sun = bpy.data.objects.new("EyesSun", bpy.data.lights.new("EyesSun", "SUN"))
        sun.data.energy = 3.0
        sun.rotation_euler = Euler((math.radians(50), 0, math.radians(30)), "XYZ")
        scene.collection.objects.link(sun)
    facts = _facts(low, high)
    if "color" in spec["materials"] and not (facts["textures"] or facts["color_attributes"] or facts["materials"]):
        warnings.append("The model has no colours of its own (no textures, vertex colours or materials): "
                        "its colour views look like the neutral ones")
    clay = bpy.data.materials.new("EyesClay")
    clay.diffuse_color = (*NEUTRAL, 1.0)
    views = []
    for material in spec["materials"]:
        _material_mode(scene, material, clay)
        for view in spec["views"]:
            _place(camera, cam, view)
            target = f"{spec['out']}/{view}-{material}.png"
            scene.render.filepath = target
            bpy.ops.render.render(write_still=True)
            views.append({"name": f"{view}-{material}", "label": f"{view.replace('_', ' ')} · {material}",
                          "view": view, "material": material, "path": target})
    resolved = {"views": spec["views"], "material": "both" if len(spec["materials"]) == 2 else spec["materials"][0],
                "camera": cam, "turn": float(spec.get("turn") or 0.0), "up": spec.get("up") or "z",
                "pose": applied_pose, "size": int(spec.get("size") or 640), "engine": spec.get("engine") or "workbench"}
    return {"ok": True, "views": views, "facts": facts, "resolved": resolved, "warnings": warnings}


try:
    RESULT = main()
except Exception as exc:  # noqa: BLE001 - reported to the hub, which turns it into an error for the agent
    RESULT = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:]}
print("EYES_RESULT " + json.dumps(RESULT), flush=True)
