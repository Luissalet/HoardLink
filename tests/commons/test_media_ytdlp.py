"""yt-dlp / gallery-dl argument building, output parsing and failure classification: Python and Node agree on
tests/vectors/media_ytdlp.json, and the real error strings classify the way the apps need."""

from __future__ import annotations

import pytest

from hoard_link.media import bins
from tests.commons.jsrun import load_vectors, normalise, python_call, run_js

CASES = load_vectors("media_ytdlp")


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['fn']}:{str(c['args'])[:28]}:{sorted(c.get('opts') or {})}")
def test_python(case):
    assert normalise(python_call(bins, case)) == case["expect"]


def test_node_twin():
    got = run_js("media.js", CASES)
    bad = [(c["fn"], c["args"], g, c["expect"]) for c, g in zip(CASES, got) if g != c["expect"]]
    assert not bad, bad[:3]


# -- real yt-dlp strings -------------------------------------------------------------------------------------

NETWORK = "ERROR: Unable to download webpage: <urlopen error [Errno -2] Name or service not known> (caused by URLError(gaierror(-2, 'Name or service not known')))"
LOGIN = "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication."
FORBIDDEN = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


def test_network_error_is_not_a_login_problem():
    # the old Cook regex matched the bare word "age" inside "webpage"
    assert bins.classify_failure("yt-dlp", NETWORK) == "network"
    assert bins.classify_failure("yt-dlp", "ERROR: Unable to download webpage: <urlopen error timed out>") == "network"


def test_bot_check_is_login():
    assert bins.classify_failure("yt-dlp", LOGIN) == "login"


def test_403_is_forbidden_outdated():
    assert bins.classify_failure("yt-dlp", FORBIDDEN) == "forbidden"
    # but a 403 that mentions cookies / sign in is a login problem
    assert bins.classify_failure("yt-dlp", "HTTP Error 403: Forbidden. Sign in to confirm your age") == "login"


def test_explain_failure_flags_node(tmp_path):
    from tests.commons.jsrun import JS_DIR, node
    import json
    import subprocess

    script = tmp_path / "t.mjs"
    mod = (JS_DIR / "media.js").resolve().as_uri()
    script.write_text(
        f"import {{ explainFailure }} from {json.dumps(mod)};\n"
        f"const pick = (e) => ({{ kind: e.kind, authLike: !!e.authLike, outdatedLike: !!e.outdatedLike, status: e.status, msg: e.message }});\n"
        f"console.log(JSON.stringify([explainFailure('yt-dlp', {json.dumps(NETWORK)}), explainFailure('yt-dlp', {json.dumps(LOGIN)}),\n"
        f"  explainFailure('yt-dlp', {json.dumps(FORBIDDEN)}), explainFailure('yt-dlp', 'ERROR: Unsupported URL: https://x.test/')].map(pick)));\n",
        encoding="utf-8")
    out = json.loads(subprocess.run([node(), str(script)], capture_output=True, text=True, check=True).stdout)
    net, login, forb, unsup = out
    assert net["kind"] == "network" and not net["authLike"] and net["status"] == 502
    assert login["kind"] == "login" and login["authLike"] and "cookies" in login["msg"].lower()
    assert forb["kind"] == "forbidden" and forb["outdatedLike"]
    assert unsup["kind"] == "unsupported"
    assert "Name or service not known" in net["msg"]


# -- urls ---------------------------------------------------------------------------------------------------

PRIVATE = [
    "http://localhost/x", "http://127.0.0.1/x", "http://127.1/", "http://2130706433/", "http://0x7f.0.0.1/", "http://0177.0.0.1/",
    "http://10.0.0.5/", "http://172.16.3.4/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data", "http://100.64.0.1/",
    "http://0.0.0.0/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/", "http://[fd00::1]/", "http://printer.local/",
    "http://db.internal/", "http://intranet/", "file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "", "   ",
]


@pytest.mark.parametrize("url", PRIVATE)
def test_private_urls_refused_python(url):
    with pytest.raises(bins.MediaUrlError):
        bins.normalize_media_url(url)


def test_private_urls_refused_node():
    got = run_js("media.js", [{"fn": "normalize_media_url", "args": [u]} for u in PRIVATE])
    assert all(isinstance(g, dict) and "__error__" in g for g in got), [(u, g) for u, g in zip(PRIVATE, got) if not (isinstance(g, dict) and "__error__" in g)]


# -- misc ---------------------------------------------------------------------------------------------------

def test_progress_template_markers_round_trip():
    args = bins.build_ytdlp_args(url="https://example.com/v", dir="/d")
    assert args[-2:] == ["--", "https://example.com/v"]
    assert "--ignore-config" == args[0]
    templates = [a for a in args if a.startswith(("download:LHP|", "postprocess:LHPP|"))]
    assert len(templates) == 2


def test_node_runtime_flags_are_gated_by_version():
    assert "--js-runtimes" in bins.ytdlp_base_args(node="/n/node", version="2026.01.01")
    assert "--js-runtimes" not in bins.ytdlp_base_args(node="/n/node", version="2025.06.01")
    assert "--js-runtimes" not in bins.ytdlp_base_args(node="", version="2026.01.01")
