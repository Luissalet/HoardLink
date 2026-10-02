"""hoard_link.ics and js/hoard-commons/ics.js agree on tests/vectors/ics.json; folding, escaping and round trips."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from hoard_link import ics
from tests.commons.commerce_util import load_vectors, mismatches, run_js, run_python

CASES = load_vectors("ics")
NOW = "2026-10-02T09:30:00Z"


def test_python_vectors():
    assert not mismatches(CASES, run_python(ics, CASES))


def test_node_twin():
    assert not mismatches(CASES, run_js("ics.js", CASES))


@pytest.mark.parametrize("text", ["x" * 200, "é" * 90, "日本語" * 40, "a😀" * 50, "SUMMARY:" + "ñ" * 74, "x" * 75, "x" * 76])
def test_fold_line_limits_and_unfolds_back(text):
    folded = ics.fold_line(text)
    pieces = folded.split("\r\n")
    assert all(len(p.encode("utf-8")) <= 75 for p in pieces)
    assert all(p.startswith(" ") for p in pieces[1:])
    assert "".join(pieces[:1] + [p[1:] for p in pieces[1:]]) == text      # no character split in two


def test_build_is_crlf_folded_and_ends_with_crlf():
    out = ics.build_ics([{"uid": "u", "title": "é" * 200, "start": "2026-10-05"}], now=NOW)
    assert out.endswith("END:VCALENDAR\r\n") and "\n" not in out.replace("\r\n", "")
    assert all(len(line.encode("utf-8")) <= 75 for line in out.split("\r\n"))


def test_dtstamp_defaults_to_the_current_time():
    out = ics.build_ics([{"uid": "u", "title": "x", "start": "2026-10-05"}])
    stamp = re.search(r"DTSTAMP:(\d{8}T\d{6}Z)", out).group(1)
    got = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    assert abs(datetime.now(timezone.utc) - got) < timedelta(minutes=2)


def test_python_date_and_datetime_inputs():
    out = ics.build_ics([
        {"uid": "a", "title": "d", "start": date(2026, 10, 5), "end": date(2026, 10, 6)},
        {"uid": "b", "title": "naive", "start": datetime(2026, 10, 5, 10, 0), "end": datetime(2026, 10, 5, 11, 0), "tz": "Europe/Madrid"},
        {"uid": "c", "title": "aware", "start": datetime(2026, 10, 5, 12, 0, tzinfo=timezone(timedelta(hours=2)))},
    ], now=datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc))
    assert "DTSTART;VALUE=DATE:20261005\r\nDTEND;VALUE=DATE:20261007" in out
    assert "DTSTART;TZID=Europe/Madrid:20261005T100000\r\nDTEND;TZID=Europe/Madrid:20261005T110000" in out
    assert "DTSTART:20261005T100000Z" in out and "DTSTAMP:20261002T093000Z" in out


def test_uid_fallback_is_deterministic():
    ev = {"title": "Same", "start": "2026-10-05T21:00:00Z", "location": "Here"}
    a = ics.build_ics([ev], now=NOW)
    b = ics.build_ics([dict(ev)], now="2027-01-01")
    uid = lambda s: re.search(r"UID:(\S+)", s).group(1)
    assert uid(a) == uid(b) and uid(a).endswith("@hoard")
    assert uid(ics.build_ics([{**ev, "location": "There"}], now=NOW)) != uid(a)


def test_round_trip_build_then_parse():
    events = [
        {"uid": "r1@x", "title": "Cena, con Ana; y más", "description": "Línea 1\nLínea 2", "location": "Calle Mayor 1", "start": "2026-10-05T21:00:00",
         "end": "2026-10-05T23:30:00", "tz": "Europe/Madrid", "alarms": [15, 1440], "categories": ["Familia", "Fam,ily"], "status": "confirmed", "url": "https://x.test/e?a=1&b=2"},
        {"uid": "r2@x", "title": "Viaje", "start": "2026-10-10", "end": "2026-10-12", "rrule": "FREQ=YEARLY"},
        {"uid": "r3@x", "title": "Call", "start": "2026-10-05T10:00:00+02:00", "end": "2026-10-05T11:00:00+02:00"},
    ]
    parsed = ics.parse_ics(ics.build_ics(events, now=NOW, name="Cal"))
    assert [p["uid"] for p in parsed] == ["r1@x", "r2@x", "r3@x"]
    a, b, c = parsed
    assert (a["summary"], a["description"], a["start"], a["end"], a["tzid"]) == ("Cena, con Ana; y más", "Línea 1\nLínea 2", "2026-10-05T21:00:00", "2026-10-05T23:30:00", "Europe/Madrid")
    assert a["alarms"] == [15, 1440] and a["categories"] == ["Familia", "Fam,ily"] and a["status"] == "CONFIRMED" and a["url"] == "https://x.test/e?a=1&b=2"
    assert (b["all_day"], b["start"], b["end"], b["end_exclusive"], b["rrule"]) == (True, "2026-10-10", "2026-10-13", True, "FREQ=YEARLY")
    assert (c["start"], c["end"]) == ("2026-10-05T08:00:00Z", "2026-10-05T09:00:00Z")
    # what parse returns can be fed back to build and gives the same events
    again = ics.parse_ics(ics.build_ics(parsed, now=NOW))
    assert [{k: v for k, v in p.items() if k != "tzid"} for p in again] == [{k: v for k, v in p.items() if k != "tzid"} for p in parsed]
    assert again[0]["tzid"] == "Europe/Madrid"


def test_escape_matches_the_old_faustus_export():
    # Faustus _ics_escape: backslash, semicolon, comma and newline
    assert ics.ics_escape("Work,Home") == "Work\\,Home"
    assert ics.ics_escape("a;b") == "a\\;b"
    assert ics.ics_escape("one\ntwo") == "one\\ntwo"
    assert ics.ics_unescape(ics.ics_escape("a,b;c\\d\ne")) == "a,b;c\\d\ne"


def test_parse_is_lenient():
    assert ics.parse_ics(None) == []
    ev = ics.parse_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:No dates\nEND:VEVENT\nEND:VCALENDAR")
    assert ev[0]["start"] == "" and ev[0]["summary"] == "No dates" and ev[0]["alarms"] == []
