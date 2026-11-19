import pytest

from hoard_link.hub.agora import Agora, AgoraError, PERSON, TOOLS
from hoard_link.hub import agora_orders


@pytest.fixture()
def agora(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    ag.space_create({"agent": "claude", "id": "estudio", "title": "Estudio"})
    view = ag.for_space("estudio")
    yield view
    ag.close()


def person(**kw):
    return {"agent": PERSON, "_person": True, **kw}


def cartographer(ag, folder):
    """The scene's order, a plan of four jobs (two in parallel after the sheet) and its pieces."""
    for name in ("lamina.png", "hoja.png", "malla.glb", "giro-0.png", "giro-1.png", "modelo.blend", "render.png"):
        (folder / name).write_bytes(name.encode())
    order = ag.order_create(person(title="La cartógrafa", words="Sube tres escalones bajo la lluvia.",
                                   refs=[{"app": "writer", "object": "capítulo 1", "place": "párrafo 3"}],
                                   deliverables=["lámina", "hoja", "modelo articulado"],
                                   keep=["tubo a la izquierda", "al mapa le falta una esquina"], folder=str(folder)))["order"]
    plan = ag.plan_set({"agent": "claude", "order_id": order["id"], "jobs": [
        {"key": "concept", "title": "Lámina", "station": "prospero", "produces": ["image"], "check": "mirar la lámina"},
        {"key": "sheet", "title": "Hoja", "station": "prospero", "produces": ["character_sheet"], "after": ["concept"]},
        {"key": "mesh", "title": "Imagen a 3D", "station": "vulcan", "produces": ["mesh"], "after": ["sheet"],
         "assignee": {"agent": "codex-volumen", "model": "sol-6.2", "machine": "pc"}},
        {"key": "video", "title": "Vídeo", "station": "prospero", "produces": ["video"], "after": ["sheet"]},
        {"key": "rig", "title": "Modelo articulado", "station": "blender", "produces": ["rigged_model"], "after": ["mesh"]},
    ]})
    keys = plan["keys"]
    add = lambda **kw: ag.piece_add({"agent": "claude", "order_id": order["id"], **kw})["piece"]
    lamina = add(kind="image", title="Lámina", path="lamina.png", task_id=keys["concept"], model="qwen-image")
    hoja = add(kind="character_sheet", title="Hoja", path="hoja.png", task_id=keys["sheet"], derives_from=[lamina["id"]])
    malla = add(kind="mesh", title="Malla", path="malla.glb", previews=["giro-0.png", "giro-1.png"],
                task_id=keys["mesh"], derives_from=[hoja["id"]])
    modelo = add(kind="rigged_model", title="Modelo", path="modelo.blend", task_id=keys["rig"], derives_from=[malla["id"]])
    render = add(kind="render", title="Render", path="render.png", derives_from=[modelo["id"]])
    return order, keys, {"lamina": lamina, "hoja": hoja, "malla": malla, "modelo": modelo, "render": render}


def test_order_plan_is_made_of_agora_tasks_with_dependencies(agora, tmp_path):
    order, keys, pieces = cartographer(agora, tmp_path)
    full = agora.order({"order_id": order["id"]})["order"]
    assert full["status"] == "producing" and full["keep"] == ["tubo a la izquierda", "al mapa le falta una esquina"]
    assert full["refs"][0]["place"] == "párrafo 3"
    by_title = {j["task"]["title"]: j for j in full["plan"]}
    assert set(by_title) == {"Lámina", "Hoja", "Imagen a 3D", "Vídeo", "Modelo articulado"}
    # parallel branches: the mesh and the video both wait on the sheet
    assert by_title["Imagen a 3D"]["after"] == [keys["sheet"]] and by_title["Vídeo"]["after"] == [keys["sheet"]]
    assert by_title["Imagen a 3D"]["assignee"] == {"agent": "codex-volumen", "model": "sol-6.2", "machine": "pc"}
    task = agora.task({"task_id": keys["mesh"]})["task"] if hasattr(agora, "task") else agora._task_row(keys["mesh"])
    assert f"order-{order['id']}" in (task.get("labels") or [])
    # pieces inherit the details to keep and know where they are
    assert [k["detail"] for k in pieces["hoja"]["keep"]] == ["tubo a la izquierda", "al mapa le falta una esquina"]
    assert pieces["malla"]["view"] == "turntable" and pieces["lamina"]["view"] == "image" and pieces["lamina"]["exists"]
    assert pieces["malla"]["station"] == "vulcan"


def test_check_marks_details_and_defect_goes_back_to_its_job(agora, tmp_path):
    order, keys, pieces = cartographer(agora, tmp_path)
    hoja = pieces["hoja"]["id"]
    out = agora.piece_check({"agent": "codex-volumen", "piece_id": hoja, "note": "abrí la hoja",
                             "keep": [{"detail": "tubo a la izquierda", "ok": True}]})["piece"]
    assert out["status"] == "provisional"   # one detail is still unchecked
    out = agora.piece_check({"agent": "codex-volumen", "piece_id": hoja, "note": "y la esquina",
                             "keep": [{"detail": "al mapa le falta una esquina", "ok": True}]})["piece"]
    assert out["status"] == "checked" and len(out["checks"]) == 2
    defect = agora.piece_defect({"agent": "codex-volumen", "piece_id": pieces["malla"]["id"],
                                 "note": "el pulgar cruza el papel", "view": "espalda", "at": "5"})
    assert defect["piece"]["defects_open"] == 1
    brief = agora.job({"task_id": keys["mesh"]})["job"]
    assert brief["defects"][0]["note"] == "el pulgar cruza el papel" and brief["defects"][0]["at"] == "5"
    thread = agora._task_row(keys["mesh"])["thread_id"]
    said = [m["body"] for m in agora.db.query("SELECT body FROM messages WHERE thread_id=?", (thread,))]
    assert any("el pulgar cruza el papel" in s and "segundo 5" in s for s in said)


def test_gate_keep_fixes_reference_and_cancel_returns_and_stales(agora, tmp_path):
    order, keys, pieces = cartographer(agora, tmp_path)
    gate = agora.gate_open({"agent": "claude", "order_id": order["id"],
                            "piece_ids": [pieces["lamina"]["id"], pieces["hoja"]["id"]]})["gate"]
    mesh_gate = agora.gate_open({"agent": "claude", "order_id": order["id"], "piece_ids": [pieces["malla"]["id"]]})["gate"]
    listing = agora.orders({})
    assert listing["orders"][0]["status"] == "waiting" and len(listing["waiting"]) == 2
    assert listing["waiting"][0]["pieces"][0]["title"] == "Lámina"
    # agents never decide
    with pytest.raises(AgoraError) as no:
        agora.gate_decide({"agent": "claude", "gate_id": gate["id"], "decision": "keep"})
    assert no.value.status == 403
    # the person keeps the concept alone: split it off, then decide
    alone = agora.gate_split(person(gate_id=gate["id"], piece_id=pieces["lamina"]["id"]))["gate"]
    agora.gate_decide(person(gate_id=alone["id"], decision="keep", note="esta es Odila"))
    # and cancels the mesh, with a note
    agora.task_claim({"agent": "codex-volumen", "task_id": keys["mesh"]})
    agora.db.execute("UPDATE tasks SET status='done' WHERE id=?", (keys["mesh"],))
    out = agora.gate_decide(person(gate_id=mesh_gate["id"], decision="cancel", note="es un bulto, no se separa el mapa"))
    assert sorted(out["decided"][0]["stale"]) == sorted([pieces["modelo"]["id"], pieces["render"]["id"]])
    full = agora.order({"order_id": order["id"]})["order"]
    state = {p["title"]: p["status"] for p in full["piece_list"]}
    assert state == {"Lámina": "kept", "Hoja": "provisional", "Malla": "cancelled", "Modelo": "stale", "Render": "stale"}
    assert agora._task_row(keys["mesh"])["status"] == "changes"   # back to its station
    # the order still waits for the sheet's gate
    assert full["status"] == "waiting" and full["gates_open"] == 1
    decisions = agora.order_decisions({"order_id": order["id"]})["decisions"]
    assert [d["decision"] for d in decisions] == ["cancel", "keep"]
    assert {a["title"] for a in decisions[1]["situation"]["alternatives"]} == {"Lámina"}
    # the kept concept is the reference every later job receives; the cancel is a defect of the mesh's job
    brief = agora.job({"task_id": keys["rig"]})["job"]
    assert [r["title"] for r in brief["references"]] == ["Lámina"] and brief["references"][0]["kept_note"] == "esta es Odila"
    assert agora.job({"task_id": keys["mesh"]})["job"]["defects"][0]["source"] == "gate"
    # a piece derived from a kept one carries that decision
    (tmp_path / "variante.png").write_bytes(b"v")
    child = agora.piece_add({"agent": "claude", "order_id": order["id"], "kind": "image", "title": "Variante",
                             "path": "variante.png", "derives_from": [pieces["lamina"]["id"]]})["piece"]
    assert child["decisions"] == [decisions[1]["id"]]
    # deciding the last gate puts the order back in production
    agora.gate_decide(person(gate_id=gate["id"], decision="keep"))
    assert agora.order({"order_id": order["id"]})["order"]["status"] == "producing"


def test_undo_puts_back_each_write(agora, tmp_path):
    order, keys, pieces = cartographer(agora, tmp_path)
    plan = agora.plan_set({"agent": "claude", "order_id": order["id"],
                           "jobs": [{"title": "Figura", "station": "phidias", "after": [keys["rig"]]}]})
    new_task = next(iter(t for t in plan["plan"] if t["task"]["title"] == "Figura"))["task_id"]
    agora.order_undo({"agent": "claude", "token": plan["undo"]})
    assert agora._task_row(new_task)["status"] == "dropped"
    assert all(j["task_id"] != new_task for j in agora.order({"order_id": order["id"]})["order"]["plan"])
    # changing what an existing job waits for is undone too
    moved = agora.plan_set({"agent": "claude", "order_id": order["id"], "jobs": [{"task_id": keys["video"], "after": [keys["concept"]]}]})
    assert agora.order({"order_id": order["id"]})["order"]["plan"][3]["after"] == [keys["concept"]]
    agora.order_undo({"agent": "claude", "token": moved["undo"]})
    assert agora.order({"order_id": order["id"]})["order"]["plan"][3]["after"] == [keys["sheet"]]
    check = agora.piece_check({"agent": "claude", "piece_id": pieces["lamina"]["id"], "note": "ok",
                               "keep": [{"detail": "tubo a la izquierda", "ok": False}]})
    agora.order_undo({"agent": "claude", "token": check["undo"]})
    again = agora.order({"order_id": order["id"]})["order"]
    lamina = next(p for p in again["piece_list"] if p["id"] == pieces["lamina"]["id"])
    assert lamina["checks"] == [] and all(k["state"] == "pending" for k in lamina["keep"])
    gate = agora.gate_open({"agent": "claude", "order_id": order["id"], "piece_ids": [pieces["hoja"]["id"]]})
    assert agora.order({"order_id": order["id"]})["order"]["status"] == "waiting"
    agora.order_undo({"agent": "claude", "token": gate["undo"]})
    assert agora.orders({})["waiting"] == [] and agora.order({"order_id": order["id"]})["order"]["status"] == "producing"
    with pytest.raises(AgoraError):
        agora.order_undo({"agent": "claude", "token": gate["undo"]})


def test_piece_file_serves_only_what_a_piece_names(agora, tmp_path):
    order, keys, pieces = cartographer(agora, tmp_path)
    site = tmp_path / "web"
    site.mkdir()
    (site / "index.html").write_text("<link href=site.css>", encoding="utf-8")
    (site / "site.css").write_text("body{}", encoding="utf-8")
    web = agora.piece_add({"agent": "claude", "order_id": order["id"], "kind": "web", "title": "Web",
                           "path": str(site / "index.html")})["piece"]
    assert web["view"] == "web"
    assert agora_orders.piece_file(agora, web["id"], rel="site.css").endswith("site.css")
    with open(agora_orders.piece_file(agora, pieces["malla"]["id"], preview="1"), "rb") as fh:
        assert fh.read() == b"giro-1.png"   # the kept copy of that preview
    with pytest.raises(AgoraError):
        agora_orders.piece_file(agora, web["id"], rel="../lamina.png")
    (site / "logo.png").write_bytes(b"PNG")
    (site / "app.js").write_text("console.log('</script>')", encoding="utf-8")
    (site / "index.html").write_text('<link rel="stylesheet" href="site.css"><script src="app.js"></script>'
                                     '<img src="logo.png"><img src="../lamina.png"><a href="https://example.org">x</a>',
                                     encoding="utf-8")
    doc = agora_orders.inline_html(str(site / "index.html"))
    assert "<style>body{}</style>" in doc and "data:image/png;base64," in doc and 'src="../lamina.png"' in doc
    assert r"console.log('<\/script>')" in doc and 'src="app.js"' not in doc and "https://example.org" in doc


def test_tools_are_listed_and_the_decision_has_none():
    names = {t["name"] for t in TOOLS}
    for op in ("orders", "order", "order_create", "order_update", "plan_set", "job", "piece_add", "piece_check",
               "piece_defect", "defect_close", "gate_open", "gate_split", "order_decisions", "order_undo"):
        assert f"hub_agora_{op}" in names
    assert "hub_agora_gate_decide" not in names


def test_reference_use_survives_a_cancel_and_material_use_does_not(agora, tmp_path):
    order, keys, pieces = cartographer(agora, tmp_path)
    (tmp_path / "rig.blend").write_bytes(b"hand-built")
    rig = agora.piece_add({"agent": "claude", "order_id": order["id"], "kind": "rigged_model", "title": "Bloqueo",
                           "path": "rig.blend", "derives_from": [{"piece": pieces["malla"]["id"], "role": "reference"},
                                                                 {"piece": pieces["hoja"]["id"], "role": "reference"}]})["piece"]
    assert rig["provenance"] == "known" and {d["role"] for d in rig["derivation"]} == {"reference"}
    gate = agora.gate_open({"agent": "claude", "order_id": order["id"], "piece_ids": [pieces["malla"]["id"]]})["gate"]
    out = agora.gate_decide(person(gate_id=gate["id"], decision="cancel", note="bulto"))
    stale = out["decided"][0]["stale"]
    assert rig["id"] not in stale and pieces["modelo"]["id"] in stale    # the material child goes stale, the reference one does not
    full = {p["id"]: p for p in agora.order({"order_id": order["id"]})["order"]["piece_list"]}
    assert full[rig["id"]]["status"] == "provisional" and "cancelada" in full[rig["id"]]["warnings"][0]


def test_same_output_twice_is_one_piece_and_a_changed_file_is_its_next_revision(agora, tmp_path):
    order, keys, pieces = cartographer(agora, tmp_path)
    mesh = {"agent": "claude", "order_id": order["id"], "kind": "mesh", "title": "Malla", "path": "malla.glb",
            "task_id": keys["mesh"], "derives_from": [pieces["hoja"]["id"]]}
    again = agora.piece_add(dict(mesh))
    assert again["existing"] and again["piece"]["id"] == pieces["malla"]["id"] and again["piece"]["sha256"]
    # the same bytes from another job are a piece of their own, with their own lineage
    other = agora.piece_add({**mesh, "task_id": keys["rig"], "kind": "rigged_model"})
    assert "existing" not in other and other["piece"]["revision"] == 1
    # overwriting the file makes the next revision; the first still shows its own bytes
    (tmp_path / "malla.glb").write_bytes(b"rebuilt")
    second = agora.piece_add(dict(mesh))["piece"]
    assert second["id"] != pieces["malla"]["id"] and second["revision"] == 2 and second["supersedes"] == pieces["malla"]["id"]
    with open(agora_orders.piece_file(agora, pieces["malla"]["id"]), "rb") as fh:
        assert fh.read() == b"malla.glb"
    with open(agora_orders.piece_file(agora, second["id"]), "rb") as fh:
        assert fh.read() == b"rebuilt"
    # an app object: the same object at the same revision is the same piece; a new revision is the next one
    native = {"agent": "claude", "order_id": order["id"], "kind": "text", "title": "Capítulo", "app": "writer",
              "object": "capítulo 1", "place": "párrafo 3", "source_rev": "r7", "task_id": keys["concept"]}
    first = agora.piece_add(dict(native))["piece"]
    assert agora.piece_add(dict(native))["piece"]["id"] == first["id"]
    assert agora.piece_add({**native, "source_rev": "r8"})["piece"]["revision"] == 2
    # inheriting the details never inherits their approval, and a hidden detail is not a failure
    marks = agora.piece_check({"agent": "claude", "piece_id": second["id"], "note": "vista de espalda",
                               "keep": [{"id": "k1", "state": "not_visible", "how": "giro 180"},
                                        {"detail": "al mapa le falta una esquina", "state": "ok", "evidence": "giro-1.png"}]})
    assert marks["piece"]["status"] == "checked"
    assert [k["state"] for k in marks["piece"]["keep"]] == ["not_visible", "ok"] and marks["piece"]["keep"][1]["evidence"] == "giro-1.png"


def test_defect_carries_expected_observed_and_closes_on_a_new_revision(agora, tmp_path):
    order, keys, pieces = cartographer(agora, tmp_path)
    d = agora.piece_defect({"agent": "codex-volumen", "piece_id": pieces["malla"]["id"], "note": "mapa fundido",
                            "expected": "mapa separado", "observed": "una sola malla", "evidence": "giro-1.png", "view": "frente"})
    (tmp_path / "malla2.glb").write_bytes(b"new")
    fixed = agora.piece_add({"agent": "codex-volumen", "order_id": order["id"], "kind": "mesh", "title": "Malla 2",
                             "path": "malla2.glb", "supersedes": pieces["malla"]["id"]})["piece"]
    assert fixed["revision"] == 2
    out = agora.defect_close({"agent": "codex-volumen", "defect_id": d["defect_id"], "fixed_in": fixed["id"],
                              "note": "mismo giro: el mapa llega separado"})
    closed = out["defect"]
    assert closed["status"] == "closed" and closed["closed_in"] == fixed["id"] and closed["expected"] == "mapa separado"
    assert agora.job({"task_id": keys["mesh"]})["job"]["defects"] == []
    agora.order_undo({"agent": "codex-volumen", "token": out["undo"]})
    assert agora.job({"task_id": keys["mesh"]})["job"]["defects"][0]["id"] == d["defect_id"]


def test_a_piece_whose_file_changed_without_a_copy_is_not_decided(agora, tmp_path, monkeypatch):
    order, keys, pieces = cartographer(agora, tmp_path)
    monkeypatch.setattr(agora_orders, "STORE_MAX", 0)     # too big to keep a copy
    (tmp_path / "big.glb").write_bytes(b"A")
    big = agora.piece_add({"agent": "claude", "order_id": order["id"], "kind": "mesh", "title": "Grande", "path": "big.glb"})["piece"]
    assert big["bytes"] == "unverified"
    gate = agora.gate_open({"agent": "claude", "order_id": order["id"], "piece_ids": [big["id"]]})["gate"]
    (tmp_path / "big.glb").write_bytes(b"B")
    with pytest.raises(AgoraError) as changed:
        agora.gate_decide(person(gate_id=gate["id"], decision="keep"))
    assert changed.value.status == 409
    with pytest.raises(AgoraError):
        agora_orders.piece_file(agora, big["id"])
    (tmp_path / "big.glb").write_bytes(b"A")
    assert agora.gate_decide(person(gate_id=gate["id"], decision="keep"))["ok"]


def test_a_web_page_is_a_new_revision_when_only_its_image_changes(agora, tmp_path):
    order, keys, pieces = cartographer(agora, tmp_path)
    for name in ("a", "b"):
        site = tmp_path / f"site-{name}"
        site.mkdir()
        (site / "index.html").write_text('<img src="foto.png">', encoding="utf-8")
    (tmp_path / "site-a" / "foto.png").write_bytes(b"A")
    (tmp_path / "site-b" / "foto.png").write_bytes(b"other site")
    web = {"agent": "claude", "order_id": order["id"], "kind": "web", "title": "Web", "path": "site-a/index.html"}
    r1 = agora.piece_add(dict(web))["piece"]
    (tmp_path / "site-a" / "foto.png").write_bytes(b"B")       # same entry page, another image
    r2 = agora.piece_add(dict(web))["piece"]
    assert r2["id"] != r1["id"] and r2["revision"] == 2 and r2["sha256"] != r1["sha256"]
    show = lambda pid: open(agora_orders.piece_file(agora, pid, rel="foto.png"), "rb").read()
    assert show(r1["id"]) == b"A" and show(r2["id"]) == b"B"
    assert "QQ==" in agora_orders.inline_html(agora_orders.piece_file(agora, r1["id"]))   # base64 of A
    # another folder with the same entry page is another site
    other = agora.piece_add({**web, "path": "site-b/index.html"})["piece"]
    assert other["sha256"] not in (r1["sha256"], r2["sha256"]) and show(other["id"]) == b"other site"
