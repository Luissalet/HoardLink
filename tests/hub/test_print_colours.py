import json
from zipfile import ZipFile

from hoard_link.hub import print_colours


MAIN = "3D/3dmodel.model"
CORE = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
EXPECTED = [
    {"slot": 1, "colour": "#26213F", "faces": 1},
    {"slot": 3, "colour": "#EFC66B", "faces": 2},
]


def test_reads_painted_slots_without_touching_registered_archive(tmp_path):
    path = tmp_path / "original.3mf"
    main = f"""<model {CORE} xmlns:nikias="urn:nikias-hoard:print-colour:1">
        <metadata name="Application">Nikias's Hoard</metadata>
        <metadata name="nikias:PaintedSlots">{json.dumps(EXPECTED)}</metadata>
        <resources/>
    </model>"""
    with ZipFile(path, "w") as archive:
        archive.writestr(MAIN, main)
    before = path.read_bytes()
    assert print_colours.read(path) == {"dialect": "painted", "expected": EXPECTED}
    assert path.read_bytes() == before


def test_legacy_export_uses_actual_triangle_colours_and_reports_substitutions(tmp_path):
    path = tmp_path / "reference.3mf"
    main = f"""<model {CORE} xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">
        <metadata name="Application">Nikias's Hoard</metadata>
        <resources>
            <m:colorgroup id="1"><m:color color="#26213FFF"/><m:color color="#FFFFFFFF"/><m:color color="#EFC66BFF"/></m:colorgroup>
            <object id="2" pid="1" pindex="0"><mesh><triangles>
                <triangle p1="0"/><triangle p1="2"/><triangle p1="2"/>
            </triangles></mesh></object>
        </resources>
        <build><item objectid="2"/></build>
    </model>"""
    with ZipFile(path, "w") as archive:
        archive.writestr(MAIN, main)
    check = print_colours.read(path)
    assert check == {"dialect": "standard", "expected": EXPECTED}
    report = print_colours.compare(check, [
        {"slot": 1, "colour": "#26213f", "model_deposition": True},
        {"slot": 2, "colour": "#efc66b", "model_deposition": True},
        {"slot": 3, "colour": "#efc66b", "model_deposition": False},
    ])
    assert report["differences"] == [
        {"slot": 2, "expected": None, "actual": "#EFC66B"},
        {"slot": 3, "expected": "#EFC66B", "actual": None},
    ]
    with ZipFile(path, "w") as archive:
        archive.writestr(MAIN, main.replace("Nikias's Hoard", "Another generator"))
    assert print_colours.read(path) is None
