"""hoard_link.appconfig: environment readers, .env loading, the data-folder layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from hoard_link import appconfig as ac


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("HL_A", "HL_B", "HL_C", "KAFKA_DATA_DIR", "DOT_A", "DOT_B", "DOT_C", "DOT_D", "DOT_E", "DOT_F", "DOT_G"):
        monkeypatch.delenv(name, raising=False)


def test_env_str(monkeypatch):
    assert ac.env_str("HL_A") is None and ac.env_str("HL_A", default="x") == "x"
    monkeypatch.setenv("HL_B", "  value  ")
    assert ac.env_str("HL_A", "HL_B", default="x") == "value"
    monkeypatch.setenv("HL_A", "   ")                            # blank is the same as unset
    assert ac.env_str("HL_A", "HL_B") == "value"
    monkeypatch.setenv("HL_A", "first")
    assert ac.env_str("HL_A", "HL_B") == "first"


@pytest.mark.parametrize("raw,expect", [("5180", 5180), (" 42 ", 42), ("-3", -3), ("7.0", 7), ("7.5", 99), ("abc", 99), ("", 99), ("1e3", 1000)])
def test_env_int(monkeypatch, raw, expect):
    monkeypatch.setenv("HL_A", raw)
    assert ac.env_int("HL_A", default=99) == expect


def test_env_int_names_clamp_and_skip_unparsable(monkeypatch):
    monkeypatch.setenv("HL_A", "oops")
    monkeypatch.setenv("HL_B", "8000")
    assert ac.env_int("HL_A", "HL_B", default=1) == 8000          # the first parsable one wins
    assert ac.env_int("HL_B", minimum=1, maximum=100) == 100
    assert ac.env_int("HL_B", minimum=9000) == 9000
    assert ac.env_int("HL_C") is None


@pytest.mark.parametrize("raw,expect", [("1.5", 1.5), ("2", 2.0), ("-0.25", -0.25), ("nan", 9.0), ("x", 9.0), ("", 9.0)])
def test_env_float(monkeypatch, raw, expect):
    monkeypatch.setenv("HL_A", raw)
    assert ac.env_float("HL_A", default=9.0) == expect


def test_env_float_clamp(monkeypatch):
    monkeypatch.setenv("HL_A", "50")
    assert ac.env_float("HL_A", maximum=10.5) == 10.5 and ac.env_float("HL_A", minimum=60) == 60


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "No", "off", " Off "])
def test_env_flag_false_words(monkeypatch, raw):
    monkeypatch.setenv("HL_A", raw)
    assert ac.env_flag("HL_A", True) is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", " ON "])
def test_env_flag_true_words(monkeypatch, raw):
    monkeypatch.setenv("HL_A", raw)
    assert ac.env_flag("HL_A", False) is True


@pytest.mark.parametrize("raw", [None, "", "  ", "maybe", "2"])
def test_env_flag_falls_back_to_default(monkeypatch, raw):
    if raw is not None:
        monkeypatch.setenv("HL_A", raw)
    assert ac.env_flag("HL_A") is False
    assert ac.env_flag("HL_A", True) is True
    assert ac.env_flag("HL_A", default=True) is True


def test_load_dotenv_parses_and_never_overrides(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text(
        "﻿# a comment\n"
        "DOT_A=plain\n"
        "  DOT_B = \"quoted value\"  \n"
        "DOT_C='single quoted # not a comment'\n"
        "export DOT_D=exported\n"
        "DOT_E=value # trailing comment\n"
        "no equals sign here\n"
        "\n"
        "=novalue\n"
        "DOT_F=\n"
        "DOT_G=a=b=c\n",
        encoding="utf-8")
    monkeypatch.setenv("DOT_A", "from the real environment")
    values = ac.load_dotenv(f)
    assert values == {"DOT_A": "plain", "DOT_B": "quoted value", "DOT_C": "single quoted # not a comment", "DOT_D": "exported",
                      "DOT_E": "value", "DOT_F": "", "DOT_G": "a=b=c"}
    import os
    assert os.environ["DOT_A"] == "from the real environment"          # the real environment wins
    assert os.environ["DOT_B"] == "quoted value" and os.environ["DOT_D"] == "exported" and os.environ["DOT_G"] == "a=b=c"
    for name in ("DOT_B", "DOT_C", "DOT_D", "DOT_E", "DOT_F", "DOT_G"):
        monkeypatch.delenv(name)


def test_load_dotenv_missing_file_and_default_path(tmp_path, monkeypatch):
    assert ac.load_dotenv(tmp_path / "nope.env") == {}
    (tmp_path / ".env").write_text("DOT_A=cwd\n")
    monkeypatch.chdir(tmp_path)
    assert ac.load_dotenv() == {"DOT_A": "cwd"}
    monkeypatch.delenv("DOT_A")
    (tmp_path / "bad.env").write_bytes(b"\xff\xfe\x00bad")
    assert ac.load_dotenv(tmp_path / "bad.env") == {}


def test_resolve_paths_default_layout(tmp_path):
    p = ac.resolve_paths("kafka", tmp_path, env_prefix="KAFKA")
    assert p.data_dir == tmp_path / "data" and p.configured is False and p.root == tmp_path
    assert p.db_path == tmp_path / "data" / "kafka.db"
    assert p.token_path.name == "mcp-token" and p.url_path.name == "url" and p.backend_json_path.name == "backend.json"
    assert p.logs_dir == tmp_path / "data" / "logs" and p.cache_dir == tmp_path / "data" / "cache"
    assert not (tmp_path / "data").exists()                              # nothing is created by resolving


def test_resolve_paths_honours_the_env_prefix(tmp_path, monkeypatch):
    custom = tmp_path / "elsewhere"
    monkeypatch.setenv("KAFKA_DATA_DIR", str(custom))
    p = ac.resolve_paths("kafka", tmp_path / "repo", env_prefix="kafka")
    assert p.data_dir == custom and p.configured is True and p.db_path == custom / "kafka.db"
    monkeypatch.setenv("KAFKA_DATA_DIR", "~/hl-data")
    assert ac.resolve_paths("kafka", tmp_path, env_prefix="KAFKA").data_dir == Path.home() / "hl-data"
    monkeypatch.setenv("KAFKA_DATA_DIR", "relative/dir")
    assert ac.resolve_paths("kafka", tmp_path, env_prefix="KAFKA").data_dir.is_absolute()
    monkeypatch.setenv("KAFKA_DATA_DIR", "   ")
    assert ac.resolve_paths("kafka", tmp_path, env_prefix="KAFKA").configured is False


def test_ensure_creates_folders_only(tmp_path):
    p = ac.resolve_paths("x", tmp_path, env_prefix="X").ensure()
    assert p.data_dir.is_dir() and p.logs_dir.is_dir() and p.cache_dir.is_dir()
    assert not p.db_path.exists() and not p.token_path.exists()
