"""A confined worker's web, video and mesh checks: no network and nothing of the host outside its scope."""

import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from hoard_link.hub import piece_checks as checks
from hoard_link.hub import piece_checks_media as media

HUB = Path(__file__).resolve().parents[1]
FAMILY = Path(os.environ.get("HOARD_FAMILY", r"E:\Development\IA\Hoards\Faustusables"))
PAGE_ENGINE = Path(os.environ.get("VITRUVIUS_REPO", FAMILY / "Vitruvius's Hoard"))
MESH_ENGINE = Path(os.environ.get("VULCAN_REPO", FAMILY / "Vulcan's Hoard"))


class Outside:
    """A server on a test address that counts every connection, with or without a request on it."""

    def __init__(self):
        self.connections, self.requests = 0, []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(50)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._read, args=(connection,), daemon=True).start()

    def _read(self, connection):
        try:
            connection.settimeout(1)
            head = connection.recv(2000).split(b"\r\n", 1)[0].decode("latin-1")
            if head:
                self.requests.append(head)
                connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        finally:
            connection.close()

    def close(self):
        self.server.close()


def _python(engine):
    python = engine / "venv/Scripts/python.exe"
    if not python.is_file():
        pytest.skip(f"The installed engine {engine.name} is unavailable")
    return str(python)


# ---------------------------------------------------------------------- web


NET_LOG_DRIVER = r"""
import json, sys
from pathlib import Path
hub, mode, target, log, encoded = sys.argv[1:6]
sys.path[:0] = [hub]
from vitruvius_hoard import browser as engine
original = engine.launch_args
engine.launch_args = lambda: [*original(), f"--log-net-log={log}", "--net-log-capture-mode=Everything"]
if mode == "web":
    from hoard_link.hub import piece_checks
    print(json.dumps(piece_checks.run_checks(Path(target), "web", json.loads(encoded))))
else:
    # A real profile preconnects; the Hub's throwaway one does not in every browser, so this is the control.
    import tempfile, time
    from playwright.sync_api import sync_playwright
    from hoard_link.hub.piece_checks_layout import _offline_flags
    from vitruvius_hoard.browser import launch_attempts
    flags = _offline_flags(target) if mode == "profile-offline" else []
    with sync_playwright() as play:
        for label, options in launch_attempts():
            try:
                context = play.chromium.launch_persistent_context(
                    tempfile.mkdtemp(), headless=True, args=[*original(), *flags, f"--log-net-log={log}",
                                                             "--net-log-capture-mode=Everything"], **options)
                break
            except Exception:
                continue
        page = context.new_page()
        page.goto(target + "/")
        time.sleep(2.5)
        context.close()
    print(json.dumps({"browser": label}))
"""


def _resolutions(log, host):
    """(every name the browser asked its resolver for, those that were the probe host)."""
    data = json.loads(Path(log).read_text(encoding="utf-8", errors="replace"))
    names = {value: key for key, value in data["constants"]["logEventTypes"].items()}
    jobs = [event for event in data["events"] if names.get(event["type"]) == "HOST_RESOLVER_MANAGER_JOB"]
    return jobs, [event for event in jobs if host in json.dumps(event.get("params", {}))]


def _run_driver(python, tmp_path, mode, target, encoded="{}", tag=""):
    log = tmp_path / f"{mode}{tag}-net.json"
    done = subprocess.run([python, "-c", NET_LOG_DRIVER, str(HUB), mode, str(target), str(log), encoded],
                          cwd=HUB, env={**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(PAGE_ENGINE)},
                          capture_output=True, encoding="utf-8", timeout=170)
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout.strip().splitlines()[-1]), log


def _prefetching_page(outside):
    host = f"probe-{os.getpid()}.invalid"
    remote = f"http://127.0.0.1:{outside.port}"
    html = ('<!doctype html><html lang="en"><title>Fixture</title><body><p>Fixture</p>'
            f'<link rel="preconnect" href="{remote}"><link rel="dns-prefetch" href="//{host}">'
            f'<link rel="preconnect" href="https://{host}">'
            f'<img alt="" src="{remote}/image?secret=1"><link rel="stylesheet" href="{remote}/style.css">'
            f'<script>fetch("{remote}/fetch?secret=2").catch(() => {{}});'
            f'try {{ new WebSocket("ws://127.0.0.1:{outside.port}/socket"); }} catch (e) {{}}'
            f'navigator.sendBeacon("{remote}/beacon", "secret");'
            f'fetch("https://{host}/x").catch(() => {{}});</script></body></html>')
    return host, html


