"""Typed test-data models (banking domain pack).

Models are the contract between pages, flows and verifiers: a Beneficiary entered on
screen is the same object later compared against the Kafka event and the back-office grid.
Add your own domain models alongside these.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Address(_Model):
    line1: str
    city: str
    pincode: str = Field(pattern=r"^\d{6}$")


class Customer(_Model):
    first_name: str
    last_name: str
    email: str
    mobile: str = Field(pattern=r"^\d{10}$")
    pan: str = Field(pattern=r"^[A-Z]{5}\d{4}[A-Z]$")
    address: Address | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Beneficiary(_Model):
    name: str
    account_number: str = Field(pattern=r"^\d{9,18}$")
    ifsc: str = Field(pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")
    nickname: str
    bank_name: str = "QA Test Bank"


class Transfer(_Model):
    mode: Literal["NEFT", "IMPS", "RTGS"] = "NEFT"
    amount: Decimal = Field(gt=0, decimal_places=2)
    remarks: str = ""

    @field_validator("amount", mode="before")
    @classmethod
    def _two_dp(cls, v: object) -> Decimal:
        return Decimal(str(v)).quantize(Decimal("0.01"))


class TransferReceipt(_Model):
    """What the application returned - captured from the confirmation screen."""

    reference: str
    status: str
    amount: Decimal
    mode: str
    beneficiary_name: str
