"""Credential for the signed-in person's Studio relay on production decisions."""
from pathlib import Path
import secrets

from ..tokens import read_or_create_token


TOKEN_NAME = "agora-person-mcp-token"
PROOF_HEADER = "X-Agora-Person-Token"


def token(hub):
    return read_or_create_token(Path(hub.config.data_dir) / TOKEN_NAME)


def can_decide(hub, req):
    caller = req.caller()
    if caller == "ui":
        return True
    proof = req.headers.get(PROOF_HEADER, "") if req.headers is not None else ""
    return caller == "hub" and bool(proof) and secrets.compare_digest(proof.encode(), token(hub).encode())
