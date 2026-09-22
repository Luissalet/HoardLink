from __future__ import annotations

import json

import pytest

from hoard_link.config import LinkConfig


def write(tmp_path, data, bom=False):
    p = tmp_path / "backend.json"
    raw = json.dumps(data).encode("utf-8")
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)
    return p


def test_backend_json_saved_with_bom_by_notepad_loads(tmp_path):
    p = write(tmp_path, {"capabilities": {"llm": {"url": "http://127.0.0.1:8081"}}}, bom=True)
    assert LinkConfig.load(p, env={}).capability("llm").url == "http://127.0.0.1:8081"


def test_empty_env_var_does_not_wipe_file_value(tmp_path):
    p = write(tmp_path, {"capabilities": {"llm": {"url": "http://127.0.0.1:8081"}}})
    cfg = LinkConfig.load(p, env={"HOARD_LLM_URL": "", "HOARD_LLM_MODEL": "m"})
    assert cfg.capability("llm").url == "http://127.0.0.1:8081"
    assert cfg.capability("llm").model == "m"


def test_env_url_overrides_file_but_keeps_file_api(tmp_path):
    p = write(tmp_path, {"capabilities": {"llm": {"url": "http://a:1", "api": "ollama", "model": "f"}}})
    cfg = LinkConfig.load(p, env={"HOARD_LLM_URL": "http://b:2"})
    cc = cfg.capability("llm")
    assert (cc.url, cc.api, cc.model) == ("http://b:2", "ollama", "f")


def test_env_faustus_and_comfy_beat_file_and_are_normalised(tmp_path):
    p = write(tmp_path, {"faustus": {"url": "http://127.0.0.1:7000/", "token": "file"}, "comfy": {"url": "http://c:1/"}})
    cfg = LinkConfig.load(p, env={"HOARD_FAUSTUS_TOKEN": "ody_env"})
    assert cfg.faustus_urls == ("http://127.0.0.1:7000",)
    assert cfg.faustus_token == "ody_env"
    assert cfg.comfy_url == "http://c:1"
    cfg = LinkConfig.load(p, env={"HOARD_FAUSTUS_URL": " http://127.0.0.1:7001/ ", "HOARD_COMFY_URL": "http://d:2"})
    assert cfg.faustus_urls == ("http://127.0.0.1:7001",)
    assert cfg.comfy_url == "http://d:2"


def test_model_without_url_is_a_preference_not_a_pin():
    cfg = LinkConfig.load(None, env={"HOARD_VISION_MODEL": "qwen2.5vl"})
    assert cfg.capability("vision").model == "qwen2.5vl"
    assert not cfg.capability("vision").explicit


def test_invalid_json_names_the_file(tmp_path):
    p = tmp_path / "backend.json"
    p.write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="backend.json"):
        LinkConfig.load(p, env={})


def test_command_as_string_is_rejected_with_example(tmp_path):
    p = write(tmp_path, {"capabilities": {"tts": {"command": "piper --model x"}}})
    with pytest.raises(ValueError, match="list of strings"):
        LinkConfig.load(p, env={})


def test_null_sections_are_tolerated(tmp_path):
    p = write(tmp_path, {"faustus": None, "comfy": None, "capabilities": {"llm": None}})
    cfg = LinkConfig.load(p, env={})
    assert cfg.faustus_urls == ("http://127.0.0.1:7000", "http://127.0.0.1:7001")
