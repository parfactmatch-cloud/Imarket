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

Holidays: when the Tuesday expiry is an NSE trading holiday, expiry
moves to the previous trading day (NSE rule). That happened on
Mon 23 Nov 2026 -- the November monthly expiry -- because Tue 24 Nov
2026 was Guru Nanak Jayanti: the old code aimed at 24 Nov, a series
that does not exist, fetched an empty option chain (63 strikes, zero
open interest, zero GEX) and held all day on run #56. The holiday
list below is the NSE 2026 trading-holiday calendar (NSE circular
FAOP71777). Add the next year's list when NSE publishes it (usually
late in the previous year); until then ``FYERS_EXPIRY_OVERRIDE``
remains the escape hatch for a holiday the list does not know.
"""

from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
# A Tuesday expiry is settled by the 15:30 IST close.
_EXPIRY_CUTOFF = time(15, 30)

# NSE trading holidays, calendar year 2026 (NSE circular FAOP71777,
# equity / equity-derivatives segments). Only weekday holidays matter
# for a Tuesday expiry, but the full list is kept so the same set can
# answer "is this a trading day?" anywhere in the repo.
NSE_TRADING_HOLIDAYS_2026 = frozenset({
    date(2026, 1, 26),   # Mon, Republic Day
    date(2026, 3, 3),    # Tue, Holi
    date(2026, 3, 26),   # Thu, Shri Ram Navami
    date(2026, 3, 31),   # Tue, Shri Mahavir Jayanti
    date(2026, 4, 3),    # Fri, Good Friday
    date(2026, 4, 14),   # Tue, Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Fri, Maharashtra Day
    date(2026, 5, 28),   # Thu, Bakri Id
    date(2026, 6, 26),   # Fri, Muharram
    date(2026, 9, 14),   # Mon, Ganesh Chaturthi
    date(2026, 10, 2),   # Fri, Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Tue, Dussehra
    date(2026, 11, 10),  # Tue, Diwali-Balipratipada
    date(2026, 11, 24),  # Tue, Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25),  # Fri, Christmas
})

# Every year's list, so a new year is one line to add.
NSE_TRADING_HOLIDAYS = NSE_TRADING_HOLIDAYS_2026


def is_trading_holiday(day: date) -> bool:
    """True when ``day`` is an NSE trading holiday in the known list."""
    return day in NSE_TRADING_HOLIDAYS


def is_trading_day(day: date) -> bool:
    """True on a weekday that is not an NSE trading holiday."""
    return day.weekday() < 5 and not is_trading_holiday(day)


def _previous_trading_day(day: date) -> date:
    """Step back from ``day`` to the nearest trading day (never ``day``)."""
    candidate = day - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _as_ist_date(now) -> datetime:
    if now is None:
        return datetime.now(IST)
    if isinstance(now, datetime):
        return now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    if isinstance(now, date):
        return datetime.combine(now, time(9, 15), tzinfo=IST)
    raise TypeError("now must be a datetime, date or None")


def next_weekly_expiry(now=None, override: str = "") -> date:
    """Return the next NIFTY weekly expiry date.

    - ``override`` (YYYY-MM-DD, from FYERS_EXPIRY_OVERRIDE) wins when set.
    - If today is Tuesday and it is before 15:30 IST, expiry is today.
    - Otherwise the next Tuesday.
    - If that Tuesday is an NSE trading holiday, expiry moves to the
      previous trading day (usually the Monday). E.g. from Mon 23 Nov
      2026 the next Tuesday, 24 Nov, is Guru Nanak Jayanti, so expiry
      is Mon 23 Nov 2026 itself; from Wed 18 Nov 2026 it is also
      Mon 23 Nov 2026, not Tue 24 Nov.
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
    if not is_trading_day(expiry):
        expiry = _previous_trading_day(expiry)
    return expiry


# Fyers weekly month codes: Jan-Sep are "1".."9", Oct/Nov/Dec are "O"/"N"/"D".
_WEEKLY_MONTH_CODES = "123456789OND"


def _tuesday_of_week(day: date) -> date:
    """The Tuesday of ``day``'s Mon-Sun week (Monday == 0, Tuesday == 1).

    Monday -> the next day; Tuesday -> itself; Wednesday-Sunday -> the
    Tuesday just gone (never the following week's Tuesday).
    """
    return day + timedelta(days=1 - day.weekday())


def is_monthly_expiry(expiry: date) -> bool:
    """True when ``expiry`` is the monthly contract's expiry.

    NSE NIFTY monthly options expire on the last Tuesday of the month;
    on that day the expiring contract is the monthly one, not a weekly.
    When that Tuesday is a holiday the expiry shifts to the previous
    trading day (usually the Monday) but it is still the monthly
    contract, so the check is made on the Tuesday of the expiry's week:
    Mon 23 Nov 2026 is a monthly expiry (its Tuesday, 24 Nov, is the
    last Tuesday of November), while Mon 9 Nov 2026 is not (its
    Tuesday, 10 Nov, is not the last Tuesday).
    """
    tuesday = _tuesday_of_week(expiry)
    if tuesday.weekday() != 1:  # Tuesday == 1 (Monday == 0).
        return False
    # The last Tuesday is the Tuesday in the final 7 days of the month.
    return (tuesday + timedelta(days=7)).month != tuesday.month


def expiry_symbol_suffix(expiry: date) -> str:
    """Expiry code for a Fyers option symbol.

    Weekly: YYMdd -- two-digit year, one-character month code, two-digit
    day. E.g. Tue 17 Nov 2026 -> ``26N17``.
    Monthly (last Tuesday of the month): YYMMM, e.g. Tue 24 Nov 2026 ->
    ``26NOV`` -- and likewise Mon 23 Nov 2026, the same monthly
    contract shifted by the 24 Nov holiday.
    """
    yy = expiry.strftime("%y")
    if is_monthly_expiry(expiry):
        return f"{yy}{_tuesday_of_week(expiry).strftime('%b').upper()}"
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
