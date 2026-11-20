"""Draft pass (pasada 0): variants of the whole chain in draft, the person's early gate and the anchors of the full pass."""
import pytest

from hoard_link.hub.agora import Agora, AgoraError, TOOLS
from hoard_link.hub.config import PERSON_ALIAS as PERSON
from hoard_link.hub import agora_drafts


@pytest.fixture()
def agora(tmp_path):
    ag = Agora(tmp_path / "agora.db")
    ag.space_create({"agent": "claude", "id": "estudio", "title": "Estudio"})
    view = ag.for_space("estudio")
    yield view
    ag.close()


def person(**kw):
    return {"agent": PERSON, "_person": True, **kw}


def shown(ag, variant_id):
    """The signature of a variant as the person's page shows it."""
    order_id = ag.db.scalar("SELECT order_id FROM agora_draft_variants WHERE id=?", (variant_id,), 0)
    for p in ag.draft_passes({"order_id": order_id})["draft_passes"]:
        for v in p["variants"]:
            if v["id"] == variant_id:
                return v["signature"]
    raise AssertionError(f"variant {variant_id} not shown")


def decide(ag, variant_id, decision, note=""):
    return ag.draft_decide(person(variant_id=variant_id, decision=decision, note=note, signature=shown(ag, variant_id)))


GESTURES = [("a", "Desenrollar el mapa"), ("b", "Voltearlo con una mano"), ("c", "Pasarlo de una mano a la otra")]
ANCHORS = ("blocking", "depth", "normals", "keyframe_start", "keyframe_end", "composition")


def draft_pass(ag, folder, *, jobs=False):
    """The cartographer's order, a draft pass of three gestures, and the pieces of each variant by anchor."""
    order = ag.order_create(person(title="La cartógrafa", words="Sube tres escalones bajo la lluvia y despliega un mapa.",
                                   keep=["tubo a la izquierda", "al mapa le falta una esquina"], folder=str(folder)))["order"]
    variants = []
    for key, title in GESTURES:
        v = {"key": key, "title": title, "idea": f"gesto {key}"}
        if jobs:
            v["jobs"] = [{"key": "bloqueo", "title": "Bloqueo", "station": "vulcan", "produces": ["blocking"]},
                         {"key": "imagen", "title": "Imagen", "station": "prospero", "produces": ["image"], "after": ["bloqueo"]}]
        variants.append(v)
    opened = ag.draft_pass_open({"agent": "claude-pasada0", "order_id": order["id"], "variants": variants,
                                 "baseline": {"seconds": 1800, "gpu_seconds": 900, "note": "sonda completa"}})
    pieces = {}
    for v in opened["draft_pass"]["variants"]:
        ids = {}
        for anchor in ANCHOR_FILES:
            name = f"{v['key']}-{anchor}.png"
            (folder / name).write_bytes(name.encode())
            kind = "blocking" if anchor in ("blocking",) else "image"
            ids[anchor] = ag.piece_add({"agent": "claude-pasada0", "order_id": order["id"], "kind": kind,
                                        "title": f"{v['title']} · {anchor}", "path": name})["piece"]["id"]
        pieces[v["key"]] = ids
    return order, opened, pieces


ANCHOR_FILES = ANCHORS


def ready(ag, opened, pieces, seconds=(20, 60, 70)):
    for v, s in zip(opened["draft_pass"]["variants"], seconds):
        ag.draft_variant_set({"agent": "claude-pasada0", "variant_id": v["id"], "status": "ready",
                              "measures": {"seconds": s, "gpu_seconds": s - 10, "gpu": "RTX 5060 Ti"},
                              "pieces": [{"piece": pid, "anchor": a} for a, pid in pieces[v["key"]].items()]})


def test_a_draft_pass_has_variants_with_pieces_by_anchor_and_measures(agora, tmp_path):
    order, opened, pieces = draft_pass(agora, tmp_path)
    ready(agora, opened, pieces)
    out = agora.draft_passes({"order_id": order["id"]})["draft_passes"][0]
    assert [v["key"] for v in out["variants"]] == ["a", "b", "c"] and out["status"] == "drafting"
    assert [p["anchor"] for p in out["variants"][0]["pieces"]] == list(ANCHORS)
    assert out["totals"] == {"seconds": 150.0, "gpu_seconds": 120.0, "variants": 3,
                             "share_of_baseline": round(150 / 1800, 3), "gpu_share_of_baseline": round(120 / 900, 3)}


