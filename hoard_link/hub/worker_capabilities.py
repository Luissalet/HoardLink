"""Turn a station's requested capability into an ordered list of verified runners."""
from __future__ import annotations


PROVIDERS = {
    "reasoning_high": ("claude", "codex", "cursor", "faustus"),
    "vision_strict": ("faustus", "codex", "claude", "cursor"),
    "fast_local": ("faustus", "codex", "claude", "cursor"),
}


def _effort(model, levels):
    offered = model.get("efforts") or []
    return next((level for level in levels if level in offered), None)


def candidates(console, capability, *, providers=None):
    from .agora_auto_review import _quota_available
    from .agora_capacity import codex_observation

    pools = {row["provider_id"]: row.get("pools", []) for row in console._agora().usage().get("providers", [])}
    vendors = console._provider_data()
    groups = []
    observation = codex_observation()
    for provider in providers or PROVIDERS[capability]:
        vendor = vendors.get(provider, {})
        if vendor.get("state") != "available":
            continue
        if provider != "faustus" and not _quota_available(provider, pools.get(provider, [])):
            continue
        if provider == "codex" and observation and not observation.get("ok"):
            continue
        group = []
        for model in vendor.get("catalog") or []:
            model_id = model["id"]
            if capability == "reasoning_high":
                effort = _effort(model, ("high", "xhigh", "max"))
                if not effort or model_id in ("default", "haiku"):
                    continue
            elif capability == "vision_strict":
                if "image" not in (model.get("input_modalities") or []):
                    continue
                effort = _effort(model, ("high", "medium", "default")) or model.get("default_effort")
            else:
                if provider == "claude" and model_id != "haiku":
                    continue
                effort = _effort(model, ("minimal", "low", "none", "default"))
                if effort is None and model.get("efforts"):
                    continue
            group.append({"leader_id": provider, "model": model_id, "effort": effort or ""})
        groups.append(group)
    # Return one candidate per provider before offering a second model from the same provider.
    return [group[index] for index in range(max(map(len, groups), default=0))
            for group in groups if index < len(group)]
