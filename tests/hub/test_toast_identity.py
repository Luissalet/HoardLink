"""Toasts carry the identity of the app that sent them (name and icon), not PowerShell's."""
from __future__ import annotations

import os

from hoard_link import notify_channels as nc
from hoard_link.hub.notify import NotifyFacet


def test_app_id_is_stable_and_safe():
    assert nc.toast_app_id("hub") == "Hoard.hub"
    assert nc.toast_app_id("ledger's hoard!") == "Hoard.ledgershoard"
    assert nc.toast_app_id("") == nc.POWERSHELL_APP_ID
    assert nc.toast_app_id("x" * 100) == "Hoard." + "x" * 60


def test_ps_quote_and_file_uri():
    assert nc.ps_quote("Ledger's Hoard") == "'Ledger''s Hoard'"
    assert nc.ps_quote("a\nb\x00") == "'ab'"
    assert nc.file_uri(r"C:\Users\a b\Icons\Ledgers hoard.png") == "file:///C:/Users/a%20b/Icons/Ledgers%20hoard.png"
    assert nc.file_uri("/home/x/ñ.png") == "file:///home/x/%C3%B1.png"


def test_identity_registers_the_app_id_and_shows_its_icon():
    icon = r"C:\Icons\Ledger's hoard.png"
    ps = nc.build_toast_ps1("Pago", "Amazon 18,35 €", "http://127.0.0.1:5180/#/informes", None,
                            nc.toast_app_id("ledger"), display_name="Ledger's Hoard", icon=icon)
    assert ps.startswith("$k = 'HKCU:\\Software\\Classes\\AppUserModelId\\Hoard.ledger'\n")
    assert "Set-ItemProperty -LiteralPath $k -Name DisplayName -Value 'Ledger''s Hoard'\n" in ps
    assert "Set-ItemProperty -LiteralPath $k -Name IconUri -Value 'C:\\Icons\\Ledger''s hoard.png'\n" in ps
    assert '<image placement="appLogoOverride" src="file:///C:/Icons/Ledger%27s%20hoard.png"/>' in ps
    assert ps.endswith("CreateToastNotifier('Hoard.ledger').Show($toast)\n")
    assert 'launch="http://127.0.0.1:5180/#/informes"' in ps


def test_without_identity_the_script_is_unchanged():
    plain = nc.build_toast_ps1("t", "b", "http://a.test")
    assert not plain.startswith("$k") and "IconUri" not in plain and "appLogoOverride" not in plain
    # A display name with PowerShell's own id never writes to the registry.
    assert "AppUserModelId" not in nc.build_toast_ps1("t", "b", None, None, nc.POWERSHELL_APP_ID, display_name="X")


def test_send_toast_uses_the_app_identity_when_asked():
    seen = []

    class Done:
        returncode = 0

    def runner(cmd, **kw):
        seen.append(open(cmd[-1], encoding="utf-8-sig").read())
        return Done()

    assert nc.send_toast("t", "b", None, app_name="Kafka's Hoard", app="kafka", icon=r"C:\I\k.png", runner=runner, platform="win32")["ok"]
    assert "CreateToastNotifier('Hoard.kafka')" in seen[-1] and "DisplayName -Value 'Kafka''s Hoard'" in seen[-1]
    assert nc.send_toast("t", "b", None, app_name="Kafka's Hoard", runner=runner, platform="win32")["ok"]
    assert nc.POWERSHELL_APP_ID in seen[-1] and 'placement="attribution"' in seen[-1]


