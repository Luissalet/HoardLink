# Chat extension methods and deadlines

`Link.chat(timeout=...)` bounds the whole call, including resolution, leases,
extension work, reasoning negotiation and HTTP. Omitting timeout retains the
existing backend timeout selected by reasoning effort. Neither setting changes
streaming adoption or the server configuration.

Extensions that override `_post_chat(self, provider, url, payload, level)` keep
that argument list. Calls to `super()._post_chat(provider, url, payload, level)`
use the current chat's remaining deadline at every HTTP attempt, including the
existing negotiation after a reasoning-field rejection. No TypeError handler or
new retry is used to discover an extension's signature.

The `_chat_openai` and `_chat_ollama` argument lists also stay as they were before
deadline propagation. An async task context carries the deadline instead of an
extra positional argument. It resets on success, error and cancellation;
concurrent calls are isolated, and a nested call restores the caller's context.
A nested chat with omitted timeout retains its ordinary backend default; the
outer call's enclosing timeout still bounds the task.

An override that sends HTTP itself, instead of delegating to `_post_chat`, keeps
its own HTTP timeout policy. The enclosing explicit `chat` timeout still applies
to its awaited work. This does not increase a custom extension's shorter HTTP
timeout or convert detached/background work into awaited work. Extensions should
delegate when they want the shared reasoning negotiation and remaining HTTP
budget. Direct calls of private hooks outside `chat` are not a replacement for
the public call's whole deadline.

Regression tests cover four-argument overrides, both API dialect hooks, omitted
and explicit timeouts, a 421ms TCP reply with a 900ms caller budget despite a
300ms default, explicit expiry, negotiation sharing one deadline, nested error
and restoration, concurrent calls on one Link, cancellation, extension-internal
TypeError and the native private Hub/family route. Fixtures are synthetic and
do not call a model. A separate QA receipt compiles Coase's original ResearchLink
class in memory and exercises it against a private fixed TCP response, without
importing the application or copying its source. Native MCP stdio and browser
QA use that same private Hub with a four-argument delegating extension; those
checks do not certify a live Coase process or any model's quality.
