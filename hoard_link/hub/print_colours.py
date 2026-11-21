"""Check the colours carried by Nikias print exports against model extrusion."""

from collections import Counter
import json
import re
from xml.etree import ElementTree as ET
import zipfile


_CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_MATERIAL = "{http://schemas.microsoft.com/3dmanufacturing/material/2015/02}"
_NAMESPACE = "urn:nikias-hoard:print-colour:1"
_MAIN = "3D/3dmodel.model"
_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")
_GENERATOR = "Nikias's Hoard"


def _painted_slots(text):
    rows = json.loads(text)
    if not isinstance(rows, list) or not rows or any(
        not isinstance(row, dict)
        or type(row.get("slot")) is not int or not 1 <= row["slot"] <= 16
        or not isinstance(row.get("colour"), str) or not _COLOUR.fullmatch(row["colour"])
        or type(row.get("faces")) is not int or row["faces"] < 1
        for row in rows
    ) or len({row["slot"] for row in rows}) != len(rows):
        raise ValueError("Invalid painted slots in the Nikias 3MF")
    return sorted(rows, key=lambda row: row["slot"])


def _inspect(stream):
    """Read the painted slot record or the legacy single colour-group export."""
    generator = None
    painted = None
    namespace = False
    colours = []
    counts = Counter()
    parent = None
    for event, element in ET.iterparse(stream, events=("start", "end", "start-ns")):
        if event == "start-ns":
            namespace |= element == ("nikias", _NAMESPACE)
            continue
        tag = element.tag
        if event == "start":
            if tag == _CORE + "resources":
                if painted is not None and namespace:
                    return "painted", _painted_slots(painted)
                if generator != _GENERATOR:
                    return None, []
            elif tag in (_CORE + "vertices", _CORE + "triangles"):
                parent = element
            continue
        if tag == _CORE + "metadata":
            name = element.get("name")
            if name == "Application":
                generator = element.text
            elif name == "nikias:PaintedSlots":
                painted = element.text or ""
        elif tag == _MATERIAL + "color":
            colours.append(element.get("color", "")[:7])
        elif tag == _CORE + "triangle":
            counts[element.get("p1", "0")] += 1
        elif element is parent:
            parent = None
        element.clear()
        if parent is not None and len(parent) >= 4096:
            parent.clear()
    rows = [{"slot": index + 1, "colour": colour.upper(), "faces": counts[str(index)]}
            for index, colour in enumerate(colours) if counts[str(index)] and _COLOUR.fullmatch(colour)]
    return ("standard" if rows else None), rows


def read(path):
    """Read the slots from a pinned Nikias print export or its standard-colour counterpart."""
    try:
        with zipfile.ZipFile(path) as package, package.open(_MAIN) as stream:
            dialect, slots = _inspect(stream)
    except (KeyError, ET.ParseError, zipfile.BadZipFile):
        # The slicer reports unreadable models through its existing diagnostics.
        return None
    return {"dialect": dialect, "expected": slots} if slots else None


def compare(check, filaments):
    """Compare exact slot and RGB using model deposition, without counting purge or support."""
    expected = {row["slot"]: row["colour"].upper() for row in check["expected"]}
    actual = {row["slot"]: row["colour"].upper() if row["colour"] else None
              for row in filaments if row["model_deposition"]}
    differences = [{"slot": slot, "expected": expected.get(slot), "actual": actual.get(slot)}
                   for slot in sorted(expected.keys() | actual.keys())
                   if slot not in actual or slot not in expected or expected[slot] != actual[slot]]
    return {**check, "actual": [{"slot": slot, "colour": colour} for slot, colour in sorted(actual.items())],
            "differences": differences}
