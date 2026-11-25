import json
from types import SimpleNamespace

import pytest

from hoard_link.hub import placement_remote
from hoard_link.hub.placement import Placement


@pytest.mark.parametrize("on_spark", [False, True])
def test_placement_mail_identifies_the_hub_without_putting_its_token_in_the_command(tmp_path, on_spark):
    invocations = []
    link_steps = []

    def runner(args, **kwargs):
        assert link_steps == (["tick", "up"] if on_spark else [])
        invocations.append((args, kwargs))
        answer = '{"ok": true}'
        return SimpleNamespace(stdout=answer.encode() if on_spark else answer, returncode=0)

    hub = SimpleNamespace(
        config=SimpleNamespace(data_dir=str(tmp_path)), token="synthetic-hub-token",
        faustus_runtime=SimpleNamespace(installation=lambda: (tmp_path, "mock-python")),
    )
    placement = Placement(hub, runner=runner)
    placement._node = lambda: "fake-spark"
    def tick():
        link_steps.append("tick")
        return {"up": False, "error": ""}

    def status():
        link_steps.append("up")
        return {"up": True, "error": ""}

    placement.link = SimpleNamespace(tick=tick, status=status)
    messages = []
    placement._log = messages.append

    placement._mail("Synthetic relocation", "Mail test fixture", on_spark=on_spark)

    [(args, kwargs)] = invocations
    assert hub.token not in " ".join(args)
    assert json.loads(kwargs["input"])["caller"] == {"app": "hub", "token": hub.token}
    assert messages == ["aviso por correo enviado"]
    if on_spark:
        assert args[0] == "ssh"
        assert f"HOARD_MAIL_REAL_HUB_URLS={placement_remote.LINK_URL}" in args[-1]
    else:
        assert args[0] == "mock-python"
        assert kwargs["cwd"] == str(tmp_path)


def test_placement_mail_handles_a_failed_link_before_launching(tmp_path):
    def no_launch(*_args, **_kwargs):
        pytest.fail("the mail helper must wait for the Hub link")

    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)), token="synthetic-hub-token")
    placement = Placement(hub, runner=no_launch)
    placement.link = SimpleNamespace(tick=lambda: {"up": False, "error": "simulated link outage"})
    messages = []
    placement._log = messages.append
    placement._mail("Synthetic relocation", "Mail test fixture", on_spark=True)
    assert messages == ["no he podido enviar el aviso: simulated link outage"]
