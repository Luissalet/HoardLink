"""Route shared voice calls without starting a provider or replaying a job."""
from __future__ import annotations

from . import contract
from ..service_contracts import OWNERS, PREVIOUS_OWNERS, SERVICE_TOOLS, VOICE_ALIASES, VOICE_SERVICES, service_owners

_FOLLOW_STT = frozenset({"transcribe_status", "transcribe_cancel"})
_STT_HANDLE = "hoard-stt:"


def requested_service(app_id, tool):
    """Explicit non-service tools keep their original app address."""
    if app_id in VOICE_SERVICES and tool in SERVICE_TOOLS[app_id]:
        return app_id
    return next((key for key in ("stt", "tts") if tool in SERVICE_TOOLS[key] and (
        VOICE_ALIASES.get(app_id, app_id) == OWNERS[key] or app_id == PREVIOUS_OWNERS.get(key)
    )), None)


def provider_ids(service, caller=""):
    owners = service_owners(service)
    # The new provider can delegate via a vendored client that still uses the
    # old service address. Never send that request back to the same provider.
    if caller in VOICE_ALIASES or caller in VOICE_ALIASES.values():
        return tuple(owner for owner in owners if owner not in VOICE_ALIASES and owner not in VOICE_ALIASES.values())
    return owners


def select_provider(hub, service, tools, *, caller=""):
    """Check only catalogues. A work request is sent after selection."""
    seen = set()
    for owner in provider_ids(service, caller):
        app = hub.get(owner)
        if app is None or not app.url or app.id in seen:
            continue
        seen.add(app.id)
        if hub.automation_blocked(app.id):
            continue
        catalog = contract.app_tools(app, timeout=2)
        names = {entry.get("name") for entry in catalog.get("tools", ()) if isinstance(entry, dict)}
        if catalog.get("ok") and all(tool in names for tool in tools):
            return app.id
        # The legacy proxy still supports owners that publish only per-tool routes.
        if owner == PREVIOUS_OWNERS.get(service) and catalog.get("contract") == "per-tool":
            return app.id
    return None


def unavailable():
    # Maintain the transport-shaped response understood by older vendored clients.
    return {"ok": False, "status": None, "contract": "shared", "error": "voice provider not reachable or tool unavailable"}


def call(hub, service, address, tool, args, caller, direct_call):
    if service == "stt" and tool in _FOLLOW_STT:
        job = str(args.get("job_id") or "")
        owner = PREVIOUS_OWNERS["stt"] if address == "stt" else address
        qualified = job.startswith(_STT_HANDLE)
        if qualified:
            owner, sep, job = job[len(_STT_HANDLE):].partition(":")
            if owner not in service_owners("stt") or not sep or not job:
                return {"ok": False, "status": 400, "error": "invalid transcription job ID"}
            args = {**args, "job_id": job}
        if hub.get(owner) is None:
            return {"ok": False, "status": 404, "error": f"unknown app: {owner}", "service_owner": owner}
        return with_owner(direct_call(owner, tool, args), owner, qualify_job=qualified)

    needed = SERVICE_TOOLS["stt"] if service == "stt" else (tool,)
    owner = select_provider(hub, service, needed, caller=caller)
    if owner is None:
        return unavailable()
    result = direct_call(owner, tool, args)
    if not result.get("ok") and result.get("status") is None:
        # The provider was reachable before the mutation. A lost reply can
        # follow acceptance, so even older clients must not start it again.
        result = {**result, "status": 502, "submission_unknown": True}
    return with_owner(result, owner, qualify_job=(service == "stt"))


def with_owner(result, owner, *, qualify_job):
    response = {**result, "service_owner": owner}
    data = response.get("result")
    if qualify_job and owner != PREVIOUS_OWNERS["stt"] and isinstance(data, dict) and data.get("job_id"):
        response["result"] = {**data, "job_id": f"{_STT_HANDLE}{owner}:{data['job_id']}"}
    return response
