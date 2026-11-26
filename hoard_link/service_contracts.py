"""Single machine-readable owner/tool contract; no model or network activity.

Node ships a byte-identical copy of _data/family-services.json. Its parity is
checked by tests and scripts/sync_vendored.py before distribution.
"""
import json
from pathlib import Path
from types import MappingProxyType

_DATA = json.loads((Path(__file__).parent / "_data" / "family-services.json").read_text(encoding="utf-8"))
OWNERS = MappingProxyType({name: spec["owner"] for name, spec in _DATA["services"].items()})
SERVICES = tuple((name, spec["owner"], tuple(spec["tools"])) for name, spec in _DATA["services"].items())
VOICE_ALIASES = MappingProxyType(_DATA.get("voice_aliases", {}))
PREVIOUS_OWNERS = MappingProxyType(_DATA.get("previous_owners", {}))
VOICE_SERVICES = frozenset({"stt", "tts", "dictation"})
VOICE_CATALOGUE_TIMEOUT_S = 2.0
SERVICE_TOOLS = MappingProxyType({name: tools for name, _, tools in SERVICES})


def service_owners(service: str) -> tuple[str, ...]:
    """Registered provider first, followed by compatible app IDs."""
    owner = OWNERS[service]
    aliases = (alias for alias, target in VOICE_ALIASES.items() if target == owner)
    return tuple(dict.fromkeys(filter(None, (owner, *aliases, PREVIOUS_OWNERS.get(service)))))
