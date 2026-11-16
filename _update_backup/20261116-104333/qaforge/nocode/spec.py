"""No-code test definitions ("specs"): YAML files under tests/nocode/specs/<id>.yaml.

A spec is reviewable by anyone (plain YAML, readable diff in git) and executed by pytest
through tests/nocode/test_nocode_specs.py, so it gets exactly the same evidence, JIRA
publishing and parallelism as a coded test.
"""
from __future__ import annotations

import keyword as pykeyword
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qaforge.nocode import keywords as kw
from qaforge.nocode.locator_expr import LocatorExprError, parse

SPEC_DIR = Path("tests/nocode/specs")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
JIRA_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
TAG_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


class SpecError(ValueError):
    pass


class DataItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    factory: str
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v) or pykeyword.iskeyword(v):
            raise ValueError(f"'{v}' is not a valid data name (lowercase letters, digits, underscores)")
        return v


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    target: str | None = None
    locator: str | None = None
    value: Any = None
    save_as: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> Step:
        if self.target == "":
            self.target = None
        if self.locator == "":
            self.locator = None
        if self.save_as == "":
            self.save_as = None
        if self.locator:
            try:
                parse(self.locator)
            except LocatorExprError as exc:
                raise ValueError(f"locator: {exc}") from exc
        if self.save_as and (not NAME_RE.match(self.save_as) or pykeyword.iskeyword(self.save_as)):
            raise ValueError(f"'{self.save_as}' is not a valid name to remember a value under")
        problems = kw.validate_step(self.model_dump())
        if problems:
            raise ValueError("; ".join(problems))
        return self


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    description: str = ""
    reusable: bool = False  # reusable step groups are not run on their own
    jira: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    data: list[DataItem] = Field(default_factory=list)
    data_file: str | None = None
    steps: list[Step] = Field(default_factory=list)
    updated_by: str | None = None
    updated_at: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("Test id: lowercase letters, digits and underscores, starting with a letter")
        return v

    @field_validator("jira")
    @classmethod
    def _jira(cls, v: list[str]) -> list[str]:
        bad = [k for k in v if not JIRA_RE.match(k)]
        if bad:
            raise ValueError(f"Not JIRA keys: {', '.join(bad)} (expected like BANK-123)")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if not TAG_RE.match(t)]
        if bad:
            raise ValueError(f"Tags must be lowercase words: {', '.join(bad)}")
        return v

    @field_validator("data_file")
    @classmethod
    def _data_file(cls, v: str | None) -> str | None:
        if v and (".." in Path(v).parts or Path(v).is_absolute() or not v.startswith("testdata/")):
            raise ValueError("Data file must be inside testdata/")
        return v or None


def spec_dir(root: Path) -> Path:
    return root / SPEC_DIR


def spec_path(root: Path, spec_id: str) -> Path:
    if not ID_RE.match(spec_id):
        raise SpecError(f"Invalid test id '{spec_id}'")
    return spec_dir(root) / f"{spec_id}.yaml"


def load_spec(root: Path, spec_id: str) -> Spec:
    path = spec_path(root, spec_id)
    if not path.exists():
        raise SpecError(f"No no-code test '{spec_id}'")
    return Spec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def list_specs(root: Path) -> list[Spec]:
    out = []
    for f in sorted(spec_dir(root).glob("*.yaml")):
        try:
            out.append(Spec.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")) or {}))
        except Exception:  # noqa: BLE001 - one broken file must not hide the rest
            continue
    return out


def broken_specs(root: Path) -> dict[str, str]:
    bad = {}
    for f in sorted(spec_dir(root).glob("*.yaml")):
        try:
            Spec.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
        except Exception as exc:  # noqa: BLE001
            bad[f.stem] = str(exc)
    return bad


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def save_spec(root: Path, spec: Spec, user: str | None = None) -> Path:
    spec.updated_by = user
    spec.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    data = spec.model_dump(mode="json")
    data["steps"] = [_clean({k: v for k, v in s.items() if not (k == "enabled" and v is True)})
                     for s in data["steps"]]
    data = {k: v for k, v in data.items() if v not in (None, "", [], {}) or k in ("id", "title", "steps")}
    path = spec_path(root, spec.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    tmp.replace(path)
    return path


def load_rows(root: Path, data_file: str) -> list[dict[str, Any]]:
    from qaforge.data.loader import _read

    rows = _read(root / data_file)
    return [dict(r) for r in rows]


def spec_params(root: Path) -> list[Any]:
    """pytest parameters: one per spec, or one per data row for data-driven specs."""
    import pytest

    params = []
    for spec in list_specs(root):
        if spec.reusable:
            continue
        marks = [pytest.mark.jira(*spec.jira)] if spec.jira else []
        marks += [getattr(pytest.mark, t) for t in spec.tags]
        if spec.data_file:
            for i, row in enumerate(load_rows(root, spec.data_file)):
                row_id = str(row.get("id", f"row{i + 1}"))
                row_marks = list(marks)
                if row.get("jira"):
                    row_marks.append(pytest.mark.jira(*str(row["jira"]).split(",")))
                params.append(pytest.param((spec.id, row), id=f"{spec.id}-{row_id}", marks=row_marks))
        else:
            params.append(pytest.param((spec.id, None), id=spec.id, marks=marks))
    return params


def all_tags(root: Path) -> set[str]:
    return {t for s in list_specs(root) for t in s.tags}
