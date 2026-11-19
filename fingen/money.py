"""Money and FX arithmetic (CONTRACT §2, §4).

Amounts are integers in minor units. Conversion uses ``decimal.Decimal`` with
an explicit rounding mode; floats are never used for money.
"""

from __future__ import annotations

import decimal
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP

# CONTRACT §2: currency exponents.
CURRENCY_EXPONENTS: dict[str, int] = {
    "KZT": 2, "USD": 2, "EUR": 2, "GBP": 2, "CNY": 2, "JPY": 0, "KWD": 3,
}

ROUNDING_MODES: dict[str, str] = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
}

MAX_RATE_DECIMALS = 6

_CTX = decimal.Context(prec=60, rounding=ROUND_HALF_EVEN, traps=[
    decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow])


def exponent(currency: str) -> int:
    try:
        return CURRENCY_EXPONENTS[currency]
    except KeyError:
        raise ValueError(f"unknown currency {currency!r}") from None


def parse_rate(rate_str: str) -> Decimal:
    """Parse and validate a decimal rate string (positive, <= 6 decimals)."""
    if not isinstance(rate_str, str):
        raise TypeError("rate must be a decimal string")
    try:
        d = Decimal(rate_str)
    except decimal.InvalidOperation:
        raise ValueError(f"invalid rate {rate_str!r}") from None
    if not d.is_finite() or d <= 0:
        raise ValueError(f"rate must be positive: {rate_str!r}")
    if -d.as_tuple().exponent > MAX_RATE_DECIMALS:
        raise ValueError(f"rate has more than {MAX_RATE_DECIMALS} decimals: {rate_str!r}")
    return d


def convert(amount_minor: int, src: str, base: str, rate_str: str | None, mode: str) -> int:
    """CONTRACT §4: ``round(amount_minor * rate * 10^(exp_base - exp_src))``.

    ``rate_str`` is units of ``base`` per 1 unit of ``src``. A document already
    in the base currency is not converted (``rate_str`` is ignored).
    ``mode`` is ``"half_up"`` or ``"half_even"``; the result is an ``int``.
    Negative amounts are rounded symmetrically (half_up = away from zero).
    """
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError("amount_minor must be int")
    if src == base:
        return amount_minor
    if rate_str is None:
        raise ValueError(f"missing FX rate for {src}->{base}")
    rounding = ROUNDING_MODES[mode]
    rate = parse_rate(rate_str)
    shift = exponent(base) - exponent(src)
    value = _CTX.multiply(Decimal(amount_minor), rate)
    value = value.scaleb(shift, _CTX)
    return int(value.quantize(Decimal(1), rounding=rounding, context=_CTX))


def format_rate(value: Decimal, places: int) -> str:
    """Fixed-point decimal string with ``places`` decimals (<= 6)."""
    if not 0 <= places <= MAX_RATE_DECIMALS:
        raise ValueError("places out of range")
    q = Decimal(1).scaleb(-places)
    return format(value.quantize(q, rounding=ROUND_HALF_EVEN, context=_CTX), "f")
