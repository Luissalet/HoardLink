"""One complete paginated chapter handoff without real account writes."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest


SPEC = importlib.util.spec_from_file_location(
    "story_to_writer", Path(__file__).resolve().parents[1] / "scripts" / "story_to_writer.py"
)
workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow)


def test_paginated_export_then_idempotent_import():
    seen = []

    def fake_post(url, body, token=""):
        seen.append((url, body, token))
        if url.endswith("session_export"):
            offset = body["offset"]
            return ({"kind": "chapter", "offset": 0, "text": "# La sal\n", "truncated": True,
                     "next_offset": 9, "total_chars": 14} if offset == 0 else
                    {"kind": "chapter", "offset": 9, "text": "Final", "truncated": False,
                     "next_offset": None, "total_chars": 14})
        return {"ok": True, "result": {"state": "created", "id": "writing_1", "title": "La sal"}}

    with patch.object(workflow, "post_json", side_effect=fake_post):
        chapter = workflow.export_chapter("http://127.0.0.1:8816", "w1", "s1")
        result = workflow.import_chapter("http://127.0.0.1:8766", "secret", project="p1",
                                         world="w1", session="s1", title="La sal", content=chapter, refresh=False)
    assert chapter == "# La sal\nFinal"
    assert result["state"] == "created"
    assert seen[-1][1]["args"]["content"] == chapter
    assert seen[-1][2] == "secret"


def test_export_refuses_gap_before_writer_call():
    pages = iter([
        {"kind": "chapter", "offset": 0, "text": "a", "truncated": True, "next_offset": 2, "total_chars": 2},
    ])
    with patch.object(workflow, "post_json", side_effect=lambda *_args, **_kwargs: next(pages)):
        with pytest.raises(RuntimeError, match="Paginación incompleta"):
            workflow.export_chapter("http://127.0.0.1:8816", "w", "s")


def test_external_urls_are_rejected():
    with pytest.raises(ValueError):
        workflow.loopback_url("https://example.com")
