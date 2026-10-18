# Absorbed application identities in the Hub

The Apps dashboard, app counter, group member picker and rescan notification exclude identities explicitly declared with both `x-family.absorbed_by` and `x-family.standalone: false`. Their functions remain in the merged application and its independently controlled modules. Normal non-launchable applications remain visible.

The registry, `/api/apps`, native tool catalogues and MCP keep those identities for existing family calls and conversations. Presentation filtering does not remove tokens, data, profile memberships or integrations. It does not start, stop or restart applications. The existing visual theme and icons are unchanged.
