"""The skills shelf: what an app takes from the library, what an agent that connects gets."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hoard_link import skills
from hoard_link.agentkit import make_agent_router


def library(tmp_path):
    lib = tmp_path / "faustus" / "skills" / "library"
    for slug, tags, status in (("alpha", "[3d, blender]", "published"), ("beta", "[blender]", "published"), ("gamma", "[python]", "published"),
                               ("delta", "[3d, mesh]", "published"), ("draft", "[3d, mesh, blender]", "draft")):
        (lib / slug / "references").mkdir(parents=True)
        (lib / slug / "SKILL.md").write_text(f"---\nname: {slug}\ndescription: About {slug}. Use when needed.\nversion: 1.0.0\ncategory: engineering\n"
                                             f"tags: {tags}\nstatus: {status}\n---\n\n## Procedure\n\nDo {slug}.\n", encoding="utf-8")
        (lib / slug / "references" / "example.md").write_text("An example.", encoding="utf-8")
        (lib / slug / "references" / "picture.png").write_bytes(b"\x89PNG")
    return lib


def test_front_matter():
    meta, body = skills.parse("---\nname: x\ntags: [a, 'b']\n---\n\nBody")
    assert meta == {"name": "x", "tags": ["a", "b"]} and body == "Body"
    assert skills.parse("plain") == ({}, "plain")


def test_shelf_takes_named_then_closest_and_follows_the_library(tmp_path):
    lib = library(tmp_path)
    shelf = skills.Shelf("app", tmp_path / "data", {"slugs": ["beta", "missing"], "tags": ["3d", "mesh"], "limit": 3}, library=str(lib), max_age_s=0)
    first = shelf.refresh(force=True)
    assert first["added"] == ["beta", "delta", "alpha"] and first["source"] == "library"
    assert [s["slug"] for s in shelf.list()] == ["beta", "delta", "alpha"]          # named, then two shared tags, then one; the draft stays out
    assert shelf.read("delta")["files"] == ["references/example.md"]               # text files only
    theirs = lib / "theirs"
    theirs.mkdir()
    (theirs / "SKILL.md").write_text("---\nname: theirs\ndescription: Drives another app. Use when there.\ntags: [3d, mesh, blender]\napps: [other]\n---\n\nSteps.\n", encoding="utf-8")
    assert "theirs" not in [s["slug"] for s in skills.Shelf("app", tmp_path / "d2", {"tags": ["3d", "mesh"]}, library=str(lib)).list()]
    assert "theirs" in [s["slug"] for s in skills.Shelf("other", tmp_path / "d3", {"tags": ["3d", "mesh"]}, library=str(lib)).list()]
    assert "theirs" in [s["slug"] for s in skills.Shelf("app", tmp_path / "d4", {"slugs": ["theirs"]}, library=str(lib)).list()]
    theirs.joinpath("SKILL.md").unlink()
    assert shelf.read("skill-alpha", "references/example.md")["text"] == "An example."
    (lib / "delta" / "SKILL.md").write_text((lib / "delta" / "SKILL.md").read_text(encoding="utf-8") + "\nMore.\n", encoding="utf-8")
    (lib / "alpha" / "SKILL.md").unlink()
    second = shelf.refresh(force=True)
    assert second["updated"] == ["delta"] and second["removed"] == ["alpha"] and not (tmp_path / "data" / "skills" / "alpha").exists()
    shelf.library = str(tmp_path / "gone")
    skills.find_library = lambda setting=None: None                                 # no library anywhere
    try:
        assert [s["slug"] for s in shelf.list()] == ["beta", "delta"] and "More." in shelf.read("delta")["text"]
    finally:
        import importlib
        importlib.reload(skills)


def test_an_agent_that_connects_gets_them(tmp_path):
    shelf = skills.Shelf("app", tmp_path / "data", {"slugs": ["alpha"]}, library=str(library(tmp_path)))
    app = FastAPI()
    assert shelf.list(refresh=False) == [] and not (tmp_path / "data").exists()         # only reading: nothing looked up, nothing written
    shelf.refresh(force=True)
    app.include_router(make_agent_router(tools_fn=lambda: [{"name": "ping", "description": "", "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object"}}],
                                         call_fn=lambda name, arguments: {"pong": True}, token_fn=lambda: "t", instructions="An app.", app_name="app", skills=shelf))
    with TestClient(app) as c:
        listing = c.get("/api/agent/tools").json()
        assert [t["name"] for t in listing["tools"]] == ["ping", "skills_list", "skill_read", "skills_refresh", "skills_library", "skills_assign"]
        assert listing["instructions"].startswith("An app.\n\nSkills.") and "- alpha: About alpha." in listing["instructions"]
        assert [s["slug"] for s in listing["skills"]] == ["alpha"]
        auth = {"Authorization": "Bearer t"}
        assert "Do alpha." in c.post("/api/agent/call", json={"name": "skill_read", "arguments": {"slug": "alpha"}}, headers=auth).json()["text"]
        assert c.post("/api/agent/call", json={"name": "skill_read", "arguments": {"slug": "nope"}}, headers=auth).status_code == 404
        assert c.post("/api/agent/call", json={"name": "ping", "arguments": {}}, headers=auth).json() == {"pong": True}
        assert c.post("/api/agent/call", json={"name": "skills_list"}).status_code == 401
        state = c.get("/api/agent/skills").json()
        assert state["shelf"][0]["slug"] == "alpha" and state["shelf"][0]["why"] == "manifest" and len(state["library_skills"]) == 4
        assert c.post("/api/agent/skills/assign", json={"slug": "gamma"}).json()["shelf"][1]["why"] == "added"
        assert c.post("/api/agent/skills/assign", json={"slug": "alpha", "on": False}).json()["shelf"][0]["slug"] == "gamma"
        assert "__BASE__" not in c.get("/api/agent/skills/page").text


def test_an_app_without_skills_is_as_before(tmp_path):
    app = FastAPI()
    app.include_router(make_agent_router(tools_fn=lambda: [], call_fn=lambda name, arguments: None, token_fn=lambda: "t", instructions="An app.",
                                         app_name="app", skills=False))
    with TestClient(app) as c:
        listing = c.get("/api/agent/tools").json()
        assert listing == {"instructions": "An app.", "tools": [], "app": "app"}


def test_the_hub_assigns_skills_to_any_app(tmp_path):
    from types import SimpleNamespace

    from hoard_link.hub.skills import SkillsFacet

    lib = library(tmp_path)
    folder = tmp_path / "apps" / "one"
    (folder / "pkg" / "hoard_link").mkdir(parents=True)
    (folder / "pkg" / "hoard_link" / "skills.py").write_text("", encoding="utf-8")
    (folder / "pkg" / "hoard_link" / "bridge.py").write_text("def _own_skills(): ...", encoding="utf-8")
    (folder / "mcp_server.py").write_text("CatalogBridge(app='one')", encoding="utf-8")
    (folder / "faustus-plugin.json").write_text('{"id": "one", "skills": {"slugs": ["alpha"]}, "mcp": {"args": ["{ONE_DIR}/mcp_server.py"]}}', encoding="utf-8")
    bare = tmp_path / "apps" / "two"
    bare.mkdir()
    (bare / "own.py").write_text("server = Server('two')", encoding="utf-8")
    (bare / "faustus-plugin.json").write_text('{"id": "two", "mcp": {"args": ["-m", "own"]}}', encoding="utf-8")
    apps = [SimpleNamespace(id="one", name="One", folder=str(folder), data_dir=str(folder / "data"), manifest_path=str(folder / "faustus-plugin.json"), url="http://127.0.0.1:9"),
            SimpleNamespace(id="two", name="Two", folder=str(bare), data_dir="", manifest_path=str(bare / "faustus-plugin.json"), url="http://127.0.0.1:9")]
    facet = SkillsFacet(SimpleNamespace(apps=apps, config=SimpleNamespace(faustus_dir=str(lib))))

    def req(method, path, body=None, **query):
        from hoard_link.hub.facets import Request
        return Request(method=method, path=path, query={k: [v] for k, v in query.items()}, body=body or {}, caller=lambda: "ui", agent=lambda: False)

    assert facet.post(req("POST", "/api/skills/refresh"))["changed"] == [{"app": "one", "added": ["alpha"], "updated": [], "removed": []}]
    rows = {r["id"]: r for r in facet.get(req("GET", "/api/skills/overview"))["apps"]}
    assert rows["one"]["skills"] == ["alpha"] and rows["one"]["serves"] and rows["two"]["skills"] == [] and not rows["two"]["serves"]
    out = facet.post(req("POST", "/api/skills/app/two/assign", {"slug": "gamma"}))
    assert [s["slug"] for s in out["shelf"]] == ["gamma"] and (bare / "data" / "skills" / "gamma" / "SKILL.md").is_file()
    assert facet.post(req("POST", "/api/skills/app/two/assign", {"slug": "nope"}))["status"] == 404
    state = facet.get(req("GET", "/api/skills/app/one", q="python"))
    assert [s["slug"] for s in state["library_skills"]] == ["gamma"] and state["shelf"][0]["why"] == "manifest"
    assert "Do delta." in facet.get(req("GET", "/api/skills/app/one/read/delta"))["text"]                # not on the shelf: read from the library
    page = facet.get(req("GET", "/api/skills/page", app="one"))
    assert b'"/api/skills/app/one"' in page.body and page.content_type.startswith("text/html")
    assert facet.handlers()["hub_skills_assign"]({"app": "one", "slug": "beta"})["ok"]
    assert [t["name"] for t in SkillsFacet.tools()] == ["hub_agent_flows", "hub_skills_overview", "hub_skills_library", "hub_skill_read", "hub_skills_assign"]
    assert "Do alpha." in facet.handlers()["hub_skill_read"]({"app": "one", "slug": "alpha"})["text"]
    assert not facet.handlers()["hub_skill_read"]({"app": "one", "slug": "nope"})["ok"]


def test_the_bridge_hands_out_skills_for_an_app_whose_own_tool_list_does_not(tmp_path):
    import asyncio
    import time

    from hoard_link.bridge import CatalogBridge

    lib = library(tmp_path)
    skills.Shelf("app", tmp_path / "data", {"slugs": ["alpha"]}, library=str(lib)).refresh(force=True)          # the hub (or the app) keeps the copy
    bridge = CatalogBridge(app="app", service="svc", package="pkg", default_port=1, root=str(tmp_path / "mcp_server.py"))
    assert bridge._shelf() is None                                                                             # an app's own tests never see it
    reader = skills.Shelf("app", tmp_path / "data", {"slugs": ["alpha"]}, library=str(lib), max_age_s=float("inf"))
    reader._checked = time.monotonic()
    bridge._shelf_obj = reader
    bridge._catalog = [{"name": "ping", "description": "", "inputSchema": {"type": "object"}}]
    bridge.instructions = "An app."
    bridge._own_skills()
    assert [t["name"] for t in bridge._catalog] == ["ping", "skills_list", "skill_read", "skills_library", "skills_assign"]
    assert "- alpha: About alpha." in bridge.instructions and [s["slug"] for s in bridge.skills] == ["alpha"]
    assert "Do alpha." in asyncio.run(bridge.call("skill_read", {"slug": "alpha"})).body["text"]
    assert asyncio.run(bridge.call("skill_read", {"slug": "nope"})).is_error
    # an app that already hands them out is left alone
    bridge._catalog = [{"name": "skills_list"}]
    bridge.skills = []
    bridge._own_skills()
    assert not bridge._local_skills and bridge.skills == []


def test_a_hand_written_mcp_server_is_given_the_shelf(tmp_path):
    pytest = __import__("pytest")
    pytest.importorskip("mcp")
    try:
        from mcp.server.fastmcp import FastMCP as Base          # mcp 1.x
        table, wanted = "request_handlers", None
    except ImportError:
        from mcp.server.mcpserver import MCPServer as Base      # mcp 2.x
        table, wanted = "_request_handlers", "prompts/get"
    lib = library(tmp_path)
    app = tmp_path / "app"
    app.mkdir()
    (app / "faustus-plugin.json").write_text('{"id": "app", "skills": {"slugs": ["alpha"]}}', encoding="utf-8")
    server = Base("app", instructions="An app.")
    assert skills.attach(server, "app", root=app / "mcp_server.py") is None                                    # an app's own tests never see it
    assert skills._attach(server, "app", app / "mcp_server.py", None) is None                                  # nothing copied yet: nothing handed out
    skills.Shelf("app", app / "data", {"slugs": ["alpha"]}, library=str(lib)).refresh(force=True)
    shelf = skills._attach(server, "app", app / "mcp_server.py", None)
    low = getattr(server, "_mcp_server", None) or getattr(server, "_lowlevel_server", None)
    assert [s["slug"] for s in shelf.list(refresh=False)] == ["alpha"]
    assert low.instructions.startswith("An app.\n\nSkills.") and "- alpha: About alpha." in low.instructions
    if wanted is None:
        from mcp import types
        wanted = types.GetPromptRequest
    handlers = getattr(low, table)
    assert wanted in handlers and handlers.get(wanted) is not None and handlers[wanted] is not None


def test_a_bridge_that_speaks_json_rpc_by_hand_is_given_the_shelf(tmp_path):
    lib = library(tmp_path)
    assert skills.raw("app", root=tmp_path).shelf is None                                                      # an app's own tests never see it
    idle = skills.RawBridge("app", skills.Shelf("app", tmp_path / "none", {"slugs": ["alpha"]}, library=str(lib), max_age_s=float("inf")))
    idle.shelf._checked = __import__("time").monotonic()
    assert idle.answer("prompts/list") is None and idle.extend("tools/list", {"tools": []}) == {"tools": []}    # nothing copied: nothing changes
    skills.Shelf("app", tmp_path / "data", {"slugs": ["alpha"]}, library=str(lib)).refresh(force=True)
    shelf = skills.Shelf("app", tmp_path / "data", {"slugs": ["alpha"]}, library=str(lib), max_age_s=float("inf"))
    shelf._checked = __import__("time").monotonic()
    bridge = skills.RawBridge("app", shelf)
    start = bridge.extend("initialize", {"capabilities": {"tools": {}}, "instructions": "An app."})
    assert start["capabilities"] == {"tools": {}, "prompts": {}} and start["instructions"].startswith("An app.\n\nSkills.") and "- alpha: About alpha." in start["instructions"]
    names = [t["name"] for t in bridge.extend("tools/list", {"tools": [{"name": "ping"}]})["tools"]]
    assert names == ["ping", "skills_list", "skill_read", "skills_library", "skills_assign"]
    assert bridge.extend("tools/list", {"tools": [{"name": "skills_list"}]}) == {"tools": [{"name": "skills_list"}]}                # an app that hands them out is left alone
    read = bridge.answer("tools/call", {"name": "skill_read", "arguments": {"slug": "alpha"}})
    assert not read["isError"] and "Do alpha." in json.loads(read["content"][0]["text"])["text"]
    assert bridge.answer("tools/call", {"name": "skill_read", "arguments": {"slug": "nope"}})["isError"]
    assert bridge.answer("tools/call", {"name": "ping"}) is None and bridge.answer("tools/list") is None
    assert [p["name"] for p in bridge.answer("prompts/list")["prompts"]] == ["skill-alpha"]
    assert "Do alpha." in bridge.answer("prompts/get", {"name": "skill-alpha"})["messages"][0]["content"]["text"]
