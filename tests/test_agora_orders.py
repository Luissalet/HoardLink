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
        (folder / name).write_bytes(b"x")
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
    child = agora.piece_add({"agent": "claude", "order_id": order["id"], "kind": "image", "title": "Variante",
                             "path": "lamina.png", "derives_from": [pieces["lamina"]["id"]]})["piece"]
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
    assert agora_orders.piece_file(agora, pieces["malla"]["id"], preview="1").endswith("giro-1.png")
    with pytest.raises(AgoraError):
        agora_orders.piece_file(agora, web["id"], rel="../lamina.png")


def test_tools_are_listed_and_the_decision_has_none():
    names = {t["name"] for t in TOOLS}
    for op in ("orders", "order", "order_create", "order_update", "plan_set", "job", "piece_add", "piece_check",
               "piece_defect", "gate_open", "gate_split", "order_decisions", "order_undo"):
        assert f"hub_agora_{op}" in names
    assert "hub_agora_gate_decide" not in names