def test_confined_worker_web_page_opens_no_connection_and_resolves_no_name(tmp_path):
    python = _python(PAGE_ENGINE)
    outside = Outside()
    try:
        host, html = _prefetching_page(outside)
        site = tmp_path / "site.zip"
        with ZipFile(site, "w") as archive:
            archive.writestr("index.html", html)
        requirements = json.dumps({"viewports": [[800, 600]]})
        result, log = _run_driver(python, tmp_path, "web", site, json.dumps({"viewports": [[800, 600]], "_offline": True}))
        assert result["measures"].get("viewports"), result
        jobs, _ = _resolutions(log, host)
        assert (outside.connections, outside.requests, jobs) == (0, [], [])
        # The control: the same page, rendered for a person, does reach the outside server.
        _run_driver(python, tmp_path, "web", site, requirements, "-control")
        assert outside.connections > 0 and any(row.startswith("GET /image") for row in outside.requests)
    finally:
        outside.close()


def test_offline_browser_has_no_preconnect_or_dns_prefetch(tmp_path):
    """Request interception does not see these: the browser itself must have nowhere to connect or resolve."""
    python = _python(PAGE_ENGINE)
    outside = Outside()
    host, html = _prefetching_page(outside)

    import http.server

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        control, control_log = _run_driver(python, tmp_path, "profile-control", base)
        control_jobs = _resolutions(control_log, host)[1]
        if not (outside.connections or control_jobs):
            pytest.skip(f"{control['browser']} does not preconnect from a page, so there is nothing to compare")
        assert outside.connections > 0 and control_jobs  # the page does reach out without the switches
        outside.connections = 0
        _, offline_log = _run_driver(python, tmp_path, "profile-offline", base)
        jobs, _ = _resolutions(offline_log, host)
        assert outside.connections == 0
        assert jobs == []  # not one name was handed to the resolver, for any host
    finally:
        server.shutdown()
        server.server_close()
        outside.close()


# -------------------------------------------------------------------- video


def _clip(path, size="64x48"):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is unavailable")
    subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:s={size}:d=1:r=10",
                    "-pix_fmt", "yuv420p", str(path)], check=True, timeout=60)
    return path


def _scope(folder):
    return {"files": [], "folders": [str(folder)]}


def test_confined_video_check_does_not_follow_a_concat_list_named_mp4(tmp_path):
    folder = tmp_path / "pieces"
    folder.mkdir()
    _clip(folder / "other-piece.mp4")
    clip = _clip(folder / "own.mp4", "32x24")
    # ffmpeg picks the demuxer from the content: this "mp4" is a list that names a neighbour file.
    (folder / "list.mp4").write_text("ffconcat version 1.0\nfile 'other-piece.mp4'\nduration 1.0\n", encoding="utf-8")
    person = checks.run_checks(folder / "list.mp4", "video", {})
    assert person["measures"]["size_px"] == [64, 48]  # the control: the person's check does read the neighbour
    offline = {"_offline": True, "_scope": _scope(folder)}
    worker = checks.run_checks(folder / "list.mp4", "video", offline)
    assert worker["verdict"] == "unmeasured" and "size_px" not in worker["measures"]
    assert [row["id"] for row in worker["checks"]] == ["inspection"]
    # What a clip is still measured.
    assert checks.run_checks(clip, "video", offline)["measures"]["size_px"] == [32, 24]


def test_ffmpeg_input_options_refuse_a_playlist_that_names_a_server(tmp_path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        pytest.skip("ffprobe is unavailable")
    outside = Outside()
    try:
        playlist = tmp_path / "clip.mp4"
        playlist.write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n"
                            f"#EXTINF:1.0,\nhttp://127.0.0.1:{outside.port}/segment.mp4\n#EXT-X-ENDLIST\n",
                            encoding="utf-8")

        def probe(*options, source):
            outside.connections = 0
            subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=format_name", *options, source],
                           capture_output=True, timeout=30)
            return outside.connections

        # The control: with the network protocols allowed, the playlist is read and its segment requested.
        assert probe("-f", "hls", "-protocol_whitelist", "file,http,tcp", source=str(playlist)) == 1
        # With the Hub's options neither the format nor the protocol is allowed, forced or sniffed.
        assert probe("-f", "hls", *media.SAFE_INPUT, source=f"file:{playlist}") == 0
        assert probe(*media.SAFE_INPUT, source=f"file:{playlist}") == 0
    finally:
        outside.close()


