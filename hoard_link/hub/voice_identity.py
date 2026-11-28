"""Voice app identities shared by the Hub's installed compatibility paths."""
from ..service_contracts import VOICE_ALIASES

VOICE_APP_ALIASES = {**VOICE_ALIASES, **{target: alias for alias, target in VOICE_ALIASES.items()}}


def equivalent_ids(app_id):
    alias = VOICE_APP_ALIASES.get(app_id)
    return (app_id, alias) if alias else (app_id,)


def canonical_voice_id(value):
    return VOICE_ALIASES.get(value, value) if isinstance(value, str) else value
