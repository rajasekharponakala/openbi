"""Project folders: a git-friendly on-disk format (like Power BI's PBIP).

my-project/
  openbi.yaml                 # name + list of reports
  model.yaml                  # semantic model (tables, measures, relationships, roles)
  reports/<name>.report.json  # report definition (pages, visuals, filters)
  data/                       # optional local data files
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .schema import SemanticModel


@dataclass
class Project:
    root: Path
    name: str
    model: SemanticModel
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_model(path: str | Path) -> SemanticModel:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return SemanticModel.model_validate(data)


def dump_model(model: SemanticModel) -> str:
    data = model.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True, mode="json")
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def load_project(root: str | Path) -> Project:
    root = Path(root)
    manifest_path = root / "openbi.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    model = load_model(root / manifest.get("model", "model.yaml"))
    reports: dict[str, dict[str, Any]] = {}
    for rp in sorted((root / "reports").glob("*.report.json")) if (root / "reports").exists() else []:
        reports[rp.name.removesuffix(".report.json")] = json.loads(rp.read_text(encoding="utf-8"))
    return Project(root, manifest.get("name", model.name), model, reports)


def save_project(project: Project) -> None:
    root = project.root
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "openbi.yaml").write_text(
        yaml.safe_dump({"name": project.name, "model": "model.yaml"}, sort_keys=False), encoding="utf-8"
    )
    (root / "model.yaml").write_text(dump_model(project.model), encoding="utf-8")
    for name, report in project.reports.items():
        (root / "reports" / f"{name}.report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
