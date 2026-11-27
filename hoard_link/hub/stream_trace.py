"""Trace correlation and the event sink used by the stream gateway."""

from dataclasses import dataclass, field
import logging
import re
from time import monotonic, time
from uuid import uuid4

HEADER = "X-Hoard-Trace"
_ROOT = "0" * 32
_CONTEXT = re.compile(r"([a-f0-9]{32})/([a-f0-9]{32})")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
log = logging.getLogger(__name__)


@dataclass
class GatewayTrace:
    incoming: str
    caller: str
    callee: str
    enabled: bool = True
    span_id: str = field(default_factory=lambda: uuid4().hex)
    started: float = field(default_factory=monotonic)
    started_at: float = field(default_factory=time)
    first_forward: tuple[float, float] | None = None
    outcome: str = "error"

    def __post_init__(self):
        context = _CONTEXT.fullmatch(self.incoming)
        self.trace_id, parent = context.groups() if context and context[1] != _ROOT else (uuid4().hex, _ROOT)
        self.parent_id = None if parent == _ROOT else parent

    @property
    def header(self):
        return f"{self.trace_id}/{self.span_id}"

    @property
    def headers(self):
        return {HEADER: self.header} if self.enabled else {}

    def forwarded(self, received):
        if self.first_forward is None:
            delay = monotonic() - received
            self.first_forward = (time() - delay, delay)

    def _record(self, operation, span_id, parent_id, start, elapsed, outcome, labels):
        return {
            "version": "hoard.trace/v1", "trace_id": self.trace_id, "span_id": span_id,
            "parent_id": parent_id, "caller": self.caller if _NAME.fullmatch(self.caller) else "unknown",
            "callee": self.callee if _NAME.fullmatch(self.callee) else "unknown",
            "operation": operation, "label_es": labels[0], "label_en": labels[1],
            "started_at": start, "duration_ms": round(elapsed * 1000, 3),
            "outcome": outcome, "detail": {},
        }

    def emit(self, events):
        if not self.enabled:
            return
        records = [self._record("gateway.stream", self.span_id, self.parent_id, self.started_at,
                                monotonic() - self.started, self.outcome, ("Llamada a herramienta", "Tool call"))]
        if self.first_forward is not None:
            start, elapsed = self.first_forward
            records.append(self._record("gateway.forward_first", uuid4().hex, self.span_id, start, elapsed,
                                        "ok", ("Etapa", "Stage")))
        sink = getattr(events, "emit_trace_spans", None)
        if sink is None:  # Before the shared trace recorder is installed.
            for record in records:
                log.debug("Stream gateway span %s", record)
            return
        try:
            sink(records, source="hub")
        except Exception:  # Telemetry must not change the result of the HTTP request.
            log.warning("Could not record stream gateway spans")