def test_hub_toasts_show_the_sending_app(hub):
    f = hub.facet("notify")
    f.platform = "win32"
    f.toast_async = False
    scripts = []

    class Done:
        returncode = 0

    def runner(cmd, **kw):
        scripts.append(open(cmd[-1], encoding="utf-8-sig").read())
        return Done()

    f.powershell_runner = runner
    res = f.send("Hola", "cuerpo", app="hub", url="http://127.0.0.1:8810/#today", channels_override=["windows"])
    assert res["delivered"] == ["windows"], res
    assert "CreateToastNotifier('Hoard.hub')" in scripts[-1]
    assert "DisplayName -Value 'Hoard Hub'" in scripts[-1]
    assert 'launch="http://127.0.0.1:8810/#today"' in scripts[-1]
    # An app of the family shows its own name (from the registry) under its own application id.
    some = next(iter(hub.apps))
    f.send("Aviso", "x", app=some.id, channels_override=["windows"])
    assert f"CreateToastNotifier('{nc.toast_app_id(some.id)}')" in scripts[-1]
    assert f"DisplayName -Value {nc.ps_quote(some.name[:60])}" in scripts[-1]


def test_hub_toasts_show_markdown_as_plain_lines(hub):
    f = hub.facet("notify")
    f.platform = "win32"
    f.toast_async = False
    scripts = []

    class Done:
        returncode = 0

    f.powershell_runner = lambda cmd, **kw: (scripts.append(open(cmd[-1], encoding="utf-8-sig").read()), Done())[1]
    f.send("Resumen — Personal", "## Hoy\nNada pendiente.\n\n## Atención\n- **2** correos\n\n## Sistema\n",
           app="hub", channels_override=["windows"])
    assert "<text>Hoy: Nada pendiente.\nAtención: 2 correos</text>" in scripts[-1]
    assert "##" not in scripts[-1] and "**" not in scripts[-1]


def test_toast_text():
    assert nc.toast_text("## Hoy\nNada pendiente.\n\n## Atención\n") == "Hoy: Nada pendiente."
    assert nc.toast_text("- uno\n- [dos](http://x)\n`tres`") == "• uno\n• dos\ntres"
    assert nc.toast_text("cuerpo simple") == "cuerpo simple"
    assert nc.toast_text("\n".join(f"l{i}" for i in range(6)), max_lines=3) == "l0\nl1\nl2\n…"
    assert nc.toast_text("") == ""


def test_dry_run_never_starts_powershell(monkeypatch):
    """The whole suite runs with HOARD_NOTIFY_DRY_RUN=1 (tests/conftest.py): a test hub once put a real toast in front of
    the person, and its click opened a test port that was already gone."""
    import subprocess

    def boom(*a, **k):
        raise AssertionError("a real PowerShell was started")

    monkeypatch.setattr(subprocess, "run", boom)
    assert os.environ.get("HOARD_NOTIFY_DRY_RUN") == "1"
    assert nc.run_ps1("Write-Output 1") == {"ok": True, "dry_run": True}
    assert nc.send_toast("t", "b", platform="win32") == {"ok": True, "dry_run": True}
    assert nc.run_ps1("x", runner=subprocess.run) == {"ok": True, "dry_run": True}  # the default runner, spelled out
    # An injected runner is still called: tests that check the PowerShell call keep working.
    seen = []

    class Done:
        returncode = 0

    assert nc.run_ps1("x", runner=lambda cmd, **kw: (seen.append(cmd), Done())[1]) == {"ok": True}
    assert seen
    monkeypatch.setenv("HOARD_NOTIFY_DRY_RUN", "0")
    assert not nc.dry_run()


def test_node_twin_honours_the_dry_run():
    from tests.commons.test_notify_router import run_link_js

    out = run_link_js("return [fam.notifyDryRun(), await fam.runPs1('Write-Output 1')];")
    assert out == [True, {"ok": True, "dry_run": True}]


def test_hub_dry_run_is_decided_before_the_toast_thread(hub, monkeypatch):
    """A test's teardown can restore the environment before an async toast thread runs: the hub must decide first."""
    f = hub.facet("notify")
    f.platform = "win32"
    f.toast_async = True
    import subprocess
    assert f.powershell_runner is subprocess.run  # the hub's real default
    started = []
    monkeypatch.setattr(nc, "run_ps1", lambda *a, **k: started.append(a) or {"ok": True})
    res = f.send("Resumen", "## Hoy\nNada.", app="hub", channels_override=["windows"])
    assert res["delivered"] == ["windows"], res
    import time
    time.sleep(0.2)
    assert started == []
