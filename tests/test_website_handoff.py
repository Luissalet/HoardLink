from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from hoard_link.hub.agora import Agora
from hoard_link.hub import website_handoff


@pytest.fixture
def order_source(tmp_path):
    root = Agora(tmp_path / "agora.db")
    root.space_create({"agent": "director", "id": "studio", "title": "Studio"})
    agora = root.for_space("studio")
    source = tmp_path / "source"
    source.mkdir()
    public = source / "public"
    public.mkdir()
    (public / "index.html").write_text("<h1>Original</h1>", encoding="utf-8")
    (public / "style.css").write_text("h1{color:red}", encoding="utf-8")
    bundle = source / "site.zip"
    with ZipFile(bundle, "w") as archive:
        archive.write(public / "index.html", "public/index.html")
    order = agora.order_create({"agent": "coder", "title": "A site", "words": "Make a site",
                               "folder": str(source)})["order"]

    def add(path, kind, **kwargs):
        return agora.piece_add({"agent": "coder", "order_id": order["id"], "path": path,
                                "kind": kind, "title": kind, **kwargs})["piece"]

    web = add("public/index.html", "web")
    package = add("site.zip", "data", derives_from=[web["id"]])
    hub = SimpleNamespace(facet=lambda name: SimpleNamespace(agora=root),
                          call_app=Mock(return_value={"ok": True, "result": {"undo_token": "publication-undo"}}))
    try:
        yield hub, agora, add, web, package
    finally:
        root.close()


def test_website_handoff_uses_registered_zip_bytes_with_lineage(order_source):
    hub, agora, _, web, package = order_source
    Path(package["path"]).write_bytes(b"new unregistered content")
    before = agora.order({"order_id": web["order_id"]})
    args = {"space_id": "studio", "piece_id": web["id"], "revision": web["revision"],
            "package_piece_id": package["id"], "slug": "demo", "start_with_hub": False}
    reply = website_handoff.publish_piece(hub, args)
    assert reply["result"]["undo_token"] == "publication-undo"
    app, tool, payload = hub.call_app.call_args.args
    assert (app, tool) == ("mercator", "website_publish")
    assert payload["source"] == package["stored"]
    assert sha256(Path(payload["source"]).read_bytes()).hexdigest() == package["sha256"] == payload["source_sha256"]
    assert payload["provenance"] == {"space_id": "studio", "order_id": web["order_id"],
                                    "web": {key: web[key] for key in ("id", "revision", "sha256")},
                                    "package": {key: package[key] for key in ("id", "revision", "sha256")}}
    assert not payload["start_with_hub"]
    assert agora.order({"order_id": web["order_id"]}) == before


def test_website_handoff_uses_retained_folder_and_rejects_changed_snapshot(order_source):
    hub, _, _, web, _ = order_source
    Path(web["path"]).with_name("style.css").write_text("h1{color:green}", encoding="utf-8")
    args = {"space_id": "studio", "piece_id": web["id"], "revision": web["revision"], "slug": "demo"}
    assert website_handoff.publish_piece(hub, args)["ok"]
    folder = Path(hub.call_app.call_args.args[2]["source"])
    assert folder == Path(web["stored"]).parent
    assert (folder / "style.css").read_text(encoding="utf-8") == "h1{color:red}"
    (folder / "style.css").write_text("changed retained bytes", encoding="utf-8")
    assert website_handoff.publish_piece(hub, args)["code"] == "piece_changed"
    assert hub.call_app.call_count == 1


def test_website_handoff_forwards_the_form_without_changing_the_registered_piece(order_source):
    hub, agora, _, web, _ = order_source
    before = agora.order({"order_id": web["order_id"]})
    form = {"path": "/api/encargos", "fields": {"details": "Synthetic"}, "marker_field": "details"}
    args = {"space_id": "studio", "piece_id": web["id"], "revision": web["revision"], "slug": "demo", "form": form}
    assert website_handoff.publish_piece(hub, args)["ok"]
    payload = hub.call_app.call_args.args[2]
    assert payload["form"] == form
    assert payload["provenance"]["web"]["sha256"] == web["sha256"]
    assert not (Path(payload["source"]) / "website.json").exists()
    assert agora.order({"order_id": web["order_id"]}) == before


def test_website_handoff_rejects_wrong_revision_space_and_unlinked_package(order_source):
    hub, _, add, web, _ = order_source
    unrelated = add("site.zip", "web-package")
    args = {"space_id": "studio", "piece_id": web["id"], "revision": web["revision"], "slug": "demo"}
    for update in ({"revision": web["revision"] + 1}, {"space_id": "ecosystem"}, {"package_piece_id": unrelated["id"]}):
        assert not website_handoff.publish_piece(hub, {**args, **update})["ok"]
    hub.call_app.assert_not_called()