def test_a_variant_without_pieces_cannot_be_ready_and_the_gate_waits_for_every_variant(agora, tmp_path):
    order, opened, pieces = draft_pass(agora, tmp_path)
    first = opened["draft_pass"]["variants"][0]
    with pytest.raises(AgoraError):
        agora.draft_variant_set({"agent": "claude-pasada0", "variant_id": first["id"], "status": "ready"})
    agora.draft_variant_set({"agent": "claude-pasada0", "variant_id": first["id"], "status": "ready",
                             "pieces": [pieces["a"]["blocking"]]})
    with pytest.raises(AgoraError) as err:
        agora.draft_gate_open({"agent": "claude-pasada0", "pass_id": opened["draft_pass"]["id"]})
    assert err.value.status == 409 and "b, c" in str(err.value)
    with pytest.raises(AgoraError):   # a piece of another order is refused
        other = agora.order_create(person(title="Otra"))["order"]
        (tmp_path / "x.png").write_bytes(b"x")
        foreign = agora.piece_add({"agent": "claude", "order_id": other["id"], "kind": "image", "title": "x",
                                   "path": str(tmp_path / "x.png")})["piece"]["id"]
        agora.draft_variant_set({"agent": "claude-pasada0", "variant_id": first["id"], "pieces": [foreign]})


def test_the_early_gate_makes_the_order_wait_and_only_the_person_decides(agora, tmp_path):
    order, opened, pieces = draft_pass(agora, tmp_path)
    ready(agora, opened, pieces)
    agora.draft_gate_open({"agent": "claude-pasada0", "pass_id": opened["draft_pass"]["id"]})
    assert agora.order({"order_id": order["id"]})["order"]["status"] == "waiting"
    waiting = agora.draft_passes({"all_spaces": True})["draft_passes"]
    assert [p["id"] for p in waiting] == [opened["draft_pass"]["id"]]
    b = opened["draft_pass"]["variants"][1]
    with pytest.raises(AgoraError) as err:
        agora.draft_decide({"agent": "claude", "variant_id": b["id"], "decision": "keep"})
    assert err.value.status == 403
    # the tool list has no tool for the decision
    assert "hub_agora_draft_decide" not in {t["name"] for t in TOOLS}
    assert {"hub_agora_draft_pass_open", "hub_agora_draft_variant_set", "hub_agora_draft_gate_open",
            "hub_agora_draft_passes", "hub_agora_draft_anchors"} <= {t["name"] for t in TOOLS}


def test_keeping_one_variant_discards_the_rest_with_its_note_and_deletes_nothing(agora, tmp_path):
    order, opened, pieces = draft_pass(agora, tmp_path)
    ready(agora, opened, pieces)
    pid = opened["draft_pass"]["id"]
    agora.draft_gate_open({"agent": "claude-pasada0", "pass_id": pid})
    a, b, c = opened["draft_pass"]["variants"]
    out = decide(agora, c["id"], "cancel", "el traspaso tapa la cara")
    assert out["discarded"] == [c["id"]] and out["draft_pass"]["status"] == "shown"
    out = decide(agora, b["id"], "keep", "me quedo con el volteo")
    assert out["kept"] == b["id"] and out["discarded"] == [a["id"]]
    final = agora.draft_passes({"order_id": order["id"]})["draft_passes"][0]
    assert final["status"] == "decided" and final["kept_variant"] == b["id"]
    by = {v["key"]: v for v in final["variants"]}
    assert by["c"]["status"] == "discarded" and by["c"]["decision_note"] == "el traspaso tapa la cara"
    assert by["a"]["status"] == "discarded" and "Voltearlo con una mano" in by["a"]["decision_note"]
    assert {p["status"] for p in by["b"]["pieces"]} == {"kept"}
    assert {p["status"] for p in by["a"]["pieces"]} == {"cancelled"}       # kept on record, not deleted
    assert len(by["a"]["pieces"]) == len(ANCHORS)
    full = agora.order({"order_id": order["id"]})["order"]
    assert full["status"] == "producing"
    # nothing was sent back to a station: a discarded draft is not a defect
    assert all(p["defects_open"] == 0 for p in full["piece_list"])
    decisions = agora.order_decisions({"order_id": order["id"]})["decisions"]
    assert {d["situation"]["draft_pass"]["id"] for d in decisions} == {pid}
    assert sum(1 for d in decisions if d["decision"] == "keep") == len(ANCHORS)


