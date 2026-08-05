"""Currency conversion.

Two conversion paths exist, and they are not interchangeable in provenance even
where they agree numerically:

``convert()``
    Looks the ordered pair up in the rate table and applies it. Simple, and the
    call every existing caller in this package makes. Raises
    :class:`~ledgerline.errors.MissingRate` when the table has no such pair —
    the pair table is sparse.

``convert_via_bridge()``
    Triangulates through USD using the per-currency USD legs, so it works for
    any two currencies the table prices against USD at all.

Both round exactly once, at the end, through :meth:`ledgerline.money.Money.scale`
with an exact :class:`~fractions.Fraction`, so neither accumulates drift and
neither is "more precise" than the other. What differs is which numbers in the
table the result depends on.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Mapping

from .errors import LedgerError, MissingRate
from .money import Money

#: The currency every triangulated conversion passes through.
BRIDGE_CURRENCY = "USD"


def as_fraction(value: object) -> Fraction:
    """Exact Fraction from a string, int, Decimal or Fraction. Never from a float."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if "/" in text:
            numerator, _, denominator = text.partition("/")
            return Fraction(int(numerator), int(denominator))
        return Fraction(Decimal(text))
    raise LedgerError(f"cannot read a rate from {value!r}: pass a string, int or Decimal")


@dataclass
class RateTable:
    """Ordered-pair exchange rates plus the USD legs used for triangulation.

    ``pairs[(base, quote)]`` means "one unit of ``base`` buys ``rate`` units of
    ``quote``". The table is deliberately sparse: a vendor feed prices the liquid
    pairs and leaves the rest to be triangulated.
    """

    pairs: dict[tuple[str, str], Fraction] = field(default_factory=dict)
    label: str = "rates"

    # -- construction --------------------------------------------------------
    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, object]], label: str = "rates") -> "RateTable":
        table = cls(label=label)
        for row in rows:
            table.set(str(row["base"]), str(row["quote"]), as_fraction(row["rate"]))
        return table

    @classmethod
    def load(cls, path: str | Path) -> "RateTable":
        """Read a ``base,quote,rate`` CSV."""
        path = Path(path)
        with path.open(newline="", encoding="utf-8") as handle:
            return cls.from_rows(csv.DictReader(handle), label=path.stem)

    def set(self, base: str, quote: str, rate: Fraction) -> "RateTable":
        if rate <= 0:
            raise LedgerError(f"rate must be positive: {base}->{quote} = {rate}")
        self.pairs[(base.upper(), quote.upper())] = as_fraction(rate)
        return self

    # -- lookup --------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.pairs)

    def currencies(self) -> list[str]:
        seen = {code for pair in self.pairs for code in pair}
        seen.add(BRIDGE_CURRENCY)
        return sorted(seen)

    def has_direct(self, base: str, quote: str) -> bool:
        base, quote = base.upper(), quote.upper()
        return base == quote or (base, quote) in self.pairs

    def direct(self, base: str, quote: str) -> Fraction:
        """The quoted pair rate. Raises when the feed does not carry it."""
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return Fraction(1)
        try:
            return self.pairs[(base, quote)]
        except KeyError:
            raise MissingRate(f"no direct rate for {base}->{quote} in {self.label}") from None

    def usd_leg(self, currency: str) -> Fraction:
        """How many units of ``currency`` one USD buys."""
        currency = currency.upper()
        if currency == BRIDGE_CURRENCY:
            return Fraction(1)
        pair = self.pairs.get((BRIDGE_CURRENCY, currency))
        if pair is not None:
            return pair
        inverse = self.pairs.get((currency, BRIDGE_CURRENCY))
        if inverse is not None:
            return 1 / inverse
        raise MissingRate(f"{currency} is not priced against {BRIDGE_CURRENCY} in {self.label}")

    def bridge(self, base: str, quote: str) -> Fraction:
        """The triangulated rate ``base -> USD -> quote``."""
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return Fraction(1)
        return self.usd_leg(quote) / self.usd_leg(base)

    def to_rows(self) -> list[dict]:
        return [
            {"base": base, "quote": quote, "rate": str(Decimal(rate.numerator) / rate.denominator)}
            for (base, quote), rate in sorted(self.pairs.items())
        ]


# ── the two conversion entry points ──────────────────────────────────────────
def convert(amount: Money, to_currency: str, table: RateTable) -> Money:
    """Convert using the quoted pair rate.

    This is the ordinary conversion helper: it is what
    :mod:`ledgerline.csvio` calls when an export asks for a single currency.
    """
    to_currency = to_currency.upper()
    if amount.currency == to_currency:
        return amount
    return amount.scale(table.direct(amount.currency, to_currency), currency=to_currency)


def convert_via_bridge(amount: Money, to_currency: str, table: RateTable) -> Money:
    """Convert by triangulating through :data:`BRIDGE_CURRENCY`.

    Works for any two currencies the table prices against USD, including pairs
    the feed never quotes directly.
    """
    to_currency = to_currency.upper()
    if amount.currency == to_currency:
        return amount
    return amount.scale(table.bridge(amount.currency, to_currency), currency=to_currency)


def convert_all(
    amounts: Iterable[Money], to_currency: str, table: RateTable
) -> list[Money]:
    """Convert a sequence with :func:`convert`, preserving order."""
    return [convert(amount, to_currency, table) for amount in amounts]


def sum_in(amounts: Iterable[Money], to_currency: str, table: RateTable) -> Money:
    """Total a mixed-currency sequence in ``to_currency`` using :func:`convert`."""
    to_currency = to_currency.upper()
    running = Money.zero(to_currency)
    for amount in amounts:
        running = running + convert(amount, to_currency, table)
    return running


def missing_pairs(table: RateTable, currencies: Iterable[str]) -> list[tuple[str, str]]:
    """Ordered pairs among ``currencies`` that the table cannot quote directly."""
    codes = [c.upper() for c in currencies]
    return [
        (base, quote)
        for base in codes
        for quote in codes
        if base != quote and not table.has_direct(base, quote)
    ]