def test_confined_video_references_stay_inside_the_workers_scope(tmp_path):
    from PIL import Image

    scope_folder = tmp_path / "own"
    scope_folder.mkdir()
    clip = _clip(scope_folder / "clip.mp4")
    inside, outside = scope_folder / "ref.png", tmp_path / "host-file.png"
    for image in (inside, outside):
        Image.new("RGB", (16, 16), "red").save(image)
    base = {"_offline": True, "_scope": _scope(scope_folder), "min_similarity": 0.1}
    ok = checks.run_checks(clip, "video", {**base, "references": [{"path": str(inside)}]})
    assert "inspection" not in {row["id"] for row in ok["checks"] if row["verdict"] == "unmeasured"}
    for ref in ({"path": str(outside)}, {"path": str(outside), "time_s": 0}, {"path": str(tmp_path / "missing.png")}):
        refused = checks.run_checks(clip, "video", {**base, "references": [ref]})
        detail = [row for row in refused["checks"] if row["id"] == "inspection"][0]["detail"]
        # The same answer whether or not the file exists: the disk outside the scope is not asked.
        assert "outside the declared scope" in detail
    identity = checks.run_checks(clip, "video", {**base, "identity": {"references": [{"path": str(outside)}]}})
    assert any("outside the declared scope" in str(row.get("detail", "")) for row in identity["checks"])


def test_confined_narrative_check_does_not_open_a_source_path_outside_its_scope(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    own = tmp_path / "own"
    own.mkdir()
    pdf = own / "piece.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Hello")
        document.save(pdf)
    inside, outside = own / "manuscript.txt", tmp_path / "host-manuscript.txt"
    for source in (inside, outside):
        source.write_text("Hello", encoding="utf-8")
    for source in (outside, tmp_path / "missing.txt"):
        result = checks.run_checks(pdf, "narrative", {"source_path": str(source), "_scope": _scope(own)})
        detail = [row for row in result["checks"] if row["id"] == "inspection"][0]["detail"]
        assert "outside the declared scope" in detail  # the same answer whether or not the file exists
    result = checks.run_checks(pdf, "narrative", {"source_path": str(inside), "_scope": _scope(own)})
    assert "inspection" not in {row["id"] for row in result["checks"]}
    assert checks.run_checks(pdf, "narrative", {"source_path": str(outside)})["measures"]  # a person's check


# --------------------------------------------------------------------- mesh


def _glb(path, document, binary=b"\x00" * 12):
    text = json.dumps(document).encode()
    text += b" " * (-len(text) % 4)
    chunks = struct.pack("<I4s", len(text), b"JSON") + text
    if binary:
        chunks += struct.pack("<I4s", len(binary), b"BIN\x00") + binary
    path.write_bytes(b"glTF" + struct.pack("<II", 2, 12 + len(chunks)) + chunks)
    return path


def _document(**extra):
    return {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": "Prop"}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
            "accessors": [{"count": 3, "componentType": 5126, "type": "VEC3", "bufferView": 0}],
            "bufferViews": [{"buffer": 0, "byteLength": 36}], **extra}


def test_confined_game_mesh_check_does_not_read_files_a_glb_points_to(tmp_path):
    python = _python(MESH_ENGINE)
    from PIL import Image

    host = tmp_path / "host"
    host.mkdir()
    Image.new("RGB", (2, 2), "red").save(host / "secret.png")
    (host / "secret.bin").write_bytes(b"\x00" * 36)
    piece = tmp_path / "pieces"
    piece.mkdir()
    glb = _glb(piece / "prop.glb", _document(buffers=[{"uri": "../host/secret.bin", "byteLength": 36}],
                                             images=[{"uri": "../host/secret.png"}]), binary=b"")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": os.pathsep.join((str(HUB), str(MESH_ENGINE)))}

    def run(requirements, target=glb):
        done = subprocess.run([python, "-m", "hoard_link.hub.piece_checks", "game-mesh", str(target),
                               json.dumps(requirements)], cwd=HUB, env=env, capture_output=True, encoding="utf-8",
                              timeout=120)
        assert done.returncode == 0, done.stderr[-800:]
        return json.loads(done.stdout)

    control = run({})
    assert control["measures"]["texture_sizes"] == [[2, 2]]  # the control: a person's check measures the host file
    worker = run({"_offline": True, "_scope": _scope(piece)})
    assert worker["verdict"] == "unmeasured"
    assert [row["id"] for row in worker["checks"]] == ["external-resources"]
    assert "texture_sizes" not in worker["measures"]
    # A GLB that keeps its buffers and images inside is measured as before.
    inner = _glb(piece / "inner.glb", _document(buffers=[{"byteLength": 36}]), binary=b"\x00" * 36)
    names = {row["id"] for row in run({"_offline": True, "_scope": _scope(piece)}, inner)["checks"]}
    assert "external-resources" not in names and "triangles" in names


