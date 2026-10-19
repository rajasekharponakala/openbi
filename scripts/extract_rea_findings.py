"""Summarise REA JavaScript-application evidence for the Power BI SDK packages.

Usage: python3 -I scripts/extract_rea_findings.py <rea-out dir> <targets dir>
Prints a JSON document (see docs/rea/powerbi-sdk-findings.json).
"""

import collections
import json
import re
import sys
from pathlib import Path

ENUMS_OF_INTEREST = {
    "FilterType", "FiltersLevel", "FiltersOperations", "AggregateFunction", "RelativeDateOperators",
    "RelativeDateFilterTimeUnit", "TokenType", "Permissions", "ViewMode", "DataCacheMode", "ExportDataType",
    "ExportDataResultFormat", "LayoutType", "PageSizeType", "VisualDataRoleKind", "SortDirection",
    "BookmarksPlayMode", "CredentialType",
}


def enums(evidence: dict) -> dict[str, list[str]]:
    graph = evidence["normalized_result"]["semantic_graph"]
    out = collections.defaultdict(list)
    for n in graph["nodes"]:
        if n["kind"] != "property-slot":
            continue
        m = re.search(r'binding:(\w+)",\["(\w+)"\]', n["identity"]["role_key"])
        if m and m.group(1) in ENUMS_OF_INTEREST and m.group(2) not in out[m.group(1)]:
            out[m.group(1)].append(m.group(2))
    return {k: sorted(v) for k, v in sorted(out.items())}


def main() -> None:
    rea_out, targets = Path(sys.argv[1]), Path(sys.argv[2])
    result = {"tool": "REA analyze-javascript-application", "packages": {}}
    for f in sorted(rea_out.glob("*.json")):
        ev = json.loads(f.read_text())
        nr = ev["normalized_result"]
        result["packages"][f.stem] = {
            "evidence_id": ev["evidence_id"],
            "artifact_sha256": nr["root_artifact_sha256"],
            "confidence": ev["confidence"],
            "authority": ev["authority"],
            "statistics": nr["statistics"],
            "enums": enums(ev),
        }
    client = (targets / "powerbi-client-2.25.0" / "package" / "dist" / "powerbi.js").read_text()
    result["embed_routes"] = sorted(set(re.findall(r'"(/(?:report|reports|dashboard|visuals?)[A-Za-z/:{}]*)"', client)))
    result["embed_events"] = [json.loads("[" + m + "]") for m in re.findall(r"allowedEvents = \[([^\]]*)\]", client)]
    models = (targets / "powerbi-models-2.2.0" / "package" / "dist" / "models.js").read_text()
    result["filter_schemas"] = sorted(set(re.findall(r'"(http://powerbi\.com/product/schema#\w+)"', models)))
    result["filter_type_values"] = dict(
        sorted(((k, int(v)) for k, v in re.findall(r'FilterType\[FilterType\["(\w+)"\] = (\d+)\]', models)), key=lambda kv: kv[1])
    )
    caps = json.loads((targets / "powerbi-visuals-api-5.11.1" / "package" / "schema.capabilities.json").read_text())
    result["visual_capabilities_properties"] = list(caps["properties"])
    json.dump(result, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
