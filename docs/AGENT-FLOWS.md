# Agent flows by app

System → Skills → Show flows reads each app's current shelf without starting the app.
Search by app or procedure, expand a flow, read its trigger, full steps and expected
outputs/checks, open the complete skill, or choose skills on the existing shelf page.

`GET /api/skills/flows?app=ID&q=WORDS` and MCP `hub_agent_flows {app?, query?}`
return the same catalogue. Empty app means all apps, including empty shelves.
Each flow links to its skill and includes its version, selection reason and SHA256.
The trigger is `When to Use`, steps are `Procedure`, and outputs are `Outputs`
when supplied, otherwise the expected results in `Verification`; `outputs_source`
identifies that choice. Missing sections are marked incomplete, unreadable copies
unavailable. Procedures are preserved as Markdown, never inferred from app names.

Shelf choices and refreshed library contents are reflected on the next read. A cached
shelf still works when the library is unavailable. These are written agent procedures,
not executable recipes or claims of a successful run. Existing skill editing, selection,
refresh and MCP prompts remain available.