def test_the_full_pass_reads_its_anchors_from_the_kept_variant(agora, tmp_path):
    order, opened, pieces = draft_pass(agora, tmp_path)
    assert agora.draft_anchors({"order_id": order["id"]})["variant"] is None
    ready(agora, opened, pieces)
    agora.draft_gate_open({"agent": "claude-pasada0", "pass_id": opened["draft_pass"]["id"]})
    waiting = agora.draft_anchors({"order_id": order["id"]})
    assert waiting["variant"] is None and waiting["waiting"] is True
    b = opened["draft_pass"]["variants"][1]
    decide(agora, b["id"], "keep")
    out = agora.draft_anchors({"order_id": order["id"]})
    assert out["variant"]["key"] == "b"
    assert set(out["guides"]["image"]) == {"depth", "normals", "composition"}
    assert set(out["guides"]["video"]) == {"keyframe_start", "keyframe_end"}
    assert out["guides"]["split"]["blocking"][0]["piece"] == pieces["b"]["blocking"]
    roles = {d["piece"]: d["role"] for d in out["derives_from"]}
    assert roles[pieces["b"]["blocking"]] == "material" and roles[pieces["b"]["depth"]] == "reference"
    # a piece of the full pass registered with those derives_from carries the person's decision
    (tmp_path / "hoja.png").write_bytes(b"hoja")
    sheet = agora.piece_add({"agent": "claude-pasada0", "order_id": order["id"], "kind": "character_sheet",
                             "title": "Hoja", "path": "hoja.png", "derives_from": out["derives_from"]})["piece"]
    assert sheet["decisions"] and set(sheet["derives_from"]) == set(pieces["b"].values())


def test_variant_jobs_are_order_tasks_on_their_branch_and_one_undo_puts_everything_back(agora, tmp_path):
    order, opened, pieces = draft_pass(agora, tmp_path, jobs=True)
    plan = agora.order({"order_id": order["id"]})["order"]["plan"]
    assert len(plan) == 6 and {j["branch"] for j in plan} == {"pasada0-a", "pasada0-b", "pasada0-c"}
    a = opened["draft_pass"]["variants"][0]
    assert len(a["jobs"]) == 2 and a["tasks"][0]["title"].startswith("[Pasada 0 · a]")
    image_job = next(j for j in plan if j["task_id"] == a["jobs"][1])
    assert image_job["after"] == [a["jobs"][0]]
    agora.order_undo({"agent": "claude-pasada0", "token": opened["undo"]})
    assert agora.draft_passes({"order_id": order["id"]})["draft_passes"] == []
    assert not agora.db.scalar("SELECT COUNT(*) FROM agora_draft_variants", (), 0)
    assert {agora._task_row(t)["status"] for t in a["jobs"]} == {"dropped"}


