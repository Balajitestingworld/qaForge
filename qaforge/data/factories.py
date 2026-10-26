"""Factories: `BeneficiaryFactory.build(nickname="Rent")` -> a valid, synthetic, traceable model.

The active DataGenerator is set per test by the pytest plugin (seeded from run id + test id),
so every test gets different-but-reproducible data. The seed is written to the evidence pack:
re-running with QAF__data__seed=<seed> regenerates exactly the same data.
"""
from __future__ import annotations

import contextvars
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

from qaforge.config.settings import DataConfig
from qaforge.data.generators import DataGenerator
from qaforge.data.models import Address, Beneficiary, Customer, Transfer

M = TypeVar("M", bound=BaseModel)
_current: contextvars.ContextVar[DataGenerator | None] = contextvars.ContextVar("qaf_datagen", default=None)


def use_generator(gen: DataGenerator) -> None:
    _current.set(gen)


def gen() -> DataGenerator:
    g = _current.get()
    if g is None:
        g = DataGenerator(DataConfig())
        _current.set(g)
    return g


class Factory(Generic[M]):
    model: ClassVar[type[BaseModel]]

    @classmethod
    def defaults(cls, g: DataGenerator) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def build(cls, **overrides: Any) -> M:
        data = cls.defaults(gen())
        data.update(overrides)
        return cls.model.model_validate(data)  # type: ignore[return-value]

    @classmethod
    def build_many(cls, n: int, **overrides: Any) -> list[M]:
        return [cls.build(**overrides) for _ in range(n)]


class AddressFactory(Factory[Address]):
    model = Address

    @classmethod
    def defaults(cls, g: DataGenerator) -> dict[str, Any]:
        return {"line1": f"{g.rng.randint(1, 999)} Test Street", "city": g.city(),
                "pincode": f"{g.rng.randint(110000, 859999)}"}


class CustomerFactory(Factory[Customer]):
    model = Customer

    @classmethod
    def defaults(cls, g: DataGenerator) -> dict[str, Any]:
        first, last = g.first_name(), g.last_name()
        return {"first_name": first, "last_name": f"{last} {g.tag_letters()}",
                "email": g.email(f"{first} {last}"), "mobile": g.mobile(), "pan": g.pan_format(last),
                "address": AddressFactory.build()}


class BeneficiaryFactory(Factory[Beneficiary]):
    model = Beneficiary

    @classmethod
    def defaults(cls, g: DataGenerator) -> dict[str, Any]:
        return {"name": g.full_name(tagged=True), "account_number": g.account_number(), "ifsc": g.ifsc(),
                "nickname": g.nickname()}


class TransferFactory(Factory[Transfer]):
    model = Transfer

    @classmethod
    def defaults(cls, g: DataGenerator) -> dict[str, Any]:
        return {"mode": "NEFT", "amount": g.amount("100.00", "25000.00"), "remarks": g.remarks()}
