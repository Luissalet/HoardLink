"""The skills shelf: what an app takes from the library, what an agent that connects gets."""
from __future__ import annotations

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
    app.include_router(make_agent_router(tools_fn=lambda: [{"name": "ping", "description": "", "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object"}}],
                                         call_fn=lambda name, arguments: {"pong": True}, token_fn=lambda: "t", instructions="An app.", app_name="app", skills=shelf))
    app.include_router(skills.make_router(shelf))
    with TestClient(app) as c:
        listing = c.get("/api/agent/tools").json()
        assert [t["name"] for t in listing["tools"]] == ["ping", "skills_list", "skill_read", "skills_refresh"]
        assert listing["instructions"].startswith("An app.\n\nSkills.") and "- alpha: About alpha." in listing["instructions"]
        assert [s["slug"] for s in listing["skills"]] == ["alpha"]
        auth = {"Authorization": "Bearer t"}
        assert "Do alpha." in c.post("/api/agent/call", json={"name": "skill_read", "arguments": {"slug": "alpha"}}, headers=auth).json()["text"]
        assert c.post("/api/agent/call", json={"name": "skill_read", "arguments": {"slug": "nope"}}, headers=auth).status_code == 404
        assert c.post("/api/agent/call", json={"name": "ping", "arguments": {}}, headers=auth).json() == {"pong": True}
        assert c.post("/api/agent/call", json={"name": "skills_list"}).status_code == 401
        assert c.get("/api/skills").json()["skills"][0]["slug"] == "alpha"


def test_an_app_without_skills_is_as_before(tmp_path):
    app = FastAPI()
    app.include_router(make_agent_router(tools_fn=lambda: [], call_fn=lambda name, arguments: None, token_fn=lambda: "t", instructions="An app.",
                                         app_name="app", skills=False))
    with TestClient(app) as c:
        listing = c.get("/api/agent/tools").json()
        assert listing == {"instructions": "An app.", "tools": [], "app": "app"}