def test_visual_draft_plan_builds_guides_before_keys_and_keeps_variants_independent(agora):
    order_id = agora.order_create(person(title="El gesto del mapa"))["order"]["id"]
    opened = agora.draft_pass_open({"agent": "producer", "order_id": order_id, "template": "visual", "variants": [
        {"key": "pass", "title": "Pasar el mapa", "idea": "De la mano izquierda a la derecha."},
        {"key": "flip", "title": "Voltear el mapa"},
        {"key": "manual", "title": "Otro gesto", "jobs": [{"title": "Mi propio esquema", "station": "writer"}]},
        {"key": "pieces", "title": "Piezas existentes", "jobs": []},
    ]})
    plan = {job["task_id"]: job for job in agora.order({"order_id": order_id})["order"]["plan"]}
    pass_variant, flip_variant, manual, pieces = opened["draft_pass"]["variants"]
    for variant in (pass_variant, flip_variant):
        outline, blocking, keys, shot, figure = variant["jobs"]
        assert [plan[j]["station"] for j in variant["jobs"]] == ["writer", "vulcan", "prospero", "prospero", "phidias"]
        assert [set(plan[j]["after"]) for j in variant["jobs"]] == [
            set(), {outline}, {blocking}, {keys}, {blocking, keys}
        ]
        assert {plan[j]["branch"] for j in variant["jobs"]} == {f"pasada0-{variant['key']}"}
    assert "De la mano izquierda a la derecha." in agora._task_row(pass_variant["jobs"][0])["body"]
    assert len(manual["jobs"]) == 1 and pieces["jobs"] == []
    assert opened["draft_pass"]["status"] == "drafting"
    agora.order_undo({"agent": "producer", "token": opened["undo"]})
    assert agora.order({"order_id": order_id})["order"]["plan"] == []
    assert all(agora._task_row(task_id)["status"] == "dropped" for task_id in plan)


def test_variant_set_undo_restores_pieces_and_state(agora, tmp_path):
    order, opened, pieces = draft_pass(agora, tmp_path)
    v = opened["draft_pass"]["variants"][0]
    first = agora.draft_variant_set({"agent": "claude-pasada0", "variant_id": v["id"], "pieces": [pieces["a"]["depth"]]})
    second = agora.draft_variant_set({"agent": "claude-pasada0", "variant_id": v["id"], "status": "ready",
                                      "measures": {"seconds": 12}, "remove": [pieces["a"]["depth"]],
                                      "pieces": [{"piece": pieces["a"]["blocking"], "anchor": "blocking"}]})
    assert [p["anchor"] for p in second["variant"]["pieces"]] == ["blocking"]
    agora.order_undo({"agent": "claude-pasada0", "token": second["undo"]})
    back = agora.draft_passes({"order_id": order["id"]})["draft_passes"][0]["variants"][0]
    assert back["status"] == "drafting" and back["measures"] == {}
    assert [p["id"] for p in back["pieces"]] == [pieces["a"]["depth"]]
    with pytest.raises(AgoraError):
        agora.draft_variant_set({"agent": "claude-pasada0", "variant_id": v["id"], "status": "kept"})
    with pytest.raises(AgoraError):
        agora.draft_variant_set({"agent": "claude-pasada0", "variant_id": v["id"], "measures": {"seconds": -1}})
    assert first["undo"]


def shared_stair(ag, folder, order, opened, pieces):
    """B and C share the blocking of the staircase; each keeps its own map."""
    (folder / "escalera.png").write_bytes(b"escalera")
    stair = ag.piece_add({"agent": "claude-pasada0", "order_id": order["id"], "kind": "blocking",
                          "title": "Bloqueo de la escalera", "path": "escalera.png"})["piece"]["id"]
    _, b, c = opened["draft_pass"]["variants"]
    for v in (b, c):
        ag.draft_variant_set({"agent": "claude-pasada0", "variant_id": v["id"],
                              "pieces": [{"piece": stair, "anchor": "blocking"}]})
    (folder / "hoja-de-b.png").write_bytes(b"hoja b")
    sheet = ag.piece_add({"agent": "claude-pasada0", "order_id": order["id"], "kind": "character_sheet", "title": "Hoja",
                          "path": "hoja-de-b.png", "derives_from": [{"piece": stair, "role": "material"}]})["piece"]["id"]
    return stair, sheet


def piece_status(ag, pid):
    return ag.db.one("SELECT status FROM agora_pieces WHERE id=?", (pid,))["status"]


