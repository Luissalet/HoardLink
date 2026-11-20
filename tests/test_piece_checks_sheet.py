from datetime import date
import hashlib
from types import SimpleNamespace

import openpyxl

from hoard_link.hub.piece_checks import PieceChecksFacet, Report
from hoard_link.hub.piece_checks_sheet import check_sheet


def _workbook(path, *, seats=8):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "data"
    sheet.append(["venue", "cost", "seats", "opens"])
    sheet.append(["Pier", 90.25, seats, date(2027, 1, 2)])
    sheet.append(["Dock", 30, 4, None])
    book.save(path)
    book.close()
    return [{"name": name, "type": datatype} for name, datatype in
            zip(("venue", "cost", "seats", "opens"), ("VARCHAR", "DECIMAL(8,2)", "BIGINT", "DATE"))]


def test_sheet_matches_pinned_nightingale_schema(tmp_path):
    xlsx = tmp_path / "venues.xlsx"
    columns = _workbook(xlsx)

    def preview(app, tool, args, **kwargs):
        assert (app, tool) == ("nightingale", "data_preview")
        assert args == {"dataset": "venues", "version": 4, "limit": 1}
        return {"ok": True, "result": {"dataset": "venues", "version": 4, "row_count_total": 2, "columns": columns}}

    sha256 = hashlib.sha256(xlsx.read_bytes()).hexdigest()
    checked = PieceChecksFacet(SimpleNamespace(call_app=preview)).validate({
        "path": str(xlsx), "requirements": {"dataset": {"name": "venues", "version": 4}, "sha256": sha256},
    })
    assert checked["profile"] == "spreadsheet" and checked["verdict"] == "pass", checked
    assert checked["measures"]["dataset"] == {"name": "venues", "version": 4}
    assert checked["measures"]["row_count"] == 2
    assert checked["measures"]["columns"][1]["types"] == {"integer": 1, "number": 1}


def test_sheet_fails_wrong_row_count_and_mixed_cell_types(tmp_path):
    xlsx = tmp_path / "venues.xlsx"
    columns = _workbook(xlsx, seats="eight")
    report = Report()
    check_sheet(xlsx, {"columns": columns, "row_count": 3, "sha256": "0" * 64}, report)
    assert report.payload()["verdict"] == "fail"
    failed = {item["id"]: item for item in report.checks if item["verdict"] == "fail"}
    assert set(failed) == {"row_count", "type:seats", "sha256"}
    assert failed["type:seats"]["measured"] == {"text": 1, "integer": 1}
