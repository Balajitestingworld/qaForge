"""ScenarioContext - the memory of a test.

Everything entered into or captured from the application is saved here by key. Verifiers
later read the same values to check Kafka events, databases, APIs and other applications,
so "what we typed" and "what we expect downstream" can never drift apart.

    payee = ctx.save("payee", BeneficiaryFactory.build())
    ctx.save("txn_ref", receipt.reference)
    ctx.get_path("payee.account_number")      # dotted access for mappings
"""
from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from qaforge.compliance.logger import get_logger
from qaforge.compliance.masker import masker

if TYPE_CHECKING:
    from qaforge.data.ledger import RunLedger

log = get_logger("context")
_MISSING = object()


class ScenarioContext(MutableMapping[str, Any]):
    def __init__(self, test_id: str, ledger: RunLedger | None = None) -> None:
        self.test_id = test_id
        self._ledger = ledger
        self._data: dict[str, Any] = {}

    # -- MutableMapping ------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        if key not in self._data:
            raise KeyError(f"'{key}' was never saved in this scenario. Saved keys: {sorted(self._data)}")
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    # -- API -----------------------------------------------------------------
    def save(self, key: str, value: Any, *, entity: str | None = None, ledger_key: str | None = None) -> Any:
        """Store a value. `entity=` also records it in the run ledger immediately - use that for values
        the app has already confirmed (e.g. a captured reference). For data you are about to submit,
        call `mark_created()` after the app confirms creation."""
        self._data[key] = value
        log.info("ctx[%s] = %s", key, masker.mask_obj(_plain(value)))
        if entity and self._ledger is not None:
            self._ledger.record(entity, ledger_key or _default_key(_plain(value)), _plain(value),
                                test_id=self.test_id)
        return value

    def mark_created(self, key: str, entity: str, ledger_key: str | None = None) -> None:
        """Record ctx[key] in the run ledger once the application has confirmed it was created."""
        if self._ledger is None:
            return
        value = _plain(self[key])
        self._ledger.record(entity, ledger_key or _default_key(value), value, test_id=self.test_id)

    def get_path(self, path: str, default: Any = _MISSING) -> Any:
        """Dotted lookup through dicts, models and lists: 'payee.account_number', 'items.0.sku'."""
        head, *rest = path.split(".")
        if head not in self._data:
            if default is not _MISSING:
                return default
            raise KeyError(f"'{head}' not saved in context (path '{path}')")
        node: Any = self._data[head]
        for part in rest:
            if isinstance(node, BaseModel):
                node = getattr(node, part)
            elif isinstance(node, dict):
                node = node[part]
            elif isinstance(node, (list, tuple)):
                node = node[int(part)]
            else:
                node = getattr(node, part)
        return node

    def snapshot(self) -> dict[str, Any]:
        return {k: _plain(v) for k, v in self._data.items()}


def _default_key(value: Any) -> str:
    if isinstance(value, dict):
        for k in ("reference", "id", "account_number", "email", "name"):
            if k in value:
                return str(value[k])
    return str(value)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value
