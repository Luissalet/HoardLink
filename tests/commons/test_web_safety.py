"""hoard_link.web.safety (SSRF policy) and its Node twin agree on tests/vectors/web_safety.json."""

from __future__ import annotations

import json
import subprocess

import pytest

from hoard_link.web import safety
from tests.commons.jsrun import JS_DIR, load_vectors, node
from tests.commons.web_helpers import Site, case_id, js_results, matches, py_call, resolver_from

CASES = load_vectors("web_safety")


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_python(case):
    assert matches(case, py_call(case))


def test_node_twin():
    got = js_results("web.js", CASES)
    bad = [(c["fn"], c["args"], c.get("opts", {}).get("profile"), g, c.get("expect", c.get("expect_has"))) for c, g in zip(CASES, got) if not matches(c, g)]
    assert not bad, bad


def test_ip_tables_are_identical_in_both_languages():
    exe = node()
    script = ("import * as m from %s;process.stdout.write(JSON.stringify({n: m.NETWORKS, meta: m.METADATA_ADDRS, hosts: m.METADATA_HOSTS, loc: m.LOCAL_SUFFIXES}));"
              % json.dumps((JS_DIR / "web.js").resolve().as_uri()))
    js = json.loads(subprocess.run([exe, "--input-type=module", "-e", script], capture_output=True, text=True, check=True).stdout)
    py = {"v4LinkLocal": safety.V4_LINK_LOCAL, "v4Multicast": safety.V4_MULTICAST, "v4Private": safety.V4_PRIVATE, "v4Reserved": safety.V4_RESERVED,
          "v6LinkLocal": safety.V6_LINK_LOCAL, "v6Multicast": safety.V6_MULTICAST, "v6Private": safety.V6_PRIVATE, "v6Reserved": safety.V6_RESERVED}
    for key, nets in py.items():
        assert js["n"][key] == [str(n) for n in nets], key
    assert js["n"]["v6GlobalUnicast"] == [str(safety.V6_GLOBAL_UNICAST)]
    assert js["n"]["cgnat"] == [str(safety._CGNAT)] and js["n"]["nat64"] == [str(safety._NAT64)] and js["n"]["teredo"] == [str(safety._TEREDO)]
    assert sorted(js["meta"]) == sorted(str(a) for a in safety._METADATA_ADDRS)
    assert sorted(js["hosts"]) == sorted(safety.METADATA_HOSTS) and js["loc"] == list(safety._LOCAL_SUFFIXES)


# ---- regressions: each of these was a hole in one of the copies this module replaces -----------------

@pytest.mark.parametrize("host", ["100.64.0.1", "100.100.100.200", "100.127.255.254"])
def test_cgnat_is_not_public(host):
    assert "CGNAT" in (safety.check_url(f"http://{host}/", "public") or "") or "metadata" in (safety.check_url(f"http://{host}/") or "")
    assert safety.classify_ip(host, safety.PUBLIC)


def test_cgnat_is_allowed_only_for_the_operator_profile():
    assert safety.check_url("http://100.64.0.5/", safety.OPERATOR_LOCAL) is None
    assert safety.check_url("http://100.64.0.5/", safety.INTERNAL)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "file://localhost/etc/passwd", "FILE:///C:/Windows/win.ini", "file:C:/x"])
@pytest.mark.parametrize("profile", safety.PROFILES)
def test_file_scheme_is_always_refused(url, profile):
    reason = safety.check_url(url, profile)
    assert reason and "scheme" in reason


@pytest.mark.parametrize("host", ["2130706433", "0x7f.1", "017700000001", "127.1", "0x7f000001", "0177.0.0.1", "1.2.3"])
@pytest.mark.parametrize("profile", safety.PROFILES)
def test_obfuscated_ipv4_is_refused_under_every_profile(host, profile):
    assert "obfuscated" in (safety.check_url(f"http://{host}/", profile) or "")


@pytest.mark.parametrize("addr", ["::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:1::1", "64:ff9b::a00:1", "::127.0.0.1", "::10.0.0.1"])
def test_tunnelled_ipv4_is_judged_by_the_ipv4_part(addr):
    assert safety.classify_ip(addr, safety.PUBLIC)
    assert safety.classify_ip(addr, safety.OPERATOR_LOCAL) is None or "metadata" in safety.classify_ip(addr, safety.OPERATOR_LOCAL)


def test_tunnelled_public_ipv4_is_public():
    assert safety.classify_ip("::ffff:8.8.8.8") is None and safety.classify_ip("2002:808:808::1") is None


def test_every_resolved_address_must_be_allowed():
    resolver = resolver_from({"mixed.example": ["93.184.216.34", "10.0.0.7"]})
    assert "10.0.0.7" in safety.check_url("https://mixed.example/", safety.PUBLIC, resolver)
    assert safety.check_url("https://mixed.example/", safety.OPERATOR_LOCAL, resolver) is None


def test_resolve_public_returns_the_checked_addresses_and_raises_otherwise():
    resolver = resolver_from({"ok.example": ["93.184.216.34", "2606:4700::1"], "bad.example": ["10.1.1.1"]})
    assert safety.resolve_public("https://ok.example/", resolver=resolver) == ["93.184.216.34", "2606:4700::1"]
    with pytest.raises(safety.PolicyError) as err:
        safety.resolve_public("https://bad.example/", resolver=resolver)
    assert err.value.kind == "policy" and "10.1.1.1" in err.value.reason
    with pytest.raises(safety.PolicyError) as err:
        safety.resolve_public("https://nowhere.example/", resolver=resolver)
    assert err.value.kind == "dns" and err.value.reason.startswith(safety.UNRESOLVABLE_PREFIX)


def test_literal_addresses_need_no_resolver():
    assert safety.resolve_public("http://8.8.8.8/") == ["8.8.8.8"]
    assert safety.resolve_public("http://localhost:9/", safety.OPERATOR_LOCAL) == ["127.0.0.1", "::1"]


def test_unknown_profile_is_a_programming_error():
    with pytest.raises(ValueError):
        safety.check_url("https://example.com/", "everything")
    with pytest.raises(ValueError):
        safety.classify_ip("8.8.8.8", "loose")


def test_pinned_transport_connects_to_the_checked_address_whatever_the_name_says():
    httpx = pytest.importorskip("httpx")
    with Site({"/x": (200, {"Content-Type": "text/plain"}, b"hello")}) as site:
        with httpx.Client(transport=safety.pinned_transport("127.0.0.1")) as client:
            r = client.get(f"http://never-resolves.invalid:{site.port}/x")
        assert r.status_code == 200 and r.text == "hello"
        assert site.hits[0][2]["host"] == f"never-resolves.invalid:{site.port}"      # the name stays in the Host header


def test_pinned_transport_streams_the_body():
    httpx = pytest.importorskip("httpx")
    with Site({"/big": (200, {"Content-Type": "text/plain"}, b"x" * 100_000)}) as site:
        with httpx.Client(transport=safety.pinned_transport("127.0.0.1")) as client:
            with client.stream("GET", f"http://pinned.invalid:{site.port}/big") as r:
                got = b"".join(r.iter_bytes(1024))
        assert len(got) == 100_000


def test_a_rebinding_resolver_cannot_win_after_the_check():
    """The checked list is what the connection uses: a second, different DNS answer is never consulted."""
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    calls = []

    def resolver(host, port):
        calls.append(host)
        return next(answers)

    ips = safety.resolve_public("https://rebind.example/", resolver=resolver)
    assert ips == ["93.184.216.34"] and calls == ["rebind.example"]
