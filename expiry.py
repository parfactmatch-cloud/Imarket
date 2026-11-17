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

Symbol format (verified against Fyers' symbology docs, updated
14 Oct 2026 -- the repo's old DDMMM code, e.g. ``NSE:NIFTY17NOV23250CE``,
matched no Fyers format and would have been rejected as an invalid
symbol on the first quote or order, which is why no signal has ever
traded):

- Weekly expiry: ``NSE:NIFTY{YY}{M}{dd}{Strike}{CE|PE}``, where ``M``
  is a one-character month code -- "1".."9" for Jan-Sep, "O" for Oct,
  "N" for Nov, "D" for Dec. E.g. Tue 17 Nov 2026, 25300 CE ->
  ``NSE:NIFTY26N1725300CE``.
- Monthly expiry (the last Tuesday of the month, when the expiring
  contract is the monthly one): ``NSE:NIFTY{YY}{MMM}{Strike}{CE|PE}``,
  e.g. Tue 24 Nov 2026, 23250 CE -> ``NSE:NIFTY26NOV23250CE``.

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


# Fyers weekly month codes: Jan-Sep are "1".."9", Oct/Nov/Dec are "O"/"N"/"D".
_WEEKLY_MONTH_CODES = "123456789OND"


def is_monthly_expiry(expiry: date) -> bool:
    """True when ``expiry`` is the last Tuesday of its month.

    NSE NIFTY monthly options expire on the last Tuesday of the month;
    on that day the expiring contract is the monthly one, not a weekly.
    """
    if expiry.weekday() != 1:  # Tuesday == 1 (Monday == 0).
        return False
    # The last Tuesday is the Tuesday in the final 7 days of the month.
    return (expiry + timedelta(days=7)).month != expiry.month


def expiry_symbol_suffix(expiry: date) -> str:
    """Expiry code for a Fyers option symbol.

    Weekly: YYMdd -- two-digit year, one-character month code, two-digit
    day. E.g. Tue 17 Nov 2026 -> ``26N17``.
    Monthly (last Tuesday of the month): YYMMM, e.g. Tue 24 Nov 2026 ->
    ``26NOV``.
    """
    yy = expiry.strftime("%y")
    if is_monthly_expiry(expiry):
        return f"{yy}{expiry.strftime('%b').upper()}"
    month_code = _WEEKLY_MONTH_CODES[expiry.month - 1]
    return f"{yy}{month_code}{expiry.day:02d}"


def option_symbol(underlying: str, expiry: date, strike: int, option_type: str) -> str:
    """Build a Fyers option symbol, e.g. NSE:NIFTY26N1725300CE (weekly)
    or NSE:NIFTY26NOV23250CE (monthly, last Tuesday of the month)."""
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
