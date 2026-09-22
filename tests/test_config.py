from __future__ import annotations

import json
from pathlib import Path

from hoard_link.config import LinkConfig


def test_defaults_with_no_file_and_no_env():
    cfg = LinkConfig.load(None, env={}, app="test")
    assert cfg.only_resident is True
    assert cfg.faustus_urls == ("http://127.0.0.1:7000", "http://127.0.0.1:7001")
    assert cfg.faustus_token is None
    assert cfg.comfy_url is None
    assert cfg.capability("llm").explicit is False


def test_loads_backend_json(tmp_path: Path):
    backend = {
        "only_resident": False,
        "faustus": {"url": "http://127.0.0.1:7005", "token": "ody_abc"},
        "comfy": {"url": "http://127.0.0.1:8199"},
        "capabilities": {
            "llm": {"url": "http://127.0.0.1:8081/v1/chat/completions", "model": "m1", "api": "openai"},
            "tts": {"command": ["piper", "--text", "{text}", "--output_file", "{out}"]},
        },
    }
    path = tmp_path / "backend.json"
    path.write_text(json.dumps(backend), encoding="utf-8")

    cfg = LinkConfig.load(path, env={}, app="test")
    assert cfg.only_resident is False
    assert cfg.faustus_urls == ("http://127.0.0.1:7005",)
    assert cfg.faustus_token == "ody_abc"
    assert cfg.comfy_url == "http://127.0.0.1:8199"

    llm = cfg.capability("llm")
    assert llm.explicit is True
    assert llm.url == "http://127.0.0.1:8081/v1/chat/completions"
    assert llm.model == "m1"
    assert llm.api == "openai"

    tts = cfg.capability("tts")
    assert tts.explicit is True
    assert tts.command == ["piper", "--text", "{text}", "--output_file", "{out}"]


def test_missing_backend_json_path_is_ignored(tmp_path: Path):
    cfg = LinkConfig.load(tmp_path / "does-not-exist.json", env={}, app="test")
    assert cfg.capability("llm").explicit is False


def test_env_overrides_capability_url_and_model():
    env = {"HOARD_LLM_URL": "http://127.0.0.1:9999/v1/chat/completions", "HOARD_LLM_MODEL": "envmodel"}
    cfg = LinkConfig.load(None, env=env, app="test")
    llm = cfg.capability("llm")
    assert llm.explicit is True
    assert llm.url == "http://127.0.0.1:9999/v1/chat/completions"
    assert llm.model == "envmodel"


def test_env_overrides_faustus_and_comfy():
    env = {
        "HOARD_FAUSTUS_URL": "http://127.0.0.1:7777",
        "HOARD_FAUSTUS_TOKEN": "ody_env",
        "HOARD_COMFY_URL": "http://127.0.0.1:8123",
    }
    cfg = LinkConfig.load(None, env=env, app="test")
    assert cfg.faustus_urls == ("http://127.0.0.1:7777",)
    assert cfg.faustus_token == "ody_env"
    assert cfg.comfy_url == "http://127.0.0.1:8123"


def test_env_overrides_take_priority_over_file(tmp_path: Path):
    backend = {"capabilities": {"llm": {"url": "http://127.0.0.1:8081/v1/chat/completions", "model": "filemodel"}}}
    path = tmp_path / "backend.json"
    path.write_text(json.dumps(backend), encoding="utf-8")
    env = {"HOARD_LLM_MODEL": "envmodel"}
    cfg = LinkConfig.load(path, env=env, app="test")
    llm = cfg.capability("llm")
    assert llm.url == "http://127.0.0.1:8081/v1/chat/completions"
    assert llm.model == "envmodel"
