"""Monetary amounts held as integer minor units.

Floats never touch a balance in this package. A :class:`Money` is an integer
count of minor units (cents, pence, yen) plus an ISO-4217-ish currency code, so
addition and subtraction are exact and the only place rounding can happen is
:meth:`Money.scale`, which takes a :class:`fractions.Fraction` and rounds once,
half-to-even, at the very end of a calculation.

The exponent table is deliberately small: this is a teaching ledger, not a
treasury system. Unknown currencies default to two decimal places.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Iterable

from .errors import CurrencyMismatch, LedgerError

#: Minor-unit exponent per currency. Anything absent uses ``DEFAULT_EXPONENT``.
EXPONENTS: dict[str, int] = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CHF": 2,
    "JPY": 0,
    "KRW": 0,
}

DEFAULT_EXPONENT = 2

#: Currency symbols accepted (and emitted) by the text format.
SYMBOLS: dict[str, str] = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}


def exponent_of(currency: str) -> int:
    """Minor-unit exponent for ``currency``."""
    return EXPONENTS.get(currency.upper(), DEFAULT_EXPONENT)


def _round_half_even(numerator: int, denominator: int) -> int:
    """Round ``numerator / denominator`` to the nearest integer, ties to even.

    Written out rather than delegated to :func:`round` so the behaviour is
    identical on every Python version and independent of float representation.
    """
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    q, r = divmod(numerator, denominator)
    twice = 2 * r
    if twice > denominator:
        return q + 1
    if twice < denominator:
        return q
    return q + 1 if q % 2 else q


@dataclass(frozen=True, order=True)
class Money:
    """An exact monetary amount.

    ``minor`` is the signed count of minor units; ``currency`` is upper-cased on
    construction. Instances are hashable and orderable within one currency.
    """

    minor: int
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", str(self.currency).upper())
        if not self.currency.isalpha() or not 3 <= len(self.currency) <= 4:
            raise LedgerError(f"not a currency code: {self.currency!r}")
        object.__setattr__(self, "minor", int(self.minor))

    # -- construction --------------------------------------------------------
    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(0, currency)

    @classmethod
    def parse(cls, text: str, currency: str) -> "Money":
        """Parse a decimal string into minor units.

        Accepts a leading sign, thousands separators, a bracketed negative in
        the accounting style ``(12.50)``, and an optional currency symbol.
        """
        raw = str(text).strip()
        if not raw:
            raise LedgerError("empty amount")
        negative = False
        if raw.startswith("(") and raw.endswith(")"):
            negative = True
            raw = raw[1:-1].strip()
        for sym in SYMBOLS.values():
            raw = raw.replace(sym, "")
        raw = raw.replace(",", "").replace("_", "").strip()
        if raw.startswith("+"):
            raw = raw[1:]
        if raw.startswith("-"):
            negative = not negative
            raw = raw[1:]
        if not raw:
            raise LedgerError(f"not an amount: {text!r}")
        try:
            dec = Decimal(raw)
        except Exception as exc:  # noqa: BLE001 - Decimal raises a bare ArithmeticError
            raise LedgerError(f"not an amount: {text!r}") from exc
        exp = exponent_of(currency)
        scaled = dec * (10**exp)
        minor = _round_half_even(*Fraction(scaled).as_integer_ratio())
        return cls(-minor if negative else minor, currency)

    # -- arithmetic ----------------------------------------------------------
    def _same(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(
                f"cannot combine {self.currency} and {other.currency} without conversion"
            )

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.minor, self.currency)

    def __abs__(self) -> "Money":
        return Money(abs(self.minor), self.currency)

    def __bool__(self) -> bool:
        return self.minor != 0

    def scale(self, factor: Fraction, currency: str | None = None) -> "Money":
        """Multiply by an exact ``factor``, rounding half-to-even exactly once.

        ``currency`` re-denominates the result, which is what makes this the one
        primitive every conversion path in :mod:`ledgerline.fx` shares: the
        minor-unit exponent of the target is applied here, not by the caller.
        """
        if not isinstance(factor, Fraction):
            factor = Fraction(factor)
        target = (currency or self.currency).upper()
        shift = exponent_of(target) - exponent_of(self.currency)
        value = Fraction(self.minor) * factor
        if shift > 0:
            value *= 10**shift
        elif shift < 0:
            value /= 10 ** (-shift)
        return Money(_round_half_even(value.numerator, value.denominator), target)

    # -- rendering -----------------------------------------------------------
    @property
    def units(self) -> Decimal:
        """The amount as a :class:`~decimal.Decimal` in major units."""
        exp = exponent_of(self.currency)
        return Decimal(self.minor).scaleb(-exp)

    def format(self, *, with_currency: bool = True, grouping: bool = False) -> str:
        exp = exponent_of(self.currency)
        sign = "-" if self.minor < 0 else ""
        whole, frac = divmod(abs(self.minor), 10**exp) if exp else (abs(self.minor), 0)
        body = f"{whole:,}" if grouping else str(whole)
        if exp:
            body = f"{body}.{frac:0{exp}d}"
        return f"{sign}{body} {self.currency}" if with_currency else f"{sign}{body}"

    def __str__(self) -> str:
        return self.format()


def total(amounts: Iterable[Money], currency: str | None = None) -> Money:
    """Sum ``amounts``, which must share one currency."""
    items = list(amounts)
    if not items:
        if currency is None:
            raise LedgerError("cannot total an empty sequence without a currency")
        return Money.zero(currency)
    acc = items[0]
    for item in items[1:]:
        acc = acc + item
    if currency is not None and acc.currency != currency.upper():
        raise CurrencyMismatch(f"expected {currency}, summed {acc.currency}")
    return acc


def split_by_currency(amounts: Iterable[Money]) -> dict[str, Money]:
    """Group ``amounts`` into one total per currency, in first-seen order."""
    out: dict[str, Money] = {}
    for amount in amounts:
        if amount.currency in out:
            out[amount.currency] = out[amount.currency] + amount
        else:
            out[amount.currency] = amount
    return out
