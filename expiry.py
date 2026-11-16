"""
expiry.py
Weekly-expiry logic for NIFTY index options.

NIFTY weekly options expire on Tuesday. (They used to expire on
Thursday; NSE moved all its equity-derivative expiries to Tuesday
effective 1 Sep 2025, SEBI circular FAOP68747. The old code here
computed the next Thursday, which has been wrong since that change:
there is no NIFTY weekly series expiring on a Thursday any more, so
symbols built from a Thursday date cannot trade.)

The old code before that hardcoded the expiry into every symbol
(``NSE:NIFTY26NOV...``), which silently broke after that date passed.

Symbol format (preserved from the original code): the expiry is encoded
as DDMMM, e.g. Tuesday 17 Nov 2026 -> ``17NOV``, giving
``NSE:NIFTY17NOV23250CE``. Verify this format against the Fyers symbol
master before any live use -- it is the repo's existing convention,
not a newly invented one. Only the weekday changed here, not the format.

Limitation: trading holidays are not known from calendar logic alone.
If the Tuesday expiry is a holiday, expiry moves to the previous
trading day -- set FYERS_EXPIRY_OVERRIDE to the actual expiry date
(YYYY-MM-DD); otherwise the computed Tuesday is used and the
limitation is documented in the README.
"""

from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
# A Tuesday expiry is settled by the 15:30 IST close.
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
    """Return the next NIFTY weekly expiry date (a Tuesday).

    - ``override`` (YYYY-MM-DD, from FYERS_EXPIRY_OVERRIDE) wins when set.
    - If today is Tuesday and it is before 15:30 IST, expiry is today.
    - Otherwise the next Tuesday.
    """
    if override:
        return date.fromisoformat(override.strip())

    now_ist = _as_ist_date(now)
    today = now_ist.date()
    # Tuesday == 1 (Monday == 0).
    days_ahead = (1 - today.weekday()) % 7
    expiry = today + timedelta(days=days_ahead)
    if days_ahead == 0 and now_ist.time() >= _EXPIRY_CUTOFF:
        expiry = expiry + timedelta(days=7)
    return expiry


def expiry_symbol_suffix(expiry: date) -> str:
    """DDMMM suffix used in Fyers option symbols, e.g. 17NOV."""
    return f"{expiry.day:02d}{expiry.strftime('%b').upper()}"


def option_symbol(underlying: str, expiry: date, strike: int, option_type: str) -> str:
    """Build a Fyers option symbol, e.g. NSE:NIFTY17NOV23250CE."""
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