@pytest.mark.parametrize("c_first", [False, True])
def test_a_piece_shared_by_two_variants_stays_with_the_kept_one_in_any_order(agora, tmp_path, c_first):
    order, opened, pieces = draft_pass(agora, tmp_path)
    ready(agora, opened, pieces)
    stair, sheet = shared_stair(agora, tmp_path, order, opened, pieces)
    agora.draft_gate_open({"agent": "claude-pasada0", "pass_id": opened["draft_pass"]["id"]})
    a, b, c = opened["draft_pass"]["variants"]
    if c_first:
        out = decide(agora, c["id"], "cancel", "el traspaso tapa la cara")
        assert out["shared"] == [stair]
        assert piece_status(agora, stair) == "provisional"            # still B's, undecided
    decide(agora, b["id"], "keep")
    assert piece_status(agora, stair) == "kept"
    assert piece_status(agora, sheet) != "stale"                       # what derives from it is still good
    assert piece_status(agora, pieces["b"]["keyframe_end"]) == "kept"
    assert piece_status(agora, pieces["c"]["keyframe_end"]) == "cancelled"   # C's own map is discarded
    assert piece_status(agora, pieces["a"]["blocking"]) == "cancelled"
    decisions = agora.order_decisions({"order_id": order["id"]})["decisions"]
    assert {d["decision"] for d in decisions if d["piece_id"] == stair} == {"keep"}
    anchors = agora.draft_anchors({"order_id": order["id"]})
    assert stair in [p["piece"] for p in anchors["anchors"]["blocking"]]


def test_a_piece_shared_only_by_discarded_variants_is_cancelled(agora, tmp_path):
    order, opened, pieces = draft_pass(agora, tmp_path)
    ready(agora, opened, pieces)
    stair, sheet = shared_stair(agora, tmp_path, order, opened, pieces)
    agora.draft_gate_open({"agent": "claude-pasada0", "pass_id": opened["draft_pass"]["id"]})
    a, b, c = opened["draft_pass"]["variants"]
    decide(agora, a["id"], "keep")
    assert piece_status(agora, stair) == "cancelled" and piece_status(agora, sheet) == "stale"


def test_a_variant_changed_while_shown_has_to_be_shown_again_before_it_is_decided(agora, tmp_path):
    order, opened, pieces = draft_pass(agora, tmp_path)
    ready(agora, opened, pieces)
    agora.draft_gate_open({"agent": "claude-pasada0", "pass_id": opened["draft_pass"]["id"]})
    b = opened["draft_pass"]["variants"][1]
    seen = shown(agora, b["id"])                          # the person's tab shows B
    (tmp_path / "b-fin-2.png").write_bytes(b"otro final")
    new_end = agora.piece_add({"agent": "claude-pasada0", "order_id": order["id"], "kind": "image",
                               "title": "Voltearlo · final nuevo", "path": "b-fin-2.png"})["piece"]["id"]
    changed = agora.draft_variant_set({"agent": "claude-pasada0", "variant_id": b["id"],
                                       "remove": [pieces["b"]["keyframe_end"]],
                                       "pieces": [{"piece": new_end, "anchor": "keyframe_end"}]})
    assert changed["variant"]["signature"] != seen and changed["shown_again"] is True
    with pytest.raises(AgoraError) as err:                # the old tab cannot keep the new frames
        agora.draft_decide(person(variant_id=b["id"], decision="keep", signature=seen))
    assert err.value.status == 409 and err.value.extra.get("error_code") == "variant_changed"
    with pytest.raises(AgoraError) as err:                # nor can a decision that does not say what it saw
        agora.draft_decide(person(variant_id=b["id"], decision="keep"))
    assert err.value.status == 400
    assert piece_status(agora, new_end) == "provisional"
    out = agora.draft_decide(person(variant_id=b["id"], decision="keep", signature=changed["variant"]["signature"]))
    assert out["kept"] == b["id"] and piece_status(agora, new_end) == "kept"
    assert piece_status(agora, pieces["b"]["keyframe_end"]) == "provisional"   # removed before the decision


def test_the_signature_follows_the_bytes_shown_not_the_checks(agora, tmp_path):
    order, opened, pieces = draft_pass(agora, tmp_path)
    ready(agora, opened, pieces)
    b = opened["draft_pass"]["variants"][1]
    before = shown(agora, b["id"])
    agora.draft_variant_set({"agent": "claude-pasada0", "variant_id": b["id"], "measures": {"seconds": 99}})
    assert shown(agora, b["id"]) == before                # a measure is not what the person judges
    agora.draft_variant_set({"agent": "claude-pasada0", "variant_id": b["id"],
                             "pieces": [{"piece": pieces["b"]["depth"], "anchor": "normals"}]})
    assert shown(agora, b["id"]) != before                # a piece under another anchor is
