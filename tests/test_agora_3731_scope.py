from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from hoard_link.hub.agora import Agora
from hoard_link.hub.piece_checks import PieceChecksFacet


def test_worker_validates_only_declared_registered_snapshots(tmp_path):
    with closing(Agora(tmp_path / "agora.db")) as agora:
        actor = "worker-fixture"
        order = agora.order_create({"agent": actor, "title": "Fixture", "words": "Inspect an image."})["order"]
        path = tmp_path / "sheet.png"
        Image.new("RGB", (20, 16), "white").save(path)
        piece = agora.piece_add({"agent": actor, "order_id": order["id"], "title": "Image",
                                 "kind": "image", "path": str(path)})["piece"]
        facet = PieceChecksFacet(SimpleNamespace(facet=lambda name: SimpleNamespace(agora=agora)))
        args = {"piece_id": piece["id"], "profile": "image"}
        assert facet.validate(args)["ok"]
        assert not facet.validate({**args, "_worker_read_files": []})["ok"]
        scoped = {**args, "_worker_read_files": [piece["stored"]]}
        assert facet.validate(scoped)["measures"]["images"][0]["size_px"] == [20, 16]
        Path(piece["stored"]).unlink()
        assert path.is_file()
        assert not facet.validate(scoped)["ok"]
