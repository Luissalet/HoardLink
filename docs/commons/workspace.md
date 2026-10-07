# Shared projects through the Hub

The Hub's `workspace` facet proxies Atlas's real shared-project API through the normal authenticated app call. Atlas owns project data, membership, paths, file IDs and revisions. The Hub does not keep a second project store.

## REST

Use `GET /api/workspace/projects?sphere=work` to list projects visible to the authenticated family caller. `POST /api/workspace/call` accepts either `{"tool":"project_create","arguments":{"name":"Research","goal":"..."}}` (the existing contract) or the equivalent `operation` field. If both are supplied, they must match. Supported names are `projects`, `project`, `project_create`, `project_update`, `location`, `file_register`, `file_resolve`, `file_link_source`, `derived_publish`, `derived_lookup`, and `context`. Successful calls return Atlas's result object directly.

Identity comes from the Hub bearer token. A body `caller` is ignored. Calls with an app's token are attested to Atlas with that app's ID and retain Atlas membership enforcement. The Hub token is Atlas's operator identity. Only that Hub identity can use `file_link_source` through this facade.

Ask `location` for a shared or app-native folder, save the ordinary file there, then call `file_register` with its project-relative path. Registration hashes the existing file without copying or changing it. `file_link_source` records an explicit absolute external path as a read-only live input, without copying it; changes appear after `file_resolve`. `file_import` is deliberately absent from this facade: Atlas retains its separate explicit operator copy action.

## MCP

MCP lists one tool per Atlas operation: `hub_atlas_project_create`, `hub_atlas_project_update`, `hub_atlas_projects`, `hub_atlas_project`, `hub_atlas_location`, `hub_atlas_file_register`, `hub_atlas_file_resolve`, `hub_atlas_file_link_source`, `hub_atlas_derived_publish`, `hub_atlas_derived_lookup`, and `hub_atlas_context`. Each exposes Atlas's parameter schema directly, so required fields and enums are visible to tool selection. The compatibility tool `hub_workspace` accepts the previous `{tool, arguments}` shape; prefer granular tools where parameter guidance matters. Neither surface exposes `file_import`.

`hub_atlas_file_link_source` uses the Hub MCP token and reaches Atlas as its operator. `hub_atlas_context` requires an explicit project ID and accepts at most 20 explicit file IDs; it returns metadata and references, does not include file contents, and makes no model call. Membership controls API access, not Windows filesystem permissions.