@pytest.mark.parametrize("uri", ["../host/secret.bin", "secret.bin", "http://127.0.0.1:9/b.bin", "file:///x/y.bin",
                                 "C:/host/secret.bin", "%2e%2e/secret.bin"])
def test_every_kind_of_uri_in_a_confined_glb_is_left_unread(tmp_path, monkeypatch, uri):
    read = []
    native = SimpleNamespace(gltf_facts=lambda path: read.append("facts") or {"triangles": 1, "image_sizes": []},
                             read_gltf=lambda path: ({"buffers": [{"uri": uri}], "extensions": {"x": [{"uri": uri}]}}, b""))
    monkeypatch.setitem(sys.modules, "vulcan.asset_inspection", native)
    result = checks.run_checks(tmp_path / "prop.glb", "game-mesh", {"_offline": True})
    assert read == [] and [row["id"] for row in result["checks"]] == ["external-resources"]


def test_confined_data_uris_are_not_external(tmp_path, monkeypatch):
    document = {"buffers": [{"uri": "data:application/octet-stream;base64,AAAA"}], "images": [{"uri": "DATA:image/png;base64,AA"}]}
    native = SimpleNamespace(gltf_facts=lambda path: {"triangles": 0, "image_sizes": []},
                             read_gltf=lambda path: (document, b""))
    monkeypatch.setitem(sys.modules, "vulcan.asset_inspection", native)
    result = checks.run_checks(tmp_path / "prop.glb", "game-mesh", {"_offline": True})
    assert "external-resources" not in {row["id"] for row in result["checks"]}


@pytest.mark.parametrize("profile, name", [("print-mesh", "model.obj"), ("print-mesh", "model.glb"),
                                           ("print-mesh", "model.gltf"), ("game-mesh", "model.gltf")])
def test_confined_mesh_checks_refuse_formats_that_name_other_files(tmp_path, monkeypatch, profile, name):
    opened = []
    native = SimpleNamespace(UNIT_TO_MM={"mm": 1}, load_geometry=lambda path: opened.append(path),
                             gltf_facts=lambda path: opened.append(path), read_gltf=lambda path: opened.append(path),
                             _edge_counts=None, _wall_records=None, sha256_of=None)
    monkeypatch.setitem(sys.modules, "vulcan.asset_inspection", native)
    result = checks.run_checks(tmp_path / name, profile, {"_offline": True})
    assert opened == [] and result["verdict"] == "unmeasured" and "only" in result["checks"][0]["detail"]


# ------------------------------------------------------------------- the Hub


@pytest.mark.parametrize("profile", ["video", "print-mesh", "game-mesh", "web", "document", "slides"])
def test_the_hub_marks_every_confined_check_offline(tmp_path, monkeypatch, profile):
    seen = []
    facet = checks.PieceChecksFacet.__new__(checks.PieceChecksFacet)
    facet.hub = None
    path = tmp_path / ("piece.zip" if profile == "web" else "piece.bin")
    path.write_bytes(b"x")
    answer = {"verdict": "unmeasured", "measures": {}, "checks": []}
    monkeypatch.setattr(checks, "run_checks", lambda path, profile, requirements, **kw: seen.append(requirements) or answer)
    monkeypatch.setattr(checks, "_engine", lambda hub, engine, path, profile, requirements: seen.append(requirements) or answer)
    result = facet.validate({"path": str(path), "profile": profile, "requirements": {}, "_worker_read_files": [],
                             "_worker_read_paths": [str(tmp_path)], "_worker_write_paths": []})
    assert result["ok"] and len(seen) == 1
    assert seen[0]["_offline"] is True and seen[0]["_scope"]["folders"] == [str(tmp_path)]
    # A person's check is not changed.
    seen.clear()
    facet.validate({"path": str(path), "profile": profile, "requirements": {}})
    assert len(seen) == 1 and "_offline" not in seen[0] and "_scope" not in seen[0]
