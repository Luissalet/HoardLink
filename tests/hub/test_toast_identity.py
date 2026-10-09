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
