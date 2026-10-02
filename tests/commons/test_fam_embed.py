"""fam_embed: embeddings through Borges's Hoard, with the Link fallback — against a fake hub."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from hoard_link import _famsvc, family, fam_embed
from hoard_link.docs import vecmath
from tests.commons.fam_hub import TOKEN, FakeHub, ToolError, sequence
from tests.hub.conftest import free_port


def configure(tmp_path, url, token=TOKEN):
    tf = tmp_path / "mcp-token"
    tf.write_text(token, encoding="utf-8")
    family.configure("vitruvius", token_file=str(tf), hub=url)


@pytest.fixture(autouse=True)
def _isolated_state():
    saved = dict(family._state)
    _famsvc.forget_availability()
    yield
    family._state.clear()
    family._state.update(saved)
    _famsvc.forget_availability()


@pytest.fixture
def hub(tmp_path):
    h = FakeHub()
    h.app("borges")
    configure(tmp_path, h.url)
    yield h
    h.close()


def vec_for(text: str, dim: int = 4):
    n = float(len(text))
    return [n + i for i in range(dim)]


def borges_embed(model="bge-small", dim=4, normalized=True):
    def handler(args, call):
        vs = [vec_for(t, dim) for t in args["texts"]]
        if normalized:
            vs = [vecmath.normalize(v) for v in vs]
        return {"model": model, "dim": dim, "vectors": vs, "normalized": normalized}
    return handler


def test_embed_texts_in_batches_keeps_order_and_names_the_model(hub):
    hub.on("borges", "embed_texts", borges_embed())
    texts = ["x" * (i + 1) for i in range(130)]
    res = fam_embed.embed_texts(texts, kind="document", batch=64)
    assert res["ok"] is True and res["via"] == "borges" and res["model"] == "bge-small" and res["dim"] == 4 and len(res["vectors"]) == 130
    assert res["vectors"][0] == pytest.approx(vecmath.normalize(vec_for("x")))
    assert res["vectors"][129] == pytest.approx(vecmath.normalize(vec_for("x" * 130)))
    calls = hub.of("embed_texts")
    assert [len(c["args"]["texts"]) for c in calls] == [64, 64, 2]
    assert all(c["args"]["kind"] == "document" and c["args"]["normalize"] is True for c in calls)


def test_embed_query(hub):
    hub.on("borges", "embed_texts", borges_embed())
    res = fam_embed.embed_query("garantía")
    assert res["ok"] and res["model"] == "bge-small" and res["dim"] == 4 and math.isclose(sum(x * x for x in res["vector"]), 1.0)
    assert hub.of("embed_texts")[0]["args"] == {"texts": ["garantía"], "kind": "query", "normalize": True}


def test_empty_input_and_bad_arguments_need_no_hub(hub):
    assert fam_embed.embed_texts([]) == {"ok": True, "model": "", "dim": 0, "vectors": [], "via": "borges"}
    assert fam_embed.embed_texts(["a"], kind="sentence")["kind"] == "client_error"
    assert fam_embed.embed_texts(None)["ok"] is False
    assert hub.calls == []


def test_non_normalised_answers_are_normalised_here_when_asked(hub):
    hub.on("borges", "embed_texts", borges_embed(normalized=False))
    res = fam_embed.embed_texts(["abc"], normalize=True)
    assert math.isclose(sum(x * x for x in res["vectors"][0]), 1.0)
    raw = fam_embed.embed_texts(["abc"], normalize=False)
    assert raw["vectors"][0] == vec_for("abc") and hub.of("embed_texts")[1]["args"]["normalize"] is False


def test_a_wrong_number_or_shape_of_vectors_is_an_error(hub):
    hub.on("borges", "embed_texts", {"model": "m", "dim": 4, "vectors": [[1, 2, 3, 4]]})
    res = fam_embed.embed_texts(["a", "b"])
    assert res["ok"] is False and "wrong number or shape" in res["error"]
    hub.on("borges", "embed_texts", {"model": "m", "dim": 4, "vectors": [[1, 2], [3]]})
    assert fam_embed.embed_texts(["a", "b"])["ok"] is False


def test_the_model_changing_between_batches_is_an_error_never_a_mix(hub):
    first, second = borges_embed(model="m1"), borges_embed(model="m2")
    hub.on("borges", "embed_texts", sequence(first, second))
    res = fam_embed.embed_texts(["a", "b", "c"], batch=2)
    assert res["ok"] is False and "changed during the call" in res["error"] and "vectors" not in res


def test_a_failure_after_the_first_batch_is_an_error_not_a_fallback(hub):
    def refuse(args, call):
        raise ToolError(503, "model still loading")
    hub.on("borges", "embed_texts", sequence(borges_embed(), refuse))
    link = SimpleNamespace(sync=SimpleNamespace(embed=lambda t: pytest.fail("fallback must not run")))
    res = fam_embed.embed_texts(["a", "b", "c"], batch=2, link=link)
    assert res["ok"] is False and res["via"] == "borges" and "model still loading" in res["error"] and "after 2 of 3" in res["error"]


def test_a_tool_error_from_borges_is_not_hidden_by_the_fallback(hub):
    def refuse(args, call):
        raise ToolError(400, "texts: at most 512 per call")
    hub.on("borges", "embed_texts", refuse)
    link = SimpleNamespace(sync=SimpleNamespace(embed=lambda t: pytest.fail("fallback must not run")))
    res = fam_embed.embed_texts(["a"], link=link)
    assert res == {"ok": False, "error": "texts: at most 512 per call", "via": "borges", "kind": "tool_error"}


class FakeLink:
    def __init__(self, model="nomic-embed-text", fail=None):
        self.calls, self.fail = [], fail
        self.sync = SimpleNamespace(embed=self._embed, resolve=lambda cap: SimpleNamespace(model=model, provider="ollama"))

    def _embed(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise self.fail
        return [[3.0, 4.0, float(len(t))] for t in texts]


@pytest.mark.parametrize("why", ["hub_down", "app_down", "tool_missing"])
def test_embed_texts_falls_back_to_link_and_says_which_model(tmp_path, hub, why):
    if why == "hub_down":
        configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    elif why == "app_down":
        hub.set_state("borges", "stopped")
    link = FakeLink()
    res = fam_embed.embed_texts(["a", "bb", "ccc"], batch=2, link=link)
    assert res["ok"] is True and res["via"] == "local" and res["model"] == "nomic-embed-text" and res["dim"] == 3 and len(res["vectors"]) == 3
    assert [len(c) for c in link.calls] == [2, 1]
    assert all(math.isclose(sum(x * x for x in v), 1.0) for v in res["vectors"])
    raw = fam_embed.embed_texts(["a"], normalize=False, link=link)
    assert raw["vectors"] == [[3.0, 4.0, 1.0]]


def test_the_fallback_returns_what_the_link_embeds_even_without_a_model_name(tmp_path):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    link = FakeLink()
    link.sync.resolve = lambda cap: (_ for _ in ()).throw(RuntimeError("no resolution"))
    res = fam_embed.embed_texts(["a"], link=link)
    assert res["ok"] is True and res["model"] == "" and res["via"] == "local"


def test_without_the_fallback_or_when_it_fails_the_reasons_are_given(tmp_path):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    assert fam_embed.embed_texts(["a"], local_fallback=False) == {"ok": False, "error": "hub unreachable", "via": "borges", "kind": "hub_down"}
    res = fam_embed.embed_texts(["a"], link=FakeLink(fail=RuntimeError("no embeddings model")))
    assert res["ok"] is False and res["via"] == "local" and "hub unreachable" in res["error"] and "no embeddings model" in res["error"]
    bad = FakeLink()
    bad.sync.embed = lambda texts: [[1.0]]                              # one vector for two texts
    assert fam_embed.embed_texts(["a", "b"], link=bad)["ok"] is False


def test_the_query_fallback(tmp_path):
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    res = fam_embed.embed_query("hola", link=FakeLink())
    assert res["ok"] and res["via"] == "local" and len(res["vector"]) == 3 and res["model"] == "nomic-embed-text"
    assert fam_embed.embed_query("hola", local_fallback=False)["error"] == "hub unreachable"


def test_status(hub):
    hub.on("borges", "embed_status", {"backend": "fastembed", "model": "bge-small", "dim": 384, "state": "ready"})
    res = fam_embed.status()
    assert res == {"ok": True, "backend": "fastembed", "model": "bge-small", "dim": 384, "state": "ready", "ready": True, "via": "borges"}
    hub.on("borges", "embed_status", {"backend": "fastembed", "model": "bge-small", "dim": 384, "state": "loading"})
    assert fam_embed.status()["ready"] is False
    hub.set_state("borges", "stopped")
    assert fam_embed.status() == {"ok": False, "error": "borges unreachable", "via": "borges", "kind": "app_down"}


def test_status_on_an_older_borges_and_a_dead_hub(tmp_path, hub):
    res = fam_embed.status()
    assert res["ok"] is False and res["kind"] == "tool_missing" and "embed_status" in res["error"]
    configure(tmp_path, f"http://127.0.0.1:{free_port()}")
    assert fam_embed.status()["error"] == "hub unreachable"


def test_available(hub):
    assert fam_embed.available() is True
    hub.set_state("borges", "starting")
    fam_embed.forget_availability()
    assert fam_embed.available() is False
