import json
from types import SimpleNamespace

from hoard_link.agent_flows import written_flow
from hoard_link.hub.skills import SkillsFacet


TEXT = """---
name: example
version: 1.2
---
## When to Use
When a report arrives.
## Procedure
1. Read the report.
   Keep its sources.
2. Write the result.
## Outputs
A sourced result.
## Verification
- The result links to the report.
"""


def test_written_flow_preserves_procedure_and_labels_missing_sections():
    row = {"slug": "example"}
    flow = written_flow(row, TEXT)
    assert flow["status"] == "written" and flow["version"] == "1.2"
    assert "   Keep its sources." in flow["steps"]
    assert flow["outputs"] == "A sourced result." and flow["outputs_source"] == "Outputs"
    assert len(flow["sha256"]) == 64
    fallback = written_flow(row, TEXT.replace("## Outputs\nA sourced result.\n", ""))
    assert fallback["outputs"] == fallback["verification"]
    assert fallback["outputs_source"] == "Verification"
    assert written_flow(row, "No sections")["missing"] == ["trigger", "steps", "verification"]


def test_flows_follow_current_shelf_without_starting_apps(tmp_path):
    lib = tmp_path / "library"
    (lib / "example").mkdir(parents=True)
    path = lib / "example" / "SKILL.md"
    path.write_text(TEXT, encoding="utf-8")
    manifest = tmp_path / "faustus-plugin.json"
    manifest.write_text(json.dumps({"skills": {"slugs": ["example"]}}))
    app = SimpleNamespace(id="one", name="One", data_dir=str(tmp_path / "data"), folder=str(tmp_path), manifest_path=str(manifest))
    facet = SkillsFacet(SimpleNamespace(apps=[app], config=SimpleNamespace(faustus_dir=str(lib))))
    first = facet.handlers()["hub_agent_flows"]({})
    assert first["executed"] is False
    flow = first["apps"][0]["flows"][0]
    assert flow["skill_url"] == "/api/skills/app/one/read/example"
    assert facet.flows("one", "report")["apps"][0]["count"] == 1
    assert facet.flows("one", "missing")["apps"][0]["count"] == 0
    path.write_text(TEXT.replace("Read the report.", "Read the new report."), encoding="utf-8")
    second = facet.flows("one")["apps"][0]["flows"][0]
    assert second["sha256"] != flow["sha256"] and "new report" in second["steps"]
    facet._shelf(app).assign("example", False)
    assert facet.flows("one")["apps"][0]["flows"] == []
    assert facet.flows("unknown")["status"] == 404
