from __future__ import annotations

import sys

import httpx
import pytest

from hoard_link.config import CapabilityConfig, LinkConfig
from hoard_link.errors import Unavailable
from tests.conftest import Router, make_link

HEALTHY = httpx.Response(200, json={"status": "healthy"})
EMPTY_REGISTRY = httpx.Response(200, json={"items": []})


@pytest.mark.asyncio
async def test_tts_via_faustus_service_returns_audio_bytes():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", EMPTY_REGISTRY)
    router.get(7000, "/api/tts/capabilities", httpx.Response(200, json={"voices": ["es-ES"]}))

    def synth(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"RIFF....WAVEdata", headers={"content-type": "audio/wav"})

    router.post(7000, "/api/tts/synthesize", synth)
    cfg = LinkConfig(faustus_token="ody_test")
    link = make_link(router, config=cfg)

    audio = await link.tts("hola")
    assert audio == b"RIFF....WAVEdata"


@pytest.mark.asyncio
async def test_tts_faustus_forbidden_raises_unavailable():
    router = Router()
    router.get(7000, "/api/health", HEALTHY)
    router.get(7000, "/api/models", EMPTY_REGISTRY)
    router.get(7000, "/api/tts/capabilities", httpx.Response(403, json={"error": "forbidden"}))
    cfg = LinkConfig(faustus_token="ody_test")
    link = make_link(router, config=cfg)

    with pytest.raises(Unavailable):
        await link.tts("hola")


@pytest.mark.asyncio
async def test_tts_via_configured_command_writes_to_out_file():
    router = Router()
    script = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_bytes(b'fake-wav-bytes')"
    cfg = LinkConfig(capabilities={"tts": CapabilityConfig(command=[sys.executable, "-c", script, "{out}"])})
    link = make_link(router, config=cfg)

    audio = await link.tts("hola", voice="es-ES")
    assert audio == b"fake-wav-bytes"


@pytest.mark.asyncio
async def test_tts_via_configured_command_stdout_when_no_out_placeholder():
    router = Router()
    script = "import sys; sys.stdout.buffer.write(b'stdout-audio')"
    cfg = LinkConfig(capabilities={"tts": CapabilityConfig(command=[sys.executable, "-c", script])})
    link = make_link(router, config=cfg)

    audio = await link.tts("hola")
    assert audio == b"stdout-audio"


@pytest.mark.asyncio
async def test_tts_raises_unavailable_when_nothing_resolves():
    router = Router()
    link = make_link(router, config=LinkConfig())
    with pytest.raises(Unavailable):
        await link.tts("hola")
