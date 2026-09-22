"""Exceptions raised by :mod:`hoard_link`."""

from __future__ import annotations

from typing import Optional


class HoardLinkError(Exception):
    """Base class for every error this package raises on purpose."""


class Unavailable(HoardLinkError):
    """No server could be resolved for a capability.

    ``reasons`` is the list of short strings collected while walking the
    resolution order (one per source that was tried and did not pan out),
    so the caller (or its Settings screen) can show *why*.
    """

    def __init__(self, capability: str, reasons: list[str]):
        self.capability = capability
        self.reasons = list(reasons)
        joined = "; ".join(self.reasons) if self.reasons else "no reason recorded"
        super().__init__(f"{capability} unavailable: {joined}")


class BackendError(HoardLinkError):
    """A resolved server answered, but with a non-2xx status."""

    def __init__(self, provider: Optional[str], status: int, body_excerpt: str):
        self.provider = provider
        self.status = status
        self.body_excerpt = body_excerpt
        super().__init__(
            f"{provider or 'backend'} returned HTTP {status}: {body_excerpt}"
        )
