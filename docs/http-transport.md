# Hub HTTP transport

`fetch` and `fetch_detailed` build a separate urllib opener per call. For plain
HTTP they retain the standard handlers except HTTPSHandler: constructing that
unused handler on Python 3.13 eagerly loads TLS certificates, including the
Windows certificate stores. HTTP does not require that initialization.

HTTPS and other schemes keep the original `build_opener` call, before the
request exception handler. TLS defaults and constructor exceptions are unchanged.
Environment proxies remain disabled; redirects are refused so credentials stay
with the configured owner. Request bodies, headers, timeouts and result shapes
are unchanged. No opener or TLS context is shared between calls.

CPU tests use synthetic responses and forbid sockets and certificate-store reads.
They reproduce the former TLS-construction pause with a bounded gate and asyncio
heartbeat, then check that HTTP no longer enters it. HTTPS, redirects, errors and
concurrent opener independence are checked separately.

This removes unnecessary TLS initialization from HTTP. The fetch functions are
still synchronous; it does not establish event-loop availability during network
I/O or certify application health.

App health probes and startup POSTs in `hub.procs` also avoid loading certificate
stores for plain HTTP. Their HTTPS handler initializes the standard TLS context
only when an HTTPS request is opened, including an allowed HTTP-to-HTTPS redirect.
Certificate verification, disabled proxies, optional redirect refusal, response
size limits and timeouts keep their existing behavior. This prevents repeated
local catalogue polling from repeatedly loading the Windows certificate stores.
