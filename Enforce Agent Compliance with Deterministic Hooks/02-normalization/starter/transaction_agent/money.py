"""Heterogeneous-format normalizers: currency strings, timestamps, numeric status codes.

These exist because upstream tools emit data in whatever format their source system uses. The
PostToolUse normalization hook is the single place that canonicalizes them, so the model only
ever reasons over one representation. Currency amounts are exact ``Decimal`` — never ``float``.

In this step you implement the four normalizers (``_parse_amount``, ``normalize_currency``,
``normalize_timestamp``, ``normalize_status``). The currency-detection helper, the typed error
classes, the lookup tables, and ``coerce_money`` are provided.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from transaction_agent.config import THRESHOLD_CURRENCY
from transaction_agent.models import Money

#: Symbol / ISO-code → ISO-4217 currency.
_SYMBOL_CURRENCY = {"$": "USD", "£": "GBP", "€": "EUR"}
_KNOWN_CODES = {"USD", "EUR", "GBP"}

#: Numeric account status codes → canonical labels.
_STATUS_LABELS = {1: "active", 2: "dormant", 3: "frozen"}


class CurrencyParseError(ValueError):
    """Raised when a currency string cannot be parsed. Carries the offending raw value."""

    def __init__(self, raw: str) -> None:
        super().__init__(f"cannot parse currency value: {raw!r}")
        self.raw = raw


class TimestampParseError(ValueError):
    """Raised when a timestamp cannot be parsed. Carries the offending raw value."""

    def __init__(self, raw: object) -> None:
        super().__init__(f"cannot parse timestamp value: {raw!r}")
        self.raw = raw


class StatusCodeError(ValueError):
    """Raised on an unknown numeric status code. Carries the offending raw value."""

    def __init__(self, raw: object) -> None:
        super().__init__(f"unknown status code: {raw!r}")
        self.raw = raw


def _detect_currency(raw: str) -> str:
    for symbol, code in _SYMBOL_CURRENCY.items():
        if symbol in raw:
            return code
    for code in _KNOWN_CODES:
        if code in raw.upper():
            return code
    raise CurrencyParseError(raw)


def _parse_amount(numeric: str, raw: str) -> Decimal:
    """Interpret a digits-and-separators string under both US and EU conventions.

    "," and "." swap roles between locales ("$1,234.56" vs "EUR 1.234,56"), so the
    separators are disambiguated by position rather than assumed.
    """
    has_comma = "," in numeric
    has_dot = "." in numeric

    if has_comma and has_dot:
        # Whichever separator appears last is the decimal point; the other groups thousands.
        if numeric.rindex(",") > numeric.rindex("."):
            normalized = numeric.replace(".", "").replace(",", ".")
        else:
            normalized = numeric.replace(",", "")
    elif has_comma:
        # Exactly three trailing digits reads as grouped thousands ("1,234"); anything
        # else is a decimal comma ("1,56").
        _, _, tail = numeric.rpartition(",")
        normalized = numeric.replace(",", "") if len(tail) == 3 else numeric.replace(",", ".")
    else:
        normalized = numeric

    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise CurrencyParseError(raw) from exc


def normalize_currency(raw: str) -> Money:
    """Parse a currency string in any supported format into an exact :class:`Money`."""
    currency = _detect_currency(raw)
    numeric = re.sub(r"[^0-9.,]", "", raw)
    if not numeric:
        raise CurrencyParseError(raw)
    return Money(amount=_parse_amount(numeric, raw), currency=currency)


def normalize_timestamp(raw: object) -> str:
    """Convert a Unix epoch (int / float / numeric string) to an ISO-8601 UTC string.

    Already-ISO-8601 input is returned unchanged (so the hook is idempotent).
    """
    # TODO: Reject bool explicitly (it is an int subclass). For int/float, return
    # datetime.fromtimestamp(float(raw), UTC).isoformat(). For a str: if it is all digits, treat
    # it as an epoch; otherwise validate it parses as ISO-8601 (datetime.fromisoformat) and
    # return it unchanged. Anything else raises TimestampParseError(raw).
    if isinstance(raw, bool):
        raise TimestampParseError(raw)
    if isinstance(raw, int) or isinstance(raw, float):
        return datetime.fromtimestamp(float(raw), UTC).isoformat()
    if isinstance(raw, str):
        if raw.isdigit():
            return datetime.fromtimestamp(float(raw), UTC).isoformat()
        # Validate but return verbatim, so an already-ISO value survives untouched.
        try:
            datetime.fromisoformat(raw)
        except ValueError as exc:
            raise TimestampParseError(raw) from exc
        return raw
    raise TimestampParseError(raw)


def coerce_money(value: object) -> Money:
    """Coerce a tool-input amount (number, currency string, or Money dict) to exact :class:`Money`.

    Bare numbers are assumed to be in the threshold currency (USD-equivalent).
    """
    if isinstance(value, Money):
        return value
    if isinstance(value, bool):
        raise CurrencyParseError(str(value))
    if isinstance(value, str):
        return normalize_currency(value)
    if isinstance(value, (int, float)):
        return Money(amount=Decimal(str(value)), currency=THRESHOLD_CURRENCY)
    if isinstance(value, dict) and set(value) == {"amount", "currency"}:
        return Money(amount=Decimal(str(value["amount"])), currency=str(value["currency"]))
    raise CurrencyParseError(str(value))


def normalize_status(raw: object) -> str:
    """Map a numeric status code to its canonical label. Raises on an unknown numeric code.

    String labels already in the canonical vocabulary are returned unchanged (idempotent).
    """
    # TODO: If raw is already a canonical label string (in _STATUS_LABELS.values()),
    # return it unchanged. Reject bool. Convert numeric-string codes to int, look the code up in
    # _STATUS_LABELS, and return the label. Raise StatusCodeError(raw) on any unknown code.
    # bool is an int subclass, so it must be rejected before the int branch.
    if isinstance(raw, bool):
        raise StatusCodeError(raw)

    code: int
    if isinstance(raw, str):
        if raw in _STATUS_LABELS.values():
            return raw
        if not raw.isdigit():
            raise StatusCodeError(raw)
        code = int(raw)
    elif isinstance(raw, int):
        code = raw
    else:
        raise StatusCodeError(raw)

    try:
        return _STATUS_LABELS[code]
    except KeyError as exc:
        raise StatusCodeError(raw) from exc
