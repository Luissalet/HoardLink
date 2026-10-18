# CatalogBridge and MCP SDK result contracts

`CatalogBridge` forwards a tool call once to the app's native API and returns
the response over MCP stdio. A successful HTTP response must not turn into a
tool error merely because an older SDK expects a different handler return type.

The supported regression matrix exercises MCP 1.9.0 with Pydantic 2.10.3,
MCP 1.30.0, and MCP 2.3.0 in separate environments. It does not change an app's
dependency pins or vendor its dependencies.

## Result handling

SDK 1.9's FastMCP tool adapter expects an iterable of content blocks. Returning
a complete `CallToolResult` makes it iterate the model's fields and then reject
those tuples as content. Returning only a list would discard the result's error
flag: that SDK's convenience adapter constructs a fresh result with
`isError=false`.

For SDK 1.x, the bridge therefore registers a typed `CallToolRequest` handler
in FastMCP's existing native dispatcher. It returns the SDK's
`ServerResult(CallToolResult(...))` envelope directly. Both 1.9 and 1.30 use that
dispatcher contract. The ordinary FastMCP transport, request context,
initialization, tool listing and cancellation remain SDK-owned. Unexpected
exceptions are not swallowed by the adapter. SDK 2.x already accepts the typed
result through `MCPServer.call_tool`.

The result always contains the text/image blocks and the real `isError` value.
For a dictionary body, it also contains `structuredContent` when that field is
declared by the installed SDK's actual `CallToolResult` schema. This is schema
capability detection, not a guessed version threshold. Wire aliases are validated
by the SDK itself, including SDK 2.x's Python snake-case fields.

SDK 1.9's declared result schema has no structured-content field. The bridge
does not invent support for it: the complete JSON body remains in the text
block. Non-object JSON bodies also remain text because structured content is an
object in the MCP contract. Error JSON and image blocks keep their existing
meaning rather than being repackaged as an exception string.

## Catalog and deadlines

The timeout hint remains in `inputSchema["x-timeout-s"]` and also at the tool's
top level where the SDK retains extension fields. SDK 2.x filters undeclared
top-level extras, so its schema hint is the portable discovery path.

This result adapter does not alter timeout selection. A declared 900-second
hint can outlive a short bridge default; an explicit bridge tool override still
applies. A timed-out write returns `isError=true` and `outcome_unknown` with its
reconciliation instruction. Cancellation propagates without replaying the HTTP
operation. Notifications are waiting signals, not fabricated upstream progress.

## Verification

Run `tests/commons/test_bridge_mcp_contract.py` separately with each SDK. These
tests start a real stdio server and a private local HTTP API. They verify exact
Unicode JSON, text plus image, error flags, structured-content availability,
catalog hints, long-call timeout selection, an explicit short write cap, and a
cancelled call followed by another successful call. Request counts prove that
operations were not replayed. No app, Hub, model or network service outside the
fixture is required.

## Primary contracts

- [SDK 1.9 FastMCP handlers](https://github.com/modelcontextprotocol/python-sdk/blob/v1.9.0/src/mcp/server/fastmcp/server.py)
- [SDK 1.9 typed dispatcher and iterable adapter](https://github.com/modelcontextprotocol/python-sdk/blob/v1.9.0/src/mcp/server/lowlevel/server.py)
- [SDK 1.9 result types](https://github.com/modelcontextprotocol/python-sdk/blob/v1.9.0/src/mcp/types.py)
- [SDK 1.30 typed-result normalization](https://github.com/modelcontextprotocol/python-sdk/blob/v1.30.0/src/mcp/server/lowlevel/server.py)
- [SDK 2.3 MCPServer handlers](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/server/mcpserver/server.py)
- [MCP tool result and structured-content specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
