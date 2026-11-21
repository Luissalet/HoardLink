"""Read the material summary and extrusion tracks in Orca-generated G-code."""

from collections import defaultdict
from math import atan2, dist, hypot, isfinite, pi, tau
from pathlib import Path
import re


_PARAM = re.compile(r"([A-Z])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))")
_TOOL = re.compile(r"T(\d{1,2})$")
_FEATURE = re.compile(r"(?:FEATURE|TYPE):\s*(.*)", re.I)
_SUMMARY = re.compile(r"(?:filament used \[(mm|cm3|g)\]|(filament_diameter|filament_colour|total filament change|total layer number))\s*[:=]\s*(.*)", re.I)
_TIME = re.compile(r"(?:total estimated time:|estimated printing time \(normal mode\)\s*=)\s*([^;]+)", re.I)
_SUPPORT = {"support", "support material", "support interface", "support material interface", "support transition", "support material transition"}
_NON_DEPOSITION = {"custom", "mixed", "none"}
_MODEL_DEPOSITION = {"outer wall", "inner wall", "overhang wall", "sparse infill", "internal solid infill",
                     "top surface", "bottom surface", "bridge", "internal bridge", "gap infill", "ironing"}


def _numbers(raw, divider=","):
    values = []
    for word in raw.split(divider):
        try:
            number = float(word)
            values.append(number if isfinite(number) and number >= 0 else None)
        except ValueError:
            values.append(None)
    return values


def _seconds(label):
    fields = re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", label)
    return round(sum(float(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit] for n, unit in fields)) if fields else None


def _distance(start, end, motion, params):
    if motion in ("G2", "G3") and ("I" in params or "J" in params):
        i, j = params.get("I", 0), params.get("J", 0)
        radius = hypot(i, j)
        angle_in = atan2(-j, -i)
        angle_out = atan2(end[1] - start[1] - j, end[0] - start[0] - i)
        angle = (angle_in - angle_out if motion == "G2" else angle_out - angle_in) % tau
        return hypot(radius * (angle or tau), end[2] - start[2])
    return dist(start, end)


class _Toolpaths:
    def __init__(self):
        self.xyz = (0., 0., 0.)
        self.e = self.feed = 0.
        self.relative_e = self.relative_xyz = False
        self.feature = ""
        self.tool = 0
        self.support = defaultdict(float)
        self.model = defaultdict(float)
        self.duration = defaultdict(float)
        self.has_features = False
        self.changes = self.manual_changes = self.layers = 0
        self.last_deposition_tool = None
        self.retracted = 0.

    def consume(self, command, comment):
        feature = _FEATURE.fullmatch(comment)
        if feature:
            self.feature = feature[1].casefold()
            self.has_features |= self.feature not in _NON_DEPOSITION
        if comment in ("CHANGE_LAYER", "LAYER_CHANGE"):
            self.layers += 1
        if comment.startswith(("COLOR_CHANGE", "MANUAL_TOOL_CHANGE")):
            self.manual_changes += 1
        opcode, _, arguments = command.partition(" ")
        tool = _TOOL.fullmatch(opcode)
        if tool:
            self.tool = int(tool[1])
            return
        if opcode in ("M82", "M83"):
            self.relative_e = opcode == "M83"
        elif opcode in ("G90", "G91"):
            self.relative_xyz = opcode == "G91"
        elif opcode == "G92":
            params = dict((key, float(value)) for key, value in _PARAM.findall(arguments))
            self.e = params.get("E", self.e)
            self.xyz = tuple(params.get(axis, old) for axis, old in zip("XYZ", self.xyz))
        elif opcode in ("G0", "G1", "G2", "G3"):
            self.move(opcode, dict((key, float(value)) for key, value in _PARAM.findall(arguments)))

    def move(self, opcode, params):
        end = tuple(old + params.get(axis, 0.) if self.relative_xyz else params.get(axis, old)
                    for axis, old in zip("XYZ", self.xyz))
        next_e = self.e + params.get("E", 0.) if self.relative_e else params.get("E", self.e)
        extrusion = next_e - self.e
        self.e = next_e
        if self.relative_e:
            self.e = 0.
        start, self.xyz = self.xyz, end
        self.feed = params.get("F", self.feed)
        if not self.feature or self.feature in _NON_DEPOSITION:
            self.retracted = 0.
            return
        net = extrusion - self.retracted
        self.retracted = max(0., -net)
        if net <= 0 or (start[:2] == end[:2] and not ({"I", "J"} & params.keys())):
            return
        if self.last_deposition_tool is not None and self.tool != self.last_deposition_tool:
            self.changes += 1
        self.last_deposition_tool = self.tool
        if self.feature in _SUPPORT:
            self.support[self.tool] += net
        if self.feature in _MODEL_DEPOSITION:
            self.model[self.tool] += net
        if self.feed > 0:
            self.duration[self.tool] += _distance(start, end, opcode, params) * 60. / self.feed


def measure(path: Path) -> dict:
    """Read Orca summaries and estimate per-slot deposition time from motion feed rates."""
    track = _Toolpaths()
    summary = {}
    total_s = None
    version = None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            command, _, comment = raw.strip().partition(";")
            comment = comment.strip()
            if not command:
                field = _SUMMARY.fullmatch(comment)
                if field:
                    summary[(field[1] or field[2]).lower()] = field[3]
                time = _TIME.search(comment)
                if time:
                    total_s = _seconds(time[1])
                if comment.startswith("generated by OrcaSlicer "):
                    version = comment.split()[3]
            track.consume(command.strip(), comment)

    material = {unit: _numbers(summary[unit]) if unit in summary else [] for unit in ("mm", "cm3", "g")}
    diameters = _numbers(summary.get("filament_diameter", ""))
    colours = [re.search(r"#[0-9a-fA-F]{6}\b", item) for item in re.split("[;,]", summary.get("filament_colour", ""))]
    deposited = track.duration.keys() | track.support.keys() | track.model.keys()
    slots = deposited | set(range(max(map(len, material.values()))))
    rows = []
    for slot in sorted(slots):
        amounts = {key: values[slot] if slot < len(values) else None for key, values in material.items()}
        if not any(amounts.values()) and slot not in deposited:
            continue
        diameter = diameters[slot] if slot < len(diameters) else None
        length = track.support.get(slot, 0.)
        support = round(length * pi * (diameter / 2) ** 2, 2) if length and diameter else (None if length else 0.)
        colour = colours[slot] if slot < len(colours) else None
        rows.append({"slot": slot + 1, "colour": colour[0] if colour else None,
                     "material_mm": amounts["mm"], "material_cm3": amounts["cm3"], "material_g": amounts["g"],
                     "model_deposition": slot in track.model, "model_filament_mm": round(track.model.get(slot, 0.), 3),
                     "deposition_seconds": round(track.duration[slot], 1) if slot in track.duration else None,
                     "support_mm3": support if track.has_features else None})
    volumes = [row["support_mm3"] for row in rows]
    changes = summary.get("total filament change", "")
    layers = summary.get("total layer number", "")
    return {"version": version, "layers": int(layers) if layers.isdecimal() else track.layers or None,
            "estimated_seconds": total_s, "support_mm3": round(sum(volumes), 2) if volumes and None not in volumes else None,
            "filament_changes": (int(changes) if changes.isdecimal() else track.changes) + track.manual_changes
            if track.last_deposition_tool is not None or changes.isdecimal() else None,
            "filaments": rows}
