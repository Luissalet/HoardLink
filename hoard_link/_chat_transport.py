"""The Hub's shared Link transport for cleartext and TLS backends."""
from __future__ import annotations

import ssl

import httpx


class ChatTransport(httpx.AsyncBaseTransport):
    def __init__(self, limits: httpx.Limits):
        self._limits = limits
        self._http: httpx.AsyncHTTPTransport | None = None
        self._https: httpx.AsyncHTTPTransport | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            if self._http is None:
                # HTTPX otherwise loads the CA bundle while constructing a pool,
                # even when this pool only ever opens cleartext connections.
                self._http = httpx.AsyncHTTPTransport(
                    verify=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), trust_env=False, limits=self._limits,
                )
            return await self._http.handle_async_request(request)
        if request.url.scheme == "https":
            if self._https is None:
                self._https = httpx.AsyncHTTPTransport(trust_env=False, limits=self._limits)
            return await self._https.handle_async_request(request)
        raise httpx.UnsupportedProtocol("The Link backend must use HTTP or HTTPS", request=request)

    async def aclose(self) -> None:
        try:
            if self._http is not None:
                await self._http.aclose()
        finally:
            if self._https is not None:
                await self._https.aclose()
