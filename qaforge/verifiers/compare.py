"""Field-level comparison of observed data (event, DB row, API body, UI grid) with what the test entered.

Mappings live in YAML (config/mappings/*.yaml) so BAs / reviewers can read them:

    name: payments.initiated matches the transfer entered in the UI
    fields:
      - {field: txnRef,                   expected: ctx.txn_ref}
      - {field: amount,                   expected: ctx.transfer.amount, rule: decimal}
      - {field: beneficiary.accountNumber, expected: ctx.payee.account_number}
      - {field: currency,                 expected: INR}
      - {field: createdAt,                rule: exists}

`expected` starting with "ctx." is resolved from the ScenarioContext; anything else is a literal.
Every row (match or not) goes into the evidence pack as an expected/actual table.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from qaforge.compliance.evidence import EvidencePack, VerificationRecord
from qaforge.compliance.logger import get_logger
from qaforge.data.context import ScenarioContext

log = get_logger("verify")
_MISSING = "<missing>"


class VerificationError(AssertionError):
    pass


@dataclass
class FieldRule:
    field: str
    expected: Any = None
    rule: str = "equals"
    description: str = ""


@dataclass
class FieldMapping:
    name: str
    fields: list[FieldRule] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FieldMapping:
        return cls(name=data.get("name", "verification"),
                   fields=[FieldRule(**f) for f in data.get("fields", [])])

    @classmethod
    def load(cls, name_or_path: str, mappings_dir: Path | None = None) -> FieldMapping:
        p = Path(name_or_path)
        if not p.suffix:
            p = (mappings_dir or Path("config/mappings")) / f"{name_or_path}.yaml"
        return cls.from_dict(yaml.safe_load(p.read_text(encoding="utf-8")))


def get_path(obj: Any, path: str) -> Any:
    node = obj
    for part in path.split("."):
        if node is None:
            return _MISSING
        if isinstance(node, BaseModel):
            node = getattr(node, part, _MISSING)
        elif isinstance(node, dict):
            node = node.get(part, _MISSING)
        elif isinstance(node, (list, tuple)) and part.isdigit():
            idx = int(part)
            node = node[idx] if idx < len(node) else _MISSING
        else:
            node = getattr(node, part, _MISSING)
        if node is _MISSING:
            return _MISSING
    return node


def _as_decimal(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v).replace(",", "").replace("₹", "").replace("INR", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _check(rule: str, expected: Any, actual: Any) -> bool:
    if rule == "exists":
        return actual is not _MISSING and actual not in (None, "")
    if actual is _MISSING:
        return False
    if rule == "equals":
        return str(expected).strip() == str(actual).strip()
    if rule == "equals_ci":
        return str(expected).strip().lower() == str(actual).strip().lower()
    if rule == "decimal":
        a, e = _as_decimal(actual), _as_decimal(expected)
        return a is not None and e is not None and a == e
    if rule == "contains":
        return str(expected) in str(actual)
    if rule == "regex":
        return re.fullmatch(str(expected), str(actual)) is not None
    if rule == "one_of":
        return str(actual) in {str(x) for x in (expected or [])}
    raise ValueError(f"Unknown comparison rule '{rule}'")


def resolve_expected(expected: Any, ctx: ScenarioContext | None) -> Any:
    if isinstance(expected, str) and expected.startswith("ctx."):
        if ctx is None:
            raise ValueError(f"Mapping expects context value '{expected}' but no context was given")
        return ctx.get_path(expected[4:])
    return expected


def verify(
    actual: Any,
    mapping: FieldMapping | list[dict[str, Any]] | str,
    *,
    ctx: ScenarioContext | None = None,
    evidence: EvidencePack | None = None,
    source: str = "",
    name: str | None = None,
    soft: bool = False,
) -> VerificationRecord:
    if isinstance(mapping, str):
        mapping = FieldMapping.load(mapping)
    elif isinstance(mapping, list):
        mapping = FieldMapping(name=name or "verification", fields=[FieldRule(**f) for f in mapping])
    title = name or mapping.name

    rows: list[dict[str, Any]] = []
    for fr in mapping.fields:
        expected = resolve_expected(fr.expected, ctx) if fr.rule != "exists" else "(present)"
        got = get_path(actual, fr.field)
        ok = _check(fr.rule, expected, got)
        rows.append({"field": fr.field, "expected": _show(expected), "actual": _show(got),
                     "rule": fr.rule, "match": ok})

    record = VerificationRecord(name=title, source=source, passed=all(r["match"] for r in rows), rows=rows)
    if evidence is not None:
        evidence.add_verification(record)
    mismatches = [r for r in record.rows if not r["match"]]  # rows are masked once added to evidence
    if record.passed:
        log.info("VERIFIED %s (%d fields, source=%s)", title, len(rows), source)
    else:
        lines = "\n".join(f"  - {r['field']}: expected {r['expected']!r} [{r['rule']}], actual {r['actual']!r}"
                          for r in mismatches)
        msg = f"{title}: {len(mismatches)} of {len(rows)} fields differ (source={source})\n{lines}"
        log.error(msg)
        if not soft:
            raise VerificationError(msg)
    return record


def _show(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, BaseModel):
        return v.model_dump(mode="json")
    return v
