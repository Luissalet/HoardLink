# Copilot for standard-library HTTP servers

`hoard_link.copilot_stdlib.CopilotRelay` mounts the shared Faustus panel in a
`BaseHTTPRequestHandler` app, without adding FastAPI or a new chat loop.
Faustus keeps conversations, drafts, settings, tools and model execution.

Create one relay for the app's data directory and offer it each request before
the app's ordinary routes:

```python
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hoard_link.copilot_stdlib import CopilotRelay

copilot = CopilotRelay("disk", DATA_DIR, app_name="DiskHoard")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if copilot.handle(self):
            return
        # Continue with existing app routes.

    def do_POST(self):
        if copilot.handle(self):
            return
        # Continue with existing app routes.
```

The relay handles `/api/copilot/info`, `/api/copilot/panel.js`,
`/api/copilot/launcher.js` and `/api/copilot/proxy`. GET and HEAD serve the first
three; POST serves the proxy. Other paths return `False` to preserve app routing.
The proxy uses the same JSON envelope as the shared browser launcher: `path`,
`method`, optional `body`, or base64 `raw_body` plus `content_type`, and optional
client-version/run-id headers. Upstream HTTP statuses and bodies are preserved.
SSE bytes are flushed as they arrive. Disconnection closes the upstream stream
without repeating an operation. The response closes its connection to support
both HTTP/1.0 and HTTP/1.1 hosts without buffering the stream for its length.

For the app's HTML shell, pass the uncompressed bytes through
`copilot.inject_html(html)` before computing Content-Length and sending headers.
It adds the shared launcher once. Apps that already mount `<faustus-chat>` can
serve the relay without injecting HTML. The shared launcher honours
`window.hoardCopilot.context()` and `onTurnEnd()`, so each app supplies real
object IDs, unsaved form fields and list refreshes using its existing domain API.
This transport does not infer the app's domain objects or save its forms.

Endpoint and credential precedence matches the family: explicit `faustus_urls`
and `token` arguments, then `<APP>_FAUSTUS_URL` / `HOARD_FAUSTUS_URL` and
`HOARD_FAUSTUS_TOKEN`, then `config_path`'s `backend.json`, then the normal Faustus
ports. The optional `env` mapping supports isolated embedding and tests.
Requests bypass system HTTP proxies, and the bearer token stays on the server.
Only client-version/run-id request headers and the public contract response
headers are forwarded. JSON and multipart context origins always use the relay's
app ID and persistent `copilot-space-id` in its data directory; file attachment
bytes are preserved. No model is loaded by the relay.

Verification: `python -m pytest tests/test_copilot_stdlib.py` runs private real
HTTP servers with JSON, multipart, SSE, error, identity and configuration probes.
The shared launcher is supplied by the common copilot installer, not duplicated
in this module. App migrations still require native MCP and browser checks of
their own context, edits and refresh behavior.
