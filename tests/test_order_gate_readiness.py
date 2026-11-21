from urllib.parse import quote

import pytest

from hoard_link.hub.agora import Agora, AgoraError


ACTOR = {"agent": "builder"}


@pytest.fixture()
def production(tmp_path):
    agora = Agora(tmp_path / "agora.db")
    order = agora.order_create({**ACTOR, "title": "Map turn", "keep": ["Short cape"]})["order"]
    agora.plan_set({**ACTOR, "order_id": order["id"], "jobs": [{"key": "video", "title": "Turn the map"}]})
    try:
        yield agora, order["id"]
    finally:
        agora.close()


def piece(production, title="Map turn", note=""):
    agora, order_id = production
    return agora.piece_add({**ACTOR, "order_id": order_id, "kind": "image", "title": title,
                           "note": note, "url": f"https://example.test/{quote(title)}.png"})["piece"]


def check(production, candidate, note="The short cape is visible.", state="ok", **extra):
    agora, _ = production
    return agora.piece_check({**ACTOR, "piece_id": candidate["id"], "note": note,
                             "keep": [{"detail": "Short cape", "state": state}] if state else [], **extra})


def open_gate(production, *candidates, note=""):
    agora, order_id = production
    return agora.gate_open({**ACTOR, "order_id": order_id, "piece_ids": [p["id"] for p in candidates], "note": note})


def refused(production, code, *candidates, note=""):
    agora, order_id = production
    before = len(agora.db.query("SELECT token FROM agora_order_journal"))
    with pytest.raises(AgoraError) as rejection:
        open_gate(production, *candidates, note=note)
    error = rejection.value
    assert error.status == 409 and error.extra["code"] == code
    assert error.extra["message_es"] and error.extra["message_en"]
    assert agora.orders({})["waiting"] == []
    assert agora.order({"order_id": order_id})["order"]["status"] == "producing"
    assert len(agora.db.query("SELECT token FROM agora_order_journal")) == before
    return error.extra


def test_every_piece_must_pass_and_details_are_rechecked(production):
    first, second = piece(production, "First key"), piece(production, "Final key")
    check(production, first)
    assert refused(production, "piece_unchecked", first, second)["piece_id"] == second["id"]
    check(production, second, state=None)
    assert refused(production, "piece_details_unchecked", first, second)["detail_ids"] == ["k1"]
    check(production, second, ok=False)
    refused(production, "piece_check_failed", first, second)
    check(production, second, state="missing")
    refused(production, "piece_details_unchecked", first, second)
    check(production, second, note="Cape is outside this close-up.", state="not_visible")
    gate = open_gate(production, first, second)
    assert gate["gate"]["status"] == "open" and gate["gate"]["decisions"] == []
    agora, order_id = production
    assert agora.order_undo({**ACTOR, "token": gate["undo"]})["ok"]
    assert agora.order({"order_id": order_id})["order"]["status"] == "producing"


@pytest.mark.parametrize("note", ["Pendiente de comprobación visual del giro.", "The turn still needs to be checked."])
def test_passing_status_cannot_hide_pending_work(production, note):
    candidate = piece(production, note=note)
    pending = check(production, candidate, note=note)["piece"]
    assert pending["status"] == "checked"
    check_id = pending["checks"][0]["id"]
    check(production, candidate)
    assert refused(production, "piece_check_pending", candidate)["pending"] == ["piece_note", check_id]
    other = piece(production, "Other")
    with pytest.raises(AgoraError, match="pending check IDs"):
        check(production, other, resolves_pending=[check_id])
    with pytest.raises(AgoraError, match="Only a passing check"):
        check(production, candidate, ok=False, resolves_pending=[check_id])
    closed = check(production, candidate, note="Miré el giro completo con el visor a 0,7 s.",
                   resolves_pending=["piece_note", check_id])
    assert closed["piece"]["checks"][-1]["resolves_pending"] == ["piece_note", check_id]
    agora, _ = production
    agora.order_undo({**ACTOR, "token": closed["undo"]})
    refused(production, "piece_check_pending", candidate)
    check(production, candidate, resolves_pending=["piece_note", check_id])
    assert open_gate(production, candidate)["ok"]


@pytest.mark.parametrize("note", [
    "El fotograma final conserva una pequeña mancha junto a una bota.",
    "A small dark spot remains near the boot.",
])
def test_gate_note_cannot_be_the_only_place_with_a_defect(production, note):
    candidate = piece(production)
    check(production, candidate)
    refused(production, "gate_undisclosed_defect", candidate, note=note)
    refused(production, "gate_note_pending", candidate, note="Pendiente de comprobar el giro.")
    assert open_gate(production, candidate, note="Comparar el mapa al que le falta una esquina. Sin defectos.")["ok"]


def test_only_a_non_required_defect_disclosed_on_the_piece_can_be_shown(production):
    caption = "Mancha junto a una bota"
    hidden = piece(production, note=f"Giro completo.\n{caption}")
    disclosed = piece(production, f"Giro · {caption}", f"{caption}.\nGiro completo.")
    for candidate in (hidden, disclosed):
        check(production, candidate)
    agora, order_id = production
    hidden_report = agora.piece_defect({**ACTOR, "piece_id": hidden["id"], "note": caption, "required": False})
    assert refused(production, "piece_undisclosed_defect", hidden)["defect_id"] == hidden_report["defect_id"]
    with pytest.raises(AgoraError, match="must be true or false"):
        agora.piece_defect({**ACTOR, "piece_id": disclosed["id"], "note": caption, "required": "false"})
    required = agora.piece_defect({**ACTOR, "piece_id": disclosed["id"], "note": caption})
    assert refused(production, "piece_required_defect", disclosed)["defect_id"] == required["defect_id"]
    agora.order_undo({**ACTOR, "token": required["undo"]})
    optional = agora.piece_defect({**ACTOR, "piece_id": disclosed["id"], "note": caption, "required": False})
    refused(production, "gate_undisclosed_defect", disclosed, note=f"{caption}. También hay un arañazo.")
    open_gate(production, disclosed, note=f"Detalle visible: {caption}.")
    shown = agora.order({"order_id": order_id})["order"]["gates"][0]["pieces"][0]
    assert shown["title"] == disclosed["title"]
    assert shown["defects_open"] == 1
    assert optional["piece"]["defects"][0]["required"] == 0


def test_fix_on_another_revision_does_not_make_the_defective_piece_ready(production):
    original = piece(production)
    corrected = piece(production, "Corrected turn")
    check(production, original)
    check(production, corrected)
    agora, _ = production
    defect = agora.piece_defect({**ACTOR, "piece_id": original["id"], "note": "The cape is too long."})
    agora.defect_close({**ACTOR, "defect_id": defect["defect_id"], "fixed_in": corrected["id"],
                        "note": "Checked the cape on the replacement."})
    refused(production, "piece_required_defect", original)
    assert open_gate(production, corrected)["ok"]
