"""Inspect the actual cells of an XLSX table and compare its declared schema."""

from collections import Counter
from datetime import timedelta
import hashlib
import re
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile


def dataset_requirements(hub, requirements):
    """Resolve a pinned data version over the same app tool agents use."""
    spec = requirements["dataset"]
    if (not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not spec["name"].strip()
            or type(spec.get("version")) is not int or spec["version"] < 0):
        raise ValueError("Supply dataset.name and a non-negative dataset.version.")
    response = hub.call_app("nightingale", "data_preview",
                            {"dataset": spec["name"], "version": spec["version"], "limit": 1},
                            caller="hub", reason="Compare an XLSX with its source dataset", timeout=30)
    result = response.get("result")
    if not response.get("ok") or not isinstance(result, dict) or result.get("version") != spec["version"]:
        return {**requirements, "_dataset_error": str(response.get("error") or "Dataset version unavailable")}
    return {**requirements, "columns": result.get("columns"), "row_count": result.get("row_count_total"),
            "_dataset": {"name": result.get("dataset") or spec["name"], "version": result["version"]}}


def _expected_type(value):
    name = value.upper().split("(", 1)[0].strip()
    if name.endswith("[]") or name in {"ARRAY", "LIST", "STRUCT", "MAP", "UNION", "JSON", "UUID", "ENUM"}:
        return "text"
    if name in {"VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR"}:
        return "text"
    if re.fullmatch(r"U?(TINY|SMALL|BIG|HUGE)?INT(EGER)?", name):
        return "integer"
    if name in {"NUMBER", "DECIMAL", "NUMERIC", "DOUBLE", "DOUBLE PRECISION", "FLOAT", "REAL"}:
        return "number"
    if name in {"BOOL", "BOOLEAN"}:
        return "boolean"
    if name.startswith(("DATETIME", "TIMESTAMP")):
        return "datetime"
    if name.startswith("TIME"):
        return "time"
    if name == "DATE":
        return "date"
    if name in {"INTERVAL", "DURATION"}:
        return "duration"
    return None


def _cell_type(cell, date_format):
    if cell.data_type in {"s", "str", "inlineStr"}:
        return "text"
    if cell.data_type == "n":
        return "integer" if isinstance(cell.value, int) else "number"
    if cell.data_type == "d":
        return "duration" if isinstance(cell.value, timedelta) else date_format(cell.number_format) or "datetime"
    return {"b": "boolean", "f": "formula", "e": "error"}.get(cell.data_type, cell.data_type)


def check_sheet(path, requirements, report):
    from openpyxl import load_workbook
    from openpyxl.styles.numbers import is_datetime

    try:
        with path.open("rb") as stream:
            workbook = load_workbook(stream, read_only=True, data_only=False, keep_links=False)
            try:
                _inspect(workbook, requirements, report, is_datetime)
            finally:
                workbook.close()
    except (BadZipFile, ParseError) as exc:
        raise ValueError(f"Unreadable XLSX: {exc}") from exc
    if "sha256" in requirements:
        with path.open("rb") as stream:
            measured = hashlib.file_digest(stream, "sha256").hexdigest()
        expected = requirements["sha256"]
        report.add("sha256", measured == expected, "Export fingerprint matches." if measured == expected else
                   "Export fingerprint differs.", "Coincide la huella de la exportación." if measured == expected else
                   "La huella de la exportación es distinta.", measured=measured, expected=expected)


def _inspect(workbook, requirements, report, date_format):
    sheet = requirements.get("sheet") or workbook.active.title
    names = [worksheet.title for worksheet in workbook.worksheets]
    if sheet not in names:
        report.add("sheet", False, "The requested worksheet is missing.", "Falta la hoja solicitada.",
                   measured=names, expected=sheet)
        return

    rows = workbook[sheet].iter_rows()
    header = [cell.value for cell in next(rows, ())]
    valid_header = (bool(header) and all(isinstance(value, str) and value.strip() for value in header)
                    and len(set(header)) == len(header))
    report.add("headers", valid_header, "Headers are present and unique." if valid_header else
               "Headers must be non-empty and unique.", "Las cabeceras están presentes y son únicas." if valid_header else
               "Las cabeceras deben tener texto y ser únicas.", measured=header if valid_header else list(map(str, header)))

    types = [Counter() for _ in header]
    row_count = 0
    for row_count, cells in enumerate(rows, start=1):
        for counts, cell in zip(types, cells):
            if cell.value is not None:
                counts[_cell_type(cell, date_format)] += 1
    report.measures.update(sheet=sheet, sheets=names, row_count=row_count,
                           columns=[{"name": str(name), "types": dict(counts)} for name, counts in zip(header, types)])
    if "_dataset_error" in requirements:
        report.add("dataset", None, "The source dataset could not be read.",
                   "No se ha podido leer el conjunto de origen.", detail=requirements["_dataset_error"])
    if "_dataset" in requirements:
        report.measures["dataset"] = requirements["_dataset"]
    if "row_count" in requirements:
        expected = requirements["row_count"]
        match = type(expected) is int and expected >= 0 and row_count == expected
        report.add("row_count", match, "Row count matches." if match else "Row count differs.",
                   "Coincide el número de filas." if match else "El número de filas es distinto.",
                   measured=row_count, expected=expected)
    if "columns" in requirements:
        _check_columns(report, header, types, requirements["columns"])


def _check_columns(report, header, types, columns):
    if not isinstance(columns, list) or not columns or any(
        not isinstance(col, dict) or not isinstance(col.get("name"), str)
        or not isinstance(col.get("type"), str) for col in columns
    ):
        raise ValueError("Columns must be a list of {name, type} objects.")
    names = [col["name"] for col in columns]
    match = header == names
    report.add("columns", match, "Column names and order match." if match else "Column names or order differ.",
               "Coinciden los nombres y el orden de las columnas." if match else
               "Los nombres o el orden de las columnas son distintos.", measured=list(map(str, header)), expected=names)
    if not match:
        return
    for col, counts in zip(columns, types):
        expected = _expected_type(col["type"])
        allowed = {"integer", "number"} if expected == "number" else {expected}
        compatible = None if not expected or not counts else counts.keys() <= allowed
        messages = {
            True: ("Column type matches.", "Coincide el tipo de la columna."),
            False: ("Column contains cells of a different type.", "La columna contiene celdas de un tipo distinto."),
            None: ("Column type could not be determined.", "No se ha podido determinar el tipo de la columna."),
        }
        report.add(f"type:{col['name']}", compatible, *messages[compatible], measured=dict(counts), expected=col["type"])
