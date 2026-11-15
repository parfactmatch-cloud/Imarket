"""
expiry.py
Weekly-expiry logic for NIFTY index options.

NIFTY weekly options expire on Thursday. The old code hardcoded the
expiry into every symbol (``NSE:NIFTY26NOV...``), which silently broke
after that Thursday passed.

Symbol format (preserved from the original code): the expiry is encoded
as DDMMM, e.g. Thursday 26 Nov 2026 -> ``26NOV``, giving
``NSE:NIFTY26NOV23250CE``. Verify this format against the Fyers symbol
master before any live use -- it is the repo's existing convention,
not a newly invented one.

Limitation: trading holidays are not known from calendar logic alone.
If the Thursday expiry is a holiday, set FYERS_EXPIRY_OVERRIDE to the
actual expiry date (YYYY-MM-DD); otherwise the computed Thursday is
used and the limitation is documented in the README.
"""

from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
# A Thursday expiry is settled by the 15:30 IST close.
_EXPIRY_CUTOFF = time(15, 30)


def _as_ist_date(now) -> datetime:
    if now is None:
        return datetime.now(IST)
    if isinstance(now, datetime):
        return now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    if isinstance(now, date):
        return datetime.combine(now, time(9, 15), tzinfo=IST)
    raise TypeError("now must be a datetime, date or None")


def next_weekly_expiry(now=None, override: str = "") -> date:
    """Return the next NIFTY weekly expiry date (a Thursday).

    - ``override`` (YYYY-MM-DD, from FYERS_EXPIRY_OVERRIDE) wins when set.
    - If today is Thursday and it is before 15:30 IST, expiry is today.
    - Otherwise the next Thursday.
    """
    if override:
        return date.fromisoformat(override.strip())

    now_ist = _as_ist_date(now)
    today = now_ist.date()
    # Thursday == 3 (Monday == 0).
    days_ahead = (3 - today.weekday()) % 7
    expiry = today + timedelta(days=days_ahead)
    if days_ahead == 0 and now_ist.time() >= _EXPIRY_CUTOFF:
        expiry = expiry + timedelta(days=7)
    return expiry


def expiry_symbol_suffix(expiry: date) -> str:
    """DDMMM suffix used in Fyers option symbols, e.g. 26NOV."""
    return f"{expiry.day:02d}{expiry.strftime('%b').upper()}"


def option_symbol(underlying: str, expiry: date, strike: int, option_type: str) -> str:
    """Build a Fyers option symbol, e.g. NSE:NIFTY26NOV23250CE."""
    if option_type not in ("CE", "PE"):
        raise ValueError("option_type must be 'CE' or 'PE'")
    return f"NSE:{underlying}{expiry_symbol_suffix(expiry)}{int(strike)}{option_type}"


def time_to_expiry_years(expiry: date, now=None) -> float:
    """Years from ``now`` to the expiry-day 15:30 IST close (min 1 day)."""
    now_ist = _as_ist_date(now)
    expiry_close = datetime.combine(expiry, _EXPIRY_CUTOFF, tzinfo=IST)
    seconds = (expiry_close - now_ist).total_seconds()
    if seconds <= 0:
        return 1.0 / 365.0
    return seconds / (365.0 * 24 * 3600)
