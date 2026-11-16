"""Data-driven tests from YAML / JSON / CSV files.

    @pytest.mark.parametrize("case", load_cases("testdata/transfer_limits.yaml"))
    def test_limits(case): ...

Each row may carry `id` (test id in reports) and `jira` (linked test case key).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
import yaml


def _read(path: Path) -> list[dict[str, Any]]:
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    raise ValueError(f"Unsupported test data file: {path}")


def _project_root() -> Path:
    here = Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists() or (candidate / "pytest.ini").exists():
            return candidate
    return here


def load_cases(path: str | Path, root: str | Path | None = None) -> list[Any]:
    p = Path(path)
    if not p.is_absolute():
        base = Path(root) if root else _project_root()
        p = base / p
    params = []
    for i, row in enumerate(_read(p)):
        marks = [pytest.mark.jira(*str(row["jira"]).split(","))] if row.get("jira") else []
        params.append(pytest.param(row, id=str(row.get("id", f"case{i + 1}")), marks=marks))
    return params
