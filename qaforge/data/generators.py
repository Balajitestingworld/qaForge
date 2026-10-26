"""Synthetic, format-valid banking data. Deterministic per seed, no external dependency.

Everything generated here is fake by construction:
  * card numbers use test BINs (configurable) and pass Luhn
  * IFSC codes use configurable *test* bank codes
  * PAN values follow the format only (AAAPA9999A), never a real person's PAN
  * e-mails use the reserved .test domain; mobiles use a configurable prefix
Every generator embeds the run tag where it can, so data is traceable and cleanable.
"""
from __future__ import annotations

import random
import string
from decimal import ROUND_HALF_UP, Decimal

from qaforge.config.settings import DataConfig

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Arjun", "Sai", "Reyansh", "Krishna", "Ishaan", "Rohan", "Kabir",
    "Ananya", "Diya", "Aadhya", "Saanvi", "Kavya", "Meera", "Priya", "Lakshmi", "Nandini", "Isha",
    "Rahul", "Vikram", "Suresh", "Ramesh", "Deepa", "Anjali", "Pooja", "Karthik", "Naveen", "Sneha",
]
LAST_NAMES = [
    "Sharma", "Verma", "Iyer", "Reddy", "Nair", "Patel", "Gupta", "Rao", "Menon", "Kulkarni",
    "Joshi", "Desai", "Banerjee", "Chatterjee", "Pillai", "Kapoor", "Mehta", "Shetty", "Naidu", "Singh",
]
CITIES = ["Bengaluru", "Mumbai", "Chennai", "Hyderabad", "Pune", "Delhi", "Kolkata", "Kochi", "Mysuru"]


def luhn_check_digit(partial: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(partial)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


class DataGenerator:
    def __init__(self, cfg: DataConfig, seed: int | str | None = None, run_tag: str = "QA") -> None:
        self.cfg = cfg
        self.seed = seed
        self.rng = random.Random(seed)
        self.run_tag = run_tag.upper()[:6]
        self._seq = 0

    def _digits(self, n: int) -> str:
        return "".join(self.rng.choice(string.digits) for _ in range(n))

    def _letters(self, n: int) -> str:
        return "".join(self.rng.choice(string.ascii_uppercase) for _ in range(n))

    def seq(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------- people
    def first_name(self) -> str:
        return self.rng.choice(FIRST_NAMES)

    def last_name(self) -> str:
        return self.rng.choice(LAST_NAMES)

    def full_name(self, tagged: bool = True) -> str:
        name = f"{self.first_name()} {self.last_name()}"
        # letters-only suffix keeps the name valid for strict name validators
        return f"{name} {self.tag_letters()}" if tagged else name

    def tag_letters(self) -> str:
        return "Qa" + self._letters(3).lower()

    def email(self, name: str | None = None) -> str:
        base = (name or self.full_name(tagged=False)).lower().replace(" ", ".")
        return f"{base}.{self.run_tag.lower()}{self.seq()}@{self.cfg.email_domain}"

    def mobile(self) -> str:
        prefix = self.cfg.mobile_prefix
        return prefix + self._digits(10 - len(prefix))

    def city(self) -> str:
        return self.rng.choice(CITIES)

    def pan_format(self, last_name: str | None = None) -> str:
        """Format-valid PAN-like string: AAA + P (individual) + surname initial + 4 digits + letter."""
        initial = (last_name or self.last_name())[0].upper()
        return f"{self._letters(3)}P{initial}{self._digits(4)}{self._letters(1)}"

    # ------------------------------------------------------------ banking
    def account_number(self, length: int | None = None) -> str:
        n = length or self.cfg.account_number_length
        return str(self.rng.randint(1, 9)) + self._digits(n - 1)

    def ifsc(self, bank_code: str | None = None) -> str:
        code = (bank_code or self.rng.choice(self.cfg.ifsc_bank_codes)).upper()[:4].ljust(4, "X")
        return f"{code}0{self._digits(6)}"

    def card_number(self, bin_prefix: str | None = None, length: int = 16) -> str:
        prefix = bin_prefix or self.rng.choice(self.cfg.test_card_bins)
        body = prefix + self._digits(length - len(prefix) - 1)
        return body + luhn_check_digit(body)

    def amount(self, low: str | float = "1.00", high: str | float = "50000.00") -> Decimal:
        lo, hi = Decimal(str(low)), Decimal(str(high))
        cents = self.rng.randint(int(lo * 100), int(hi * 100))
        return (Decimal(cents) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def reference(self, prefix: str = "QA") -> str:
        return f"{prefix}{self.run_tag}{self.seq():04d}{self._letters(2)}"

    def remarks(self) -> str:
        return f"{self.run_tag} automated test {self.seq()}"

    def nickname(self) -> str:
        return f"{self.first_name()}{self._letters(2).lower()}"
