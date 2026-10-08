"""Measured routes (~/.hoard/routes.json): parsing, caching, name matching, and how
Link orders the candidates it already considers by them."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from hoard_link import routes as routes_mod
from hoard_link.config import CapabilityConfig, LinkConfig
from hoard_link.link import _prefer_many
from hoard_link.routes import Routes, load_routes, names_match
from tests.conftest import Router, llama_health, make_link
from tests.test_resolution_policies import HEALTHY, ollama_router, registry
from tests.test_resolve_loopback import props, v1_models

UPDATED = "2026-10-02T10:00:00+02:00"


def pref(*names, score=0.8):
    return {"names": list(names), "score": score, "ci": [round(score - 0.07, 2), round(score + 0.06, 2)], "n": 60,
            "tok_s": 34.2, "vram_gb": 17.1}


def doc(tasks=None, capabilities=None, source="galton"):
    return {"schema": 1, "source": source, "updated_at": UPDATED, "tasks": tasks or {},
            "capabilities": capabilities or {}}


def write(tmp_path: Path, data, name="routes.json") -> Path:
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def cfg_for(path: Path, **kw) -> LinkConfig:
    env = {"HOARD_ROUTES_FILE": str(path)}
    return LinkConfig.load(None, env=env, app="test") if not kw else LinkConfig(routes_file=str(path), **kw)


FULL = doc(
    tasks={"code": {"capability": "llm", "prefer": [pref("qwen3.8:27b-q4_K_M", "qwen3.8-27b-q4-llamacpp")],
                    "explain": "best on the code set"}},
    capabilities={"llm": {"prefer": [pref("gemma3:12b", score=0.6)]}, "vision": {"prefer": [pref("vl:7b")]}},
)


# ---- parsing, caching ----------------------------------------------------------------

def test_parse_full_document(tmp_path):
    r = load_routes(write(tmp_path, FULL))
    assert r.problem is None and r.source == "galton" and r.updated_at == UPDATED
    code = r.tasks["code"]
    assert code.capability == "llm" and code.explain == "best on the code set"
    p = code.prefer[0]
    assert p.names == ("qwen3.8:27b-q4_K_M", "qwen3.8-27b-q4-llamacpp")
    assert p.score == 0.8 and p.ci == (0.73, 0.86) and p.n == 60 and p.tok_s == 34.2 and p.vram_gb == 17.1
    assert r.summary() == {"file": str(tmp_path / "routes.json"), "updated_at": UPDATED, "source": "galton",
                           "tasks": ["code"], "problem": None}


def test_cached_by_mtime_and_size(tmp_path):
    path = write(tmp_path, FULL)
    first = load_routes(path)
    assert load_routes(path) is first                       # untouched file: same object, no re-parse
    changed = dict(FULL, source="other")
    path.write_text(json.dumps(changed), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    second = load_routes(path)
    assert second is not first and second.source == "other"
    # same size, only the mtime moved: still noticed
    path.write_text(json.dumps(dict(FULL, source="ither")), encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
    assert load_routes(path).source == "ither"


def test_missing_and_invalid_files_never_raise(tmp_path):
    r = load_routes(tmp_path / "nope.json")
    assert r.empty and r.problem and r.preferences("llm") == []
    assert r.summary()["problem"]
    assert "unreadable" in (load_routes(write(tmp_path, "{not json", "bad.json")).problem or "")
    assert load_routes(write(tmp_path, "[1, 2]", "list.json")).problem == "not a JSON object"
    weird = write(tmp_path, {"tasks": {"x": 5, "y": {"prefer": "no"}, "z": {"prefer": [{"names": []}, 7,
                                                                                    {"names": "solo"}]}},
                             "capabilities": {"llm": 3}}, "weird.json")
    r = load_routes(weird)
    assert r.problem is None and r.tasks["z"].prefer[0].names == ("solo",)


def test_bom_file_is_read(tmp_path):
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(FULL).encode("utf-8"))
    assert load_routes(path).tasks["code"].capability == "llm"


def test_default_path_follows_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HOARD_ROUTES_FILE", raising=False)
    monkeypatch.setenv("HOARD_HOME", str(tmp_path / "home"))
    assert routes_mod.routes_path() == tmp_path / "home" / "routes.json"
    monkeypatch.setenv("HOARD_ROUTES_FILE", str(tmp_path / "elsewhere.json"))
    assert routes_mod.routes_path() == tmp_path / "elsewhere.json"
    write(tmp_path, FULL, "elsewhere.json")
    assert load_routes().source == "galton"


# ---- names and preference order ----------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Qwen3:8B", "qwen3:8b"),
    ("llama3.1", "llama3.1:latest"),
    ("llama3.1:latest", "LLAMA3.1"),
    ("qwen3.8-27b-q8.gguf", "qwen3.8-27b-q8"),
    ("D:\\models\\Qwen3.8-27B-Q8.gguf", "qwen3.8-27b-q8.gguf"),
    ("/models/x/m.gguf", "m"),
])
def test_names_match(a, b):
    assert names_match(a, b) and names_match(b, a)


@pytest.mark.parametrize("a,b", [("qwen3:8b", "qwen3:14b"), ("llama3.1:latest", "llama3.1:8b"), ("", ""), ("m", None)])
def test_names_do_not_match(a, b):
    assert not names_match(a, b)


def test_preferences_task_then_capability_deduplicated(tmp_path):
    r = load_routes(write(tmp_path, doc(
        tasks={"code": {"capability": "llm", "prefer": [pref("A", "b"), pref("c")]},
               "see": {"capability": "vision", "prefer": [pref("v1")]}},
        capabilities={"llm": {"prefer": [pref("c"), pref("B"), pref("d")]}})))
    assert r.preferences("llm") == ["c", "B", "d"]
    assert r.preferences("llm", "code") == ["A", "b", "c", "d"]           # c, B repeats dropped, order kept
    assert r.preferences("llm", "see") == ["c", "B", "d"]                 # the task is about vision: ignored
    assert r.preferences("vision", "see") == ["v1"]
    assert r.preferences("llm", "unknown-task") == ["c", "B", "d"]
    assert r.preferences("embeddings") == []


def test_prefer_many_keeps_original_order_for_the_rest():
    assert _prefer_many(["a", "b", "c", "d"], ["C", "b:latest"]) == ["c", "b", "a", "d"]
    assert _prefer_many(["a", "b"], []) == ["a", "b"]
    assert _prefer_many(["a", "b"], ["zzz"]) == ["a", "b"]


# ---- Link: Ollama (resident) ---------------------------------------------------------------

def ollama3(**kw):
    return ollama_router(["m-a:8b", "m-b:8b", "m-c:8b"], {n: ["completion"] for n in ("m-a:8b", "m-b:8b", "m-c:8b")}, **kw)


@pytest.mark.asyncio
async def test_resident_ollama_models_are_ranked_by_measured_routes(tmp_path):
    path = write(tmp_path, doc(tasks={"code": {"capability": "llm", "prefer": [pref("m-c:8b")]}},
                               capabilities={"llm": {"prefer": [pref("m-b:8b")]}}))
    link = make_link(ollama3(), config=cfg_for(path))
    res = await link.resolve("llm", task="code")
    assert res.model == "m-c:8b" and res.details["resident"] is True
    assert res.reason.endswith("; ranked by measured routes (task code)")
    assert res.details["routes"] == {"task": "code", "source": "galton", "updated_at": UPDATED}

    res = await make_link(ollama3(), config=cfg_for(path)).resolve("llm")          # no task: the capability list
    assert res.model == "m-b:8b" and "ranked by measured routes" in res.reason

    res = await make_link(ollama3(), config=cfg_for(path)).resolve("llm", task="writing")   # unknown task: same
    assert res.model == "m-b:8b"


@pytest.mark.asyncio
async def test_explicit_model_still_wins_over_routes(tmp_path):
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("m-c:8b")]}}))
    cfg = LinkConfig.load(None, env={"HOARD_ROUTES_FILE": str(path), "HOARD_LLM_MODEL": "m-a:8b"})
    res = await make_link(ollama3(), config=cfg).resolve("llm")
    assert res.model == "m-a:8b" and "routes" not in res.details and "measured" not in res.reason


@pytest.mark.asyncio
async def test_explicit_model_comes_first_then_routes_then_original_order(tmp_path):
    # explicit m-b, measured m-c: m-b wins; the measured one is only used when the explicit one is not resident
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("m-c:8b")]}}))
    cfg = LinkConfig.load(None, env={"HOARD_ROUTES_FILE": str(path), "HOARD_LLM_MODEL": "m-zzz"})
    res = await make_link(ollama3(), config=cfg).resolve("llm")
    assert res.model == "m-c:8b" and "preferred 'm-zzz' not available" in res.reason
    assert "ranked by measured routes" in res.reason


@pytest.mark.asyncio
async def test_no_note_when_routes_did_not_change_the_pick(tmp_path):
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("m-a:8b")]}}))      # already first
    res = await make_link(ollama3(), config=cfg_for(path)).resolve("llm")
    assert res.model == "m-a:8b" and "measured" not in res.reason and "routes" not in res.details


@pytest.mark.asyncio
async def test_ollama_latest_suffix_and_case_match(tmp_path):
    router = ollama_router(["one:latest", "two:latest"], {"one:latest": ["completion"], "two:latest": ["completion"]})
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("TWO")]}}))
    res = await make_link(router, config=cfg_for(path)).resolve("llm")
    assert res.model == "two:latest"


@pytest.mark.asyncio
async def test_routes_only_reorder_what_fits_the_capability(tmp_path):
    router = ollama_router(["embed:1", "chat:1"], {"embed:1": ["embedding"], "chat:1": ["completion"]})
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("embed:1")]}}))
    res = await make_link(router, config=cfg_for(path)).resolve("llm")
    assert res.model == "chat:1"                                    # an embedder is never chosen for chat


@pytest.mark.asyncio
async def test_chat_uses_the_measured_model_for_the_task(tmp_path):
    bodies = []

    def chat(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json={"message": {"content": "ok"}})

    router = ollama3()
    router.post(11434, "/api/chat", chat)
    path = write(tmp_path, doc(tasks={"code": {"capability": "llm", "prefer": [pref("m-c:8b")]}}))
    link = make_link(router, config=cfg_for(path))
    res = await link.chat([{"role": "user", "content": "x"}], task="code")
    assert bodies[0]["model"] == "m-c:8b" and res.model == "m-c:8b"
    await link.chat([{"role": "user", "content": "x"}])
    assert bodies[1]["model"] == "m-a:8b"                           # no task, no capability list: original order


# ---- Link: never loads because of routes ---------------------------------------------------

@pytest.mark.asyncio
async def test_routes_never_cause_a_load(tmp_path):
    seen = []

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"capabilities": ["completion"]})

    router = Router()
    router.get(11434, "/api/ps", httpx.Response(200, json={"models": []}))
    router.get(11434, "/api/tags", httpx.Response(200, json={"models": [{"name": "m-a:8b"}, {"name": "m-b:8b"}]}))
    router.post(11434, "/api/show", spy)
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("m-b:8b")]}}))
    link = make_link(router, config=cfg_for(path))
    res = await link.resolve("llm")
    assert res.state == "unavailable"                               # only_resident: a measured model is not loaded
    assert any("only_resident=True" in r for r in res.details["reasons"])
    assert seen == []
    from hoard_link.errors import Unavailable
    with pytest.raises(Unavailable):
        await link.chat([{"role": "user", "content": "x"}], task="code")


@pytest.mark.asyncio
async def test_when_loading_is_allowed_routes_rank_the_loadable_models(tmp_path):
    router = Router()
    router.get(11434, "/api/ps", httpx.Response(200, json={"models": []}))
    router.get(11434, "/api/tags", httpx.Response(200, json={"models": [{"name": "m-a:8b"}, {"name": "m-b:8b"}]}))
    router.post(11434, "/api/show", httpx.Response(200, json={"capabilities": ["completion"]}))
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("m-b:8b")]}}))
    cfg = LinkConfig(only_resident=False, routes_file=str(path), capabilities={"llm": CapabilityConfig(allow_load=True)})
    res = await make_link(router, config=cfg).resolve("llm")
    assert res.model == "m-b:8b" and res.details["resident"] is False
    assert "would load" in res.reason and "ranked by measured routes" in res.reason


@pytest.mark.asyncio
async def test_resident_model_wins_over_a_better_measured_one_that_is_not_loaded(tmp_path):
    router = ollama_router(["m-a:8b"], {"m-a:8b": ["completion"]}, tags=["m-a:8b", "m-z:70b"])
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("m-z:70b")]}}))
    res = await make_link(router, config=cfg_for(path)).resolve("llm")
    assert res.model == "m-a:8b" and res.details["resident"] is True and "measured" not in res.reason


@pytest.mark.asyncio
async def test_explicit_url_is_never_overridden(tmp_path):
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("other")]}}))
    cfg = LinkConfig(routes_file=str(path), capabilities={"llm": CapabilityConfig(
        url="http://127.0.0.1:9999/v1/chat/completions", model="pinned", api="openai")})
    res = await make_link(Router(), config=cfg).resolve("llm", task="code")
    assert res.model == "pinned" and res.details == {"source": "explicit"}


# ---- Link: several llama.cpp servers ----------------------------------------------------------

def two_llamas(modalities=(None, None)):
    from tests.conftest import llama_health

    router = Router()
    for port, name, mods in ((8081, "model-a", modalities[0]), (8082, "model-b", modalities[1])):
        llama_health(port, router=router)
        router.get(port, "/props", props(model_path=f"/models/{name}-q8.gguf", modalities=mods))
        router.get(port, "/v1/models", v1_models(name))
    return router


@pytest.mark.asyncio
async def test_llamacpp_pick_between_resident_servers_by_measured_route(tmp_path):
    path = write(tmp_path, doc(tasks={"code": {"capability": "llm", "prefer": [pref("model-b")]}}))
    link = make_link(two_llamas(), config=cfg_for(path))
    res = await link.resolve("llm", task="code")
    assert res.model == "model-b" and res.url == "http://127.0.0.1:8082/v1/chat/completions"
    assert res.details["port"] == 8082 and res.details["resident"] is True
    assert res.reason.endswith("; ranked by measured routes (task code)")
    assert res.details["routes"]["task"] == "code"
    # another task, or none: the first server, as before
    res = await make_link(two_llamas(), config=cfg_for(path)).resolve("llm")
    assert res.model == "model-a" and "routes" not in res.details


@pytest.mark.asyncio
async def test_llamacpp_matches_the_gguf_file_name_too(tmp_path):
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("model-b-q8.gguf")]}}))
    res = await make_link(two_llamas(), config=cfg_for(path)).resolve("llm")
    assert res.details["port"] == 8082


@pytest.mark.asyncio
async def test_llamacpp_first_choice_between_two_measured_servers_follows_the_list_order(tmp_path):
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("model-b"), pref("model-a")]}}))
    res = await make_link(two_llamas(), config=cfg_for(path)).resolve("llm")
    assert res.model == "model-b"


@pytest.mark.asyncio
async def test_llamacpp_no_match_keeps_the_first_server(tmp_path):
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("something-else")]}}))
    res = await make_link(two_llamas(), config=cfg_for(path)).resolve("llm")
    assert res.model == "model-a" and "measured" not in res.reason


@pytest.mark.asyncio
async def test_llamacpp_explicit_model_beats_routes(tmp_path):
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("model-b")]}}))
    cfg = LinkConfig(routes_file=str(path), capabilities={"llm": CapabilityConfig(model="model-a")})
    res = await make_link(two_llamas(), config=cfg).resolve("llm")
    assert res.model == "model-a"


@pytest.mark.asyncio
async def test_llamacpp_vision_skips_servers_without_vision_even_if_measured_best(tmp_path):
    path = write(tmp_path, doc(capabilities={"vision": {"prefer": [pref("model-a")]}}))
    res = await make_link(two_llamas(modalities=([], ["vision"])), config=cfg_for(path)).resolve("vision")
    assert res.model == "model-b"


# ---- Link: Faustus registry and OpenAI-compatible ---------------------------------------------

@pytest.mark.asyncio
async def test_faustus_registry_ollama_models_are_ranked_by_routes(tmp_path):
    item = {"url": "http://127.0.0.1:11434", "models": ["m-a:8b", "m-b:8b"], "category": "local",
            "model_type": "llm", "backend": "ollama", "endpoint_id": "e1"}
    router = ollama_router(["m-a:8b", "m-b:8b"], {}, tags=["m-a:8b", "m-b:8b"])
    router.get(7000, "/api/health", HEALTHY).get(7000, "/api/models", registry(item))
    path = write(tmp_path, doc(tasks={"code": {"capability": "llm", "prefer": [pref("m-b:8b")]}}))
    res = await make_link(router, config=cfg_for(path)).resolve("llm", task="code")
    assert res.model == "m-b:8b" and res.details["source"] == "faustus_registry"
    assert res.reason.endswith("; resident; ranked by measured routes (task code)")


@pytest.mark.asyncio
async def test_openai_compatible_server_models_are_ranked(tmp_path):
    router = Router().get(1234, "/v1/models", httpx.Response(200, json={"data": [{"id": "x"}, {"id": "y"}]}))
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("y")]}}))
    res = await make_link(router, config=cfg_for(path)).resolve("llm")
    assert res.model == "y" and "ranked by measured routes" in res.reason


# ---- switching it off, config, status ---------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_by_env(tmp_path):
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("m-c:8b")]}}))
    cfg = LinkConfig.load(None, env={"HOARD_ROUTES_FILE": str(path), "HOARD_ROUTES": "0"})
    assert cfg.use_routes is False
    res = await make_link(ollama3(), config=cfg).resolve("llm")
    assert res.model == "m-a:8b" and "measured" not in res.reason
    status = await make_link(ollama3(), config=cfg).status()
    assert status["routes"]["problem"] == "routes disabled"


@pytest.mark.asyncio
async def test_disabled_by_backend_json(tmp_path):
    path = write(tmp_path, doc(capabilities={"llm": {"prefer": [pref("m-c:8b")]}}))
    conf = write(tmp_path, {"routes": {"enabled": False, "file": str(path)}}, "backend.json")
    cfg = LinkConfig.load(conf, env={})
    assert cfg.use_routes is False and cfg.routes_file == str(path)
    assert (await make_link(ollama3(), config=cfg).resolve("llm")).model == "m-a:8b"


def test_config_defaults_and_overrides(tmp_path):
    cfg = LinkConfig.load(None, env={})
    assert cfg.use_routes is True and cfg.routes_file is None
    conf = write(tmp_path, {"routes": {"file": "/from/file.json"}}, "backend.json")
    assert LinkConfig.load(conf, env={}).routes_file == "/from/file.json"
    assert LinkConfig.load(conf, env={"HOARD_ROUTES_FILE": "/from/env.json"}).routes_file == "/from/env.json"
    assert LinkConfig.load(None, env={"HOARD_ROUTES": "off"}).use_routes is False
    assert LinkConfig.load(None, env={"HOARD_ROUTES": "1"}).use_routes is True
    assert LinkConfig.load(None, env={"HOARD_ROUTES": ""}).use_routes is True


@pytest.mark.asyncio
async def test_status_reports_the_routes_file(tmp_path):
    path = write(tmp_path, FULL)
    status = await make_link(Router(), config=cfg_for(path)).status()
    assert status["routes"] == {"file": str(path), "updated_at": UPDATED, "source": "galton",
                                "tasks": ["code"], "problem": None}
    missing = await make_link(Router(), config=cfg_for(tmp_path / "gone.json")).status()
    assert missing["routes"]["problem"] and missing["routes"]["tasks"] == []


@pytest.mark.asyncio
async def test_broken_routes_file_does_not_break_resolution(tmp_path):
    path = write(tmp_path, "{oops")
    res = await make_link(ollama3(), config=cfg_for(path)).resolve("llm", task="code")
    assert res.model == "m-a:8b"


def test_sync_facade_takes_a_task(tmp_path):
    path = write(tmp_path, doc(tasks={"code": {"capability": "llm", "prefer": [pref("m-c:8b")]}}))
    link = make_link(ollama3(), config=cfg_for(path))
    try:
        assert link.sync.resolve("llm", task="code").model == "m-c:8b"
        assert link.sync.resolve("llm", "code").model == "m-c:8b"
        assert link.sync.status(task="code")["llm"]["model"] == "m-c:8b"
    finally:
        link.sync.close()
