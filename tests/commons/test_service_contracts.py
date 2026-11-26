import json
from pathlib import Path
import shutil
import subprocess

import pytest

from hoard_link import fam_docs, fam_embed, fam_media, fam_workspace, service_contracts
from hoard_link.hub import services


def test_python_hub_and_node_share_the_exact_owner_contract():
    repo = Path(__file__).resolve().parents[2]
    canonical = repo / "hoard_link/_data/family-services.json"
    mirror = repo / "js/hoard-commons/family-services.json"
    assert canonical.read_bytes() == mirror.read_bytes()
    assert services.SERVICES is service_contracts.SERVICES
    assert (fam_media.LINKS, fam_docs.KAFKA, fam_embed.BORGES, fam_workspace.OWNER) == tuple(
        service_contracts.OWNERS[key] for key in ("media", "docs", "embed", "storage")
    )
    assert {key: service_contracts.OWNERS[key] for key in ("stt", "tts", "dictation")} == dict.fromkeys(
        ("stt", "tts", "dictation"), "boltagon"
    )
    assert dict(service_contracts.PREVIOUS_OWNERS) == {"stt": "funes", "tts": "prospero"}
    assert (fam_media.FUNES, fam_media.PROSPERO) == (
        service_contracts.PREVIOUS_OWNERS["stt"], service_contracts.PREVIOUS_OWNERS["tts"],
    )
    assert service_contracts.VOICE_ALIASES["cyrano"] == "boltagon"
    assert service_contracts.OWNERS["mail"] == "hub"
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = "import {OWNERS} from './js/hoard-commons/fam-services.js'; console.log(JSON.stringify(OWNERS));"
    result = subprocess.run([node, "--input-type=module", "-e", script], cwd=repo, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == dict(service_contracts.OWNERS)
