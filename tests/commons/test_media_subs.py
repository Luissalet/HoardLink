"""hoard_link.media.subs and the Node twin (js/hoard-commons/media.js) agree on tests/vectors/media_subs.json."""

from __future__ import annotations

import dataclasses

import pytest

from hoard_link.media import subs
from tests.commons.jsrun import load_vectors, python_call, run_js

CASES = load_vectors("media_subs")


def wire(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return wire(dataclasses.asdict(value))
    if isinstance(value, (list, tuple)):
        return [wire(v) for v in value]
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items()}
    return value


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['fn']}:{str(c['args'])[:28]}:{sorted(c.get('opts') or {})}")
def test_python(case):
    assert wire(python_call(subs, case)) == case["expect"]


def test_node_twin():
    got = run_js("media.js", CASES)
    bad = [(c["fn"], c["args"], g, c["expect"]) for c, g in zip(CASES, got) if g != c["expect"]]
    assert not bad, bad[:3]


def test_millisecond_rounding_never_gives_1000():
    for t in (59.9996, 0.9996, 3599.9999, 7.99951, 1.9995):
        assert ",1000" not in subs.srt_time(t) and ".1000" not in subs.vtt_time(t)
    assert subs.srt_time(59.9996) == "00:01:00,000"


def test_cues_accept_objects_and_dicts():
    objs = [subs.Cue(0, 1, "a"), subs.Cue(1, 2, "b", speaker="X")]
    dicts = [{"start_s": 0, "end_s": 1, "text": "a"}, {"start_s": 1, "end_s": 2, "text": "b", "speaker": "X"}]
    assert subs.to_srt(objs, speakers=True) == subs.to_srt(dicts, speakers=True)


def test_cues_from_whisper_segments():
    cues = subs.cues_from_segments([{"start_s": 0.0, "end_s": 1.0, "text": "hi"}, {"start_s": 1.0, "end_s": 2.0, "text": "there"}])
    assert [(c.start_s, c.end_s, c.text) for c in cues] == [(0.0, 1.0, "hi"), (1.0, 2.0, "there")]


def test_srt_round_trip():
    cues = [subs.Cue(1.5, 3.25, "one"), subs.Cue(4, 5.5, "two\nlines")]
    back = subs.parse_srt(subs.to_srt(cues))
    assert [(c.start_s, c.end_s, c.text) for c in back] == [(1.5, 3.25, "one"), (4.0, 5.5, "two\nlines")]


def test_ass_color_rejects_garbage():
    for bad in ("bogus", "#12345678", "", "#gg0000"):
        with pytest.raises(ValueError):
            subs.ass_color(bad)


def test_ass_color_rejects_garbage_node():
    got = run_js("media.js", [{"fn": "ass_color", "args": ["bogus"]}])
    assert "__error__" in got[0]


def test_untimed_text_and_crlf():
    cues = subs.parse_vtt("WEBVTT\r\n\r\n00:00:01.000 --> 00:00:02.000\r\nHello\r\n")
    assert [(c.start_s, c.text) for c in cues] == [(1.0, "Hello")]
    assert subs.subtitle_text("") == ""
