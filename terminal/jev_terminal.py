"""Jev Terminal core logic — standalone, standard library only.

This is the deployable version of the Jev Terminal that lived in Muse.
It does NOT talk to Fyers and it does NOT place trades. It:

* fetches NIFTY / India VIX / Bank Nifty / Sensex from the Yahoo
  Finance chart API, server-side (Yahoo sends no CORS headers, so a
  browser page cannot call it directly — that is why this terminal
  needs a small server, not just a static page),
* recomputes every level and metric the Jev Engine watches
  (PDH / PDL / PDC, weekly POC / VAH / VAL, ATM Greeks, premium
  scenarios, the engine's signal conditions as far as public data
  can judge them, and the gamma-blast watch),
* builds the post-market execution plan for the next session.

Honest limit, stated on the page itself: this is NOT tick-level
real time. It runs on Yahoo snapshots, refreshed at most every
5 minutes during market hours. The engine's real-time source is its
Fyers WebSocket, whose token is minted fresh each morning inside
GitHub Actions and expires daily — a web page cannot hold it.
Walls / PCR / max pain / Week POC come from the engine's own
Telegram messages, logged into the terminal (see server.py
``POST /api/chain``), not from a live chain feed. The gamma + CVD
signal condition is engine-only and is marked as such.

Pure functions here take plain data and are unit-tested in
``tests/test_terminal.py``; only ``fetch_*`` touches the network.
"""

from __future__ import annotations

import json
import math
import urllib.request
from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# --- Engine constants, kept in sync with the repo root config.py ---
LOT_SIZE = 75
BAR_SECONDS = 300
EARLY_TRIGGER_SECONDS = 260
POC_CHOP_PCT = 0.1          # no breakout trade within +/-0.1% of Week POC
SQUARE_OFF_TIME = "15:20"   # IST
MARKET_CLOSE_TIME = "15:31" # IST
MARKET_OPEN_TIME = "09:15"  # IST
SL_FRACTION = 0.50
T1_FRACTION = 1.50
T2_FRACTION = 2.00
MAX_DAILY_LOSS_RS = 10000.0
MAX_TRADES_PER_DAY = 3
RISK_FREE_RATE = 0.07

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "Mozilla/5.0 (Jev-Terminal/1.0)"

SYMBOLS = {
    "nifty": "%5ENSEI",
    "vix": "%5EINDIAVIX",
    "banknifty": "%5ENSEBANK",
    "sensex": "%5EBSESN",
}


# ---------------------------------------------------------------------------
# Black-Scholes (European, no dividends) — Greeks + premium scenarios
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes_greeks(spot: float, strike: float, t_years: float,
                         iv: float, r: float = RISK_FREE_RATE) -> dict:
    """ATM (or any strike) option price + Greeks. Theta/vega per day / per 1% IV."""
    # Floor at 1 hour, not 1 day: on expiry morning T is ~0.8 days and
    # a 1-day floor would overprice the ATM option by ~₹8 (verified
    # 16 Nov 2026: ATM 25300 CE ₹72.59 at T=0.8d, not ~₹81 at T=1d).
    t_years = max(t_years, 1.0 / (365.0 * 24.0))
    iv = max(iv, 0.01)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    disc = math.exp(-r * t_years)
    ce = spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    pe = strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    gamma = _norm_pdf(d1) / (spot * iv * sqrt_t)
    # Theta per calendar day (negative for a long option).
    theta_ce = (-(spot * _norm_pdf(d1) * iv) / (2 * sqrt_t)
                - r * strike * disc * _norm_cdf(d2)) / 365.0
    theta_pe = (-(spot * _norm_pdf(d1) * iv) / (2 * sqrt_t)
                + r * strike * disc * _norm_cdf(-d2)) / 365.0
    vega = spot * _norm_pdf(d1) * sqrt_t / 100.0  # per 1 vol point
    return {
        "cePremium": round(ce, 2),
        "pePremium": round(pe, 2),
        "ceDelta": round(_norm_cdf(d1), 2),
        "peDelta": round(_norm_cdf(d1) - 1.0, 2),
        "gamma": round(gamma, 4),
        "ceTheta": round(theta_ce, 2),
        "peTheta": round(theta_pe, 2),
        "vega": round(vega, 2),
    }


def atm_strike(spot: float) -> int:
    return int(round(spot / 50.0) * 50)


def scenario_table(spot: float, strike: float, t_years: float, iv: float,
                   moves=(-100, -50, 0, 50, 100)) -> list:
    rows = []
    for move in moves:
        g = black_scholes_greeks(spot + move, strike, t_years, iv)
        rows.append({"move": move, "ce": g["cePremium"], "pe": g["pePremium"]})
    return rows


# ---------------------------------------------------------------------------
# Dates / expiry — TUESDAY, the real NSE rule since 1 Sep 2025
# ---------------------------------------------------------------------------
# NIFTY 50 weekly options expire every Tuesday on NSE (SEBI/NSE schedule
# change effective 1 Sep 2025; previously Thursday). The repo's own
# expiry.py still computes the next Thursday — that disagrees with the
# exchange, with this terminal, and with the terminal's first version
# (which showed Tue 17 Nov 2026 as expiry). The engine's expiry rule
# is NOT changed by this terminal PR; it is flagged in terminal/README.md
# and in the PR description as a follow-up fix for expiry.py, because
# it decides which weekly series the engine buys.
# If the Tuesday is a trading holiday, expiry moves to the previous
# trading day — holidays are not known here, use FYERS_EXPIRY_OVERRIDE.

def next_weekly_expiry(now: datetime | None = None) -> date:
    """Next NIFTY weekly expiry (Tuesday; today if Tuesday before 15:30 IST)."""
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    today = now_ist.date()
    days_ahead = (1 - today.weekday()) % 7  # Tuesday == 1 (Monday == 0)
    expiry = today + timedelta(days=days_ahead)
    if days_ahead == 0 and now_ist.time() >= time(15, 30):
        expiry += timedelta(days=7)
    return expiry


def next_trading_day(day: date) -> date:
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:  # skip Sat/Sun (holidays not known here)
        nxt += timedelta(days=1)
    return nxt


def time_to_expiry_years(expiry: date, now: datetime | None = None) -> float:
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    close = datetime.combine(expiry, time(15, 30), tzinfo=IST)
    seconds = (close - now_ist).total_seconds()
    return max(seconds, 3600.0) / (365.0 * 24 * 3600)


def market_status(now: datetime | None = None) -> str:
    """pre-market | live | post-market | closed (weekend)."""
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    if now_ist.weekday() >= 5:
        return "closed"
    t = now_ist.time()
    if t < time(9, 15):
        return "pre-market"
    if t <= time(15, 30):
        return "live"
    return "post-market"


# ---------------------------------------------------------------------------
# Yahoo chart parsing (pure — no network)
# ---------------------------------------------------------------------------

def parse_yahoo_chart(payload: dict) -> dict:
    """Return {bars, price, previous_close} from a Yahoo chart response.

    bars: [{t, o, h, l, c}] in epoch seconds, None closes dropped.
    Raises ValueError on an empty / error payload.
    """
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        raise ValueError("Yahoo chart response has no result")
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators") or {}).get("quote") or [{}]
    q = quote[0] or {}
    closes = q.get("close") or []
    opens = q.get("open") or []
    highs = q.get("high") or []
    lows = q.get("low") or []
    bars = []
    for i, ts in enumerate(timestamps):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        bars.append({
            "t": int(ts),
            "o": float(opens[i]) if i < len(opens) and opens[i] is not None else float(c),
            "h": float(highs[i]) if i < len(highs) and highs[i] is not None else float(c),
            "l": float(lows[i]) if i < len(lows) and lows[i] is not None else float(c),
            "c": float(c),
        })
    if not bars:
        raise ValueError("Yahoo chart response has no bars")
    return {
        "bars": bars,
        "price": float(meta.get("regularMarketPrice") or bars[-1]["c"]),
        "previous_close": meta.get("previousClose"),
    }


def _ist_date(epoch_seconds: int) -> date:
    return datetime.fromtimestamp(epoch_seconds, tz=IST).date()


def split_bars_by_day(bars: list) -> dict:
    days: dict[date, list] = {}
    for b in bars:
        days.setdefault(_ist_date(b["t"]), []).append(b)
    return days


def weekly_profile(bars: list, bin_size: float = 10.0) -> dict:
    """Time-at-price POC / VAH / VAL from recent 5-minute bars.

    POC = the 10-point bin the price spent the most bars in.
    VAH/VAL = the narrowest band around the POC holding ~70% of bars.
    This is an *estimate* — the engine's Week POC (from its own
    volume profile) overrides it whenever it has been logged.
    """
    if not bars:
        return {"weekPoc": None, "vah": None, "val": None}
    closes = [b["c"] for b in bars]
    lo = min(closes)
    bins: dict[float, int] = {}
    for c in closes:
        key = math.floor(c / bin_size) * bin_size
        bins[key] = bins.get(key, 0) + 1
    poc_bin = max(bins, key=lambda k: bins[k])
    poc = poc_bin + bin_size / 2.0
    ordered = sorted(bins)
    # Expand outwards from the POC bin until ~70% of bars are covered.
    total = len(closes)
    target = total * 0.70
    idx = ordered.index(poc_bin)
    lo_i = hi_i = idx
    covered = bins[poc_bin]
    while covered < target and (lo_i > 0 or hi_i < len(ordered) - 1):
        take_lo = bins[ordered[lo_i - 1]] if lo_i > 0 else -1
        take_hi = bins[ordered[hi_i + 1]] if hi_i < len(ordered) - 1 else -1
        if take_hi >= take_lo:
            hi_i += 1
            covered += bins[ordered[hi_i]]
        else:
            lo_i -= 1
            covered += bins[ordered[lo_i]]
    return {
        "weekPoc": round(poc, 2),
        "vah": round(ordered[hi_i] + bin_size, 2),
        "val": round(ordered[lo_i], 2),
    }


# ---------------------------------------------------------------------------
# Levels, signal conditions, gamma-blast watch (pure)
# ---------------------------------------------------------------------------

def compute_levels(bars_today: list, bars_prev_day: list, bars_week: list,
                   chain: dict | None = None) -> dict:
    """PDH/PDL/PDC from the previous day; POC/VAH/VAL from the week.

    ``chain`` (engine-logged) overrides the POC when present.
    """
    if bars_prev_day:
        pdh = max(b["h"] for b in bars_prev_day)
        pdl = min(b["l"] for b in bars_prev_day)
        pdc = bars_prev_day[-1]["c"]
    else:
        pdh = pdl = pdc = None
    profile = weekly_profile(bars_week)
    week_poc = profile["weekPoc"]
    poc_source = "estimate"
    if chain and chain.get("weekPoc"):
        week_poc = float(chain["weekPoc"])
        poc_source = "engine"
    return {
        "pdh": pdh, "pdl": pdl, "pdc": pdc,
        "weekPoc": week_poc, "vah": profile["vah"], "val": profile["val"],
        "pocSource": poc_source,
    }


def signal_conditions(spot: float, bars: list, levels: dict,
                      now: datetime | None = None) -> tuple[list, str, str]:
    """Judge the engine's signal conditions from public data.

    Returns (conditions, trend_dir, breakout). The dealer-gamma + CVD
    condition cannot be judged here — it is marked engine-only.
    """
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    conditions = []
    pdc = levels.get("pdc")
    if pdc:
        bias = "bullish" if spot >= pdc else "bearish"
        conditions.append({
            "key": "bias", "label": "Bias vs previous close",
            "state": "met",
            "detail": f"Spot {spot:.2f} is {'above' if bias == 'bullish' else 'below'} "
                      f"PDC {pdc:.2f} — {bias} bias",
        })
    else:
        bias = "unknown"
        conditions.append({"key": "bias", "label": "Bias vs previous close",
                           "state": "unmet", "detail": "No previous close yet"})
    closes = [b["c"] for b in bars]
    if len(closes) >= 12:
        trend_pct = (closes[-1] / closes[-12] - 1.0) * 100
        trend_dir = "up" if trend_pct >= 0 else "down"
        agrees = (bias == "bullish" and trend_dir == "up") or \
                 (bias == "bearish" and trend_dir == "down")
        conditions.append({
            "key": "trend", "label": "12-bar trend agrees with bias",
            "state": "met" if agrees else "unmet",
            "detail": f"Last 12 bars (60 min): {trend_pct:+.2f}% — trend {trend_dir}",
        })
    else:
        trend_pct, trend_dir = 0.0, "unknown"
        conditions.append({"key": "trend", "label": "12-bar trend agrees with bias",
                           "state": "unmet", "detail": "Fewer than 12 bars so far"})
    breakout = "none"
    if len(closes) >= 6:
        ceiling = max(b["h"] for b in bars[-7:-1]) if len(bars) >= 7 else max(b["h"] for b in bars[:-1])
        base = min(b["l"] for b in bars[-7:-1]) if len(bars) >= 7 else min(b["l"] for b in bars[:-1])
        last = bars[-1]
        if last["c"] > ceiling:
            breakout = "up"
        elif last["c"] < base:
            breakout = "down"
        conditions.append({
            "key": "breakout", "label": "Breakout of last 6 bars",
            "state": "met" if breakout != "none" else "unmet",
            "detail": f"Breakout {breakout}" if breakout != "none"
                      else "No 6-bar breakout on the latest bar",
        })
    else:
        conditions.append({"key": "breakout", "label": "Breakout of last 6 bars",
                           "state": "unmet", "detail": "Fewer than 6 bars so far"})
    week_poc = levels.get("weekPoc")
    if week_poc:
        dist_pct = abs(spot - week_poc) / week_poc * 100
        clear = dist_pct > POC_CHOP_PCT
        conditions.append({
            "key": "poc", "label": f"Clear of weekly POC chop zone (±{POC_CHOP_PCT}%)",
            "state": "met" if clear else "unmet",
            "detail": f"Spot is {dist_pct:.3f}% from the "
                      f"{'engine' if levels.get('pocSource') == 'engine' else 'estimated'} "
                      f"Week POC {week_poc:.2f} — "
                      f"{'clear of the zone' if clear else 'INSIDE the no-trade zone'}",
        })
    # Bar timing: seconds into the current 5-minute bar.
    bar_start = now_ist.replace(second=0, microsecond=0)
    bar_start -= timedelta(minutes=bar_start.minute % 5,
                           seconds=bar_start.second)
    seconds_in = int((now_ist - bar_start).total_seconds())
    conditions.append({
        "key": "timing", "label": f"Bar timing ({EARLY_TRIGGER_SECONDS}s into the 5-min bar)",
        "state": "met" if seconds_in >= EARLY_TRIGGER_SECONDS else "unmet",
        "detail": f"{seconds_in}s into the current 5-minute bar — "
                  f"{'engine may trigger' if seconds_in >= EARLY_TRIGGER_SECONDS else 'engine waits for 260s'}",
    })
    conditions.append({
        "key": "gamma-cvd", "label": "Dealer gamma + CVD confirmation",
        "state": "engine",
        "detail": "Judged by the engine from the live Fyers chain and tick feed — "
                  "not visible to this terminal's public data",
    })
    return conditions, trend_dir, breakout


def gamma_blast_watch(spot: float, chain: dict | None, expiry: date,
                      now: datetime | None = None) -> dict:
    """What the terminal CAN say about a gamma blast (14:00–15:08 IST, expiry day).

    The detector itself (premium ₹2–9, spot within 0.35% of the wall
    strike, dealer net GEX < 0, 15-min OI unwinding + CVD slope up)
    is NOT wired into the live engine — it is a library module only.
    So this is a watch, not a signal, and any blast play is manual.
    """
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    in_window = time(14, 0) <= now_ist.time() <= time(15, 8)
    is_expiry = (now_ist.date() == expiry)
    call_wall = chain.get("callWall") if chain else None
    nearest_wall = None
    spot_near = None
    if call_wall:
        nearest_wall = call_wall
        spot_near = abs(spot - call_wall) / call_wall * 100
    return {
        "inWindow": bool(in_window and is_expiry),
        "isExpiryDay": bool(is_expiry),
        "window": "14:00–15:08 IST",
        "nearestWall": nearest_wall,
        "spotNearStrikePct": round(spot_near, 2) if spot_near is not None else None,
        "note": "Blast detector is NOT wired into the live engine — manual watch only, "
                "sized for a ~30% hit rate.",
    }


# ---------------------------------------------------------------------------
# Post-market plan (pure)
# ---------------------------------------------------------------------------

RISK_RULES = [
    "Paper mode — the engine places no live orders",
    f"Max {MAX_TRADES_PER_DAY} trades a day · max 1 open position · max daily loss ₹{MAX_DAILY_LOSS_RS:,.0f}",
    f"Exits on premium: SL {SL_FRACTION:.0%} · T1 {T1_FRACTION:.0%} · T2 {T2_FRACTION:.0%} · stop to breakeven after T1",
    f"Square-off {SQUARE_OFF_TIME} IST · session ends {MARKET_CLOSE_TIME} IST",
    f"No breakout trade within ±{POC_CHOP_PCT}% of the Week POC",
    "Gamma-blast detector is NOT wired into the live engine — it is a library module only",
]


def build_postmarket_plan(snapshot: dict, chain: dict | None = None) -> dict:
    """Plan for the next trading session, from today's completed session."""
    spot = snapshot["spot"]
    pdc = snapshot.get("pdc")
    based_on = snapshot.get("tradingDate")
    plan_date = next_trading_day(date.fromisoformat(based_on)).isoformat() \
        if based_on else None
    expiry = snapshot.get("expiryDate")
    is_expiry = (plan_date == expiry)
    day_high, day_low = snapshot.get("dayHigh"), snapshot.get("dayLow")
    day_range = (day_high - day_low) if day_high and day_low else None
    change = snapshot.get("change")
    change_pct = snapshot.get("changePct")
    bias = snapshot.get("bias", "unknown")
    week_poc = snapshot.get("weekPoc")
    checklist = []
    if change is not None:
        checklist.append({
            "label": f"Bias {bias}: closed {spot:.2f}, {change:+.2f} ({change_pct:+.2f}%) vs PDC — "
                     f"first trades tomorrow follow the bias unless PDH/PDL breaks the other way",
            "tone": "good" if bias == "bullish" else "warn" if bias == "bearish" else "info",
        })
    if week_poc:
        dist = spot - week_poc
        inside = abs(dist) / week_poc * 100 <= POC_CHOP_PCT
        checklist.append({
            "label": f"Close is {'inside' if inside else 'above' if dist > 0 else 'below'} "
                     f"the {'engine' if snapshot.get('pocSource') == 'engine' else 'estimated'} "
                     f"Week POC {week_poc:.2f} by {abs(dist):.2f} pts — "
                     f"{'inside the chop zone: no breakout trade there' if inside else 'outside the chop zone'}",
            "tone": "warn" if inside else "info",
        })
    if day_range and day_high and day_low:
        pos = (spot - day_low) / day_range * 100
        checklist.append({
            "label": f"Closed {pos:.0f}% up the {day_range:.1f}-pt range — tomorrow's range is "
                     f"PDH {day_high:.2f} to PDL {day_low:.2f} until one side breaks",
            "tone": "info",
        })
    vah, val = snapshot.get("vah"), snapshot.get("val")
    if vah and val:
        checklist.append({
            "label": f"Value area for tomorrow: VAH {vah:.2f} / VAL {val:.2f} "
                     f"(weekly time-at-price). Inside the value area, expect balance; "
                     f"outside it, expect travel toward the POC",
            "tone": "info",
        })
    if is_expiry:
        checklist.append({
            "label": "Tomorrow is weekly expiry day — theta is at its maximum and the "
                     "gamma-blast window is 14:00–15:08 IST; the blast detector is not wired "
                     "into the live engine, so any blast play is manual and sized for a ~30% hit rate",
            "tone": "warn",
        })
    vix = snapshot.get("vix", {}).get("price")
    if vix:
        checklist.append({
            "label": f"India VIX {vix:.2f} — a normal premium environment for the engine's "
                     f"{SL_FRACTION:.0%}/{T1_FRACTION:.0%}/{T2_FRACTION:.0%} exit ladder",
            "tone": "info",
        })
    checklist.append({
        "label": "At the 9:15 run, log the engine's Week POC (System Online message) and its "
                 "call wall / put wall from the live chain into the Engine levels panel — "
                 "tomorrow's engine levels come from that run, not from today's close",
        "tone": "info",
    })
    return {
        "planDate": plan_date,
        "basedOnDate": based_on,
        "expiryDate": expiry,
        "isExpiryDay": is_expiry,
        "close": spot,
        "open": snapshot.get("dayOpen"),
        "high": day_high,
        "low": day_low,
        "range": round(day_range, 1) if day_range else None,
        "pdc": spot,  # tomorrow's previous close IS today's close
        "pdh": day_high,
        "pdl": day_low,
        "weekPoc": week_poc,
        "pocSource": snapshot.get("pocSource", "estimate"),
        "vah": vah,
        "val": val,
        "bias": bias,
        "change": change,
        "changePct": change_pct,
        "checklist": checklist,
        "riskRules": RISK_RULES,
        "builtAt": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Network fetch + snapshot assembly
# ---------------------------------------------------------------------------

def fetch_yahoo(symbol: str, range_: str = "5d", interval: str = "5m",
                timeout: int = 15) -> dict:
    url = YAHOO_CHART_URL.format(symbol=symbol) + f"?range={range_}&interval={interval}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return parse_yahoo_chart(payload)


def fetch_quote(symbol: str, timeout: int = 15) -> dict:
    """Latest price + previous close for an index (1d / 1m chart)."""
    return fetch_yahoo(symbol, range_="1d", interval="1m", timeout=timeout)


def build_snapshot(now: datetime | None = None,
                   fetcher=fetch_yahoo) -> dict:
    """Fetch Yahoo data and assemble the full terminal snapshot."""
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    nifty = fetcher(SYMBOLS["nifty"], "5d", "5m")
    vix = fetch_quote(SYMBOLS["vix"])
    bank = fetch_quote(SYMBOLS["banknifty"])
    sensex = fetch_quote(SYMBOLS["sensex"])

    bars = nifty["bars"]
    by_day = split_bars_by_day(bars)
    trading_days = sorted(by_day)
    today = trading_days[-1] if trading_days else now_ist.date()
    bars_today = by_day.get(today, bars)
    prev_day = trading_days[-2] if len(trading_days) >= 2 else None
    bars_prev = by_day.get(prev_day, []) if prev_day else []
    # Previous close: Yahoo meta, else last bar of the previous day.
    pdc_meta = nifty.get("previous_close")
    spot = float(nifty["price"])
    pdc = float(pdc_meta) if pdc_meta else (bars_prev[-1]["c"] if bars_prev else spot)
    change = spot - pdc
    change_pct = (change / pdc * 100) if pdc else 0.0
    day_open = bars_today[0]["o"] if bars_today else spot
    day_high = max(b["h"] for b in bars_today) if bars_today else spot
    day_low = min(b["l"] for b in bars_today) if bars_today else spot

    expiry = next_weekly_expiry(now_ist)
    t_years = time_to_expiry_years(expiry, now_ist)
    iv = (vix["price"] / 100.0) if vix.get("price") else 0.15
    strike = atm_strike(spot)
    greeks = black_scholes_greeks(spot, strike, t_years, iv)
    scenarios = scenario_table(spot, strike, t_years, iv)

    # Levels need the engine-logged chain for the POC; the server passes
    # it in via build_snapshot_with_chain. Here: estimate only.
    levels = compute_levels(bars_today, bars_prev, bars, chain=None)
    conditions, trend_dir, breakout = signal_conditions(spot, bars_today, levels, now_ist)
    trend12 = 0.0
    closes = [b["c"] for b in bars_today]
    if len(closes) >= 12:
        trend12 = (closes[-1] / closes[-12] - 1.0) * 100
    bar_start = now_ist.replace(second=0, microsecond=0) - timedelta(
        minutes=now_ist.minute % 5, seconds=now_ist.second)
    seconds_in = int((now_ist - bar_start).total_seconds())

    return {
        "spot": round(spot, 2),
        "change": round(change, 2),
        "changePct": round(change_pct, 2),
        "dayOpen": round(day_open, 2),
        "dayHigh": round(day_high, 2),
        "dayLow": round(day_low, 2),
        "tradingDate": today.isoformat(),
        "prevDate": prev_day.isoformat() if prev_day else None,
        "pdc": round(pdc, 2),
        "pdh": round(levels["pdh"], 2) if levels["pdh"] else None,
        "pdl": round(levels["pdl"], 2) if levels["pdl"] else None,
        "weekPoc": levels["weekPoc"],
        "vah": levels["vah"],
        "val": levels["val"],
        "pocSource": levels["pocSource"],
        "weekProfileDays": len(trading_days),
        "atmStrike": strike,
        "expiryDate": expiry.isoformat(),
        "isExpiryDay": now_ist.date() == expiry,
        "daysToExpiry": round((datetime.combine(expiry, time(15, 30), tzinfo=IST)
                               - now_ist).total_seconds() / 86400, 1),
        "ivPct": round(iv * 100, 2),
        "greeks": greeks,
        "scenarios": scenarios,
        "bias": "bullish" if spot >= pdc else "bearish",
        "trend12Pct": round(trend12, 2),
        "trendDir": trend_dir,
        "breakout": breakout,
        "conditions": conditions,
        "blast": gamma_blast_watch(spot, None, expiry, now_ist),
        "inPocChopZone": bool(levels["weekPoc"] and
                              abs(spot - levels["weekPoc"]) / levels["weekPoc"] * 100 <= POC_CHOP_PCT),
        "distToPoc": round(spot - levels["weekPoc"], 2) if levels["weekPoc"] else None,
        "distToPocPct": round(abs(spot - levels["weekPoc"]) / levels["weekPoc"] * 100, 3)
                        if levels["weekPoc"] else None,
        "distToPdh": round(levels["pdh"] - spot, 2) if levels["pdh"] else None,
        "distToPdl": round(spot - levels["pdl"], 2) if levels["pdl"] else None,
        "barSecondsIn": seconds_in,
        "marketStatus": market_status(now_ist),
        "istTime": now_ist.strftime("%H:%M:%S IST"),
        "bars": bars_today,
        "vix": {"price": round(vix["price"], 2),
                "changePct": round((vix["price"] / vix["previous_close"] - 1) * 100, 2)
                if vix.get("previous_close") else None},
        "bankNifty": {"price": round(bank["price"], 2),
                      "changePct": round((bank["price"] / bank["previous_close"] - 1) * 100, 2)
                      if bank.get("previous_close") else None},
        "sensex": {"price": round(sensex["price"], 2),
                   "changePct": round((sensex["price"] / sensex["previous_close"] - 1) * 100, 2)
                   if sensex.get("previous_close") else None},
        "feed": "snapshot",
        "asOf": datetime.now(timezone.utc).isoformat(),
    }


def apply_chain_to_snapshot(snapshot: dict, chain: dict | None) -> dict:
    """Overlay engine-logged chain values (walls, POC, PCR, max pain)."""
    if not chain:
        return snapshot
    snap = dict(snapshot)
    if chain.get("weekPoc"):
        snap["weekPoc"] = float(chain["weekPoc"])
        snap["pocSource"] = "engine"
        snap["distToPoc"] = round(snap["spot"] - snap["weekPoc"], 2)
        snap["distToPocPct"] = round(abs(snap["spot"] - snap["weekPoc"])
                                     / snap["weekPoc"] * 100, 3)
        snap["inPocChopZone"] = snap["distToPocPct"] <= POC_CHOP_PCT
    # Re-judge the POC condition with the engine value.
    for cond in snap.get("conditions", []):
        if cond["key"] == "poc" and chain.get("weekPoc"):
            dist_pct = snap["distToPocPct"]
            cond["state"] = "met" if dist_pct > POC_CHOP_PCT else "unmet"
            cond["detail"] = (f"Spot is {dist_pct:.3f}% from the engine's "
                              f"Week POC {snap['weekPoc']:.2f} — "
                              f"{'clear of the zone' if dist_pct > POC_CHOP_PCT else 'INSIDE the no-trade zone'}")
    snap["blast"] = gamma_blast_watch(snap["spot"], chain,
                                      date.fromisoformat(snap["expiryDate"]))
    snap["chain"] = chain
    return snap


def apply_engine_snapshot(base: dict, engine: dict,
                          now: datetime | None = None) -> dict:
    """Rebuild the snapshot from the engine's live Fyers feed.

    ``base`` is the last Yahoo snapshot, kept only for India VIX / Bank
    Nifty / Sensex, which the engine does not push. ``engine`` is the
    record the engine POSTs to /api/engine once per 5-minute bar: spot,
    levels, chain walls and the session's bars. Everything the page
    judges from price — spot, change, day range, ATM Greeks, scenarios,
    signal conditions, gamma-blast watch — is recomputed from the
    engine's numbers, so the page never mixes a Fyers spot with Yahoo
    Greeks. The weekly value area (VAH/VAL) stays from the base
    snapshot's 5-day profile; the engine pushes no weekly profile.
    Any stale vendor label on the base snapshot is dropped.
    """
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    snap = dict(base)
    snap.pop("source", None)
    spot = float(engine["spot"])
    bars = engine.get("bars") or base.get("bars") or []
    bars = [b for b in bars
            if isinstance(b, dict) and b.get("c") is not None]
    pdc = engine.get("pdc") or base.get("pdc") or spot
    pdc = float(pdc)
    change = spot - pdc
    change_pct = (change / pdc * 100) if pdc else 0.0
    day_open = bars[0]["o"] if bars else spot
    day_high = max(b["h"] for b in bars) if bars else spot
    day_low = min(b["l"] for b in bars) if bars else spot

    chain = {k: engine.get(k) for k in
             ("weekPoc", "callWall", "putWall", "pcr", "maxPain")}
    chain = {k: v for k, v in chain.items() if v is not None}
    # Levels: the engine's values win; the weekly profile still comes
    # from the base snapshot's 5-day bars (see docstring).
    levels = compute_levels(bars, [], bars, chain=chain or None)
    if engine.get("pdh") is not None:
        levels["pdh"] = float(engine["pdh"])
    if engine.get("pdl") is not None:
        levels["pdl"] = float(engine["pdl"])
    levels["pdc"] = pdc
    if engine.get("weekPoc") is not None:
        levels["weekPoc"] = float(engine["weekPoc"])
        levels["pocSource"] = "engine"
    levels["vah"] = base.get("vah")
    levels["val"] = base.get("val")

    expiry = (date.fromisoformat(engine["expiryDate"])
              if engine.get("expiryDate") else next_weekly_expiry(now_ist))
    t_years = time_to_expiry_years(expiry, now_ist)
    iv = 0.15
    vix = base.get("vix") or {}
    if vix.get("price"):
        iv = float(vix["price"]) / 100.0
    strike = atm_strike(spot)
    greeks = black_scholes_greeks(spot, strike, t_years, iv)
    scenarios = scenario_table(spot, strike, t_years, iv)
    conditions, trend_dir, breakout = signal_conditions(spot, bars, levels, now_ist)
    closes = [b["c"] for b in bars]
    trend12 = (closes[-1] / closes[-12] - 1.0) * 100 if len(closes) >= 12 else 0.0
    bar_start = now_ist.replace(second=0, microsecond=0) - timedelta(
        minutes=now_ist.minute % 5, seconds=now_ist.second)
    seconds_in = int((now_ist - bar_start).total_seconds())

    snap.update({
        "spot": round(spot, 2),
        "change": round(change, 2),
        "changePct": round(change_pct, 2),
        "dayOpen": round(day_open, 2),
        "dayHigh": round(day_high, 2),
        "dayLow": round(day_low, 2),
        "pdc": round(pdc, 2),
        "pdh": round(levels["pdh"], 2) if levels["pdh"] else None,
        "pdl": round(levels["pdl"], 2) if levels["pdl"] else None,
        "weekPoc": levels["weekPoc"],
        "vah": levels["vah"],
        "val": levels["val"],
        "pocSource": levels["pocSource"],
        "atmStrike": strike,
        "expiryDate": expiry.isoformat(),
        "isExpiryDay": now_ist.date() == expiry,
        "daysToExpiry": round((datetime.combine(expiry, time(15, 30), tzinfo=IST)
                               - now_ist).total_seconds() / 86400, 1),
        "greeks": greeks,
        "scenarios": scenarios,
        "bias": "bullish" if spot >= pdc else "bearish",
        "trend12Pct": round(trend12, 2),
        "trendDir": trend_dir,
        "breakout": breakout,
        "conditions": conditions,
        "inPocChopZone": bool(levels["weekPoc"] and
                              abs(spot - levels["weekPoc"]) / levels["weekPoc"]
                              * 100 <= POC_CHOP_PCT),
        "distToPoc": round(spot - levels["weekPoc"], 2) if levels["weekPoc"] else None,
        "distToPocPct": round(abs(spot - levels["weekPoc"]) / levels["weekPoc"]
                              * 100, 3) if levels["weekPoc"] else None,
        "distToPdh": round(levels["pdh"] - spot, 2) if levels["pdh"] else None,
        "distToPdl": round(spot - levels["pdl"], 2) if levels["pdl"] else None,
        "barSecondsIn": seconds_in,
        "marketStatus": market_status(now_ist),
        "istTime": now_ist.strftime("%H:%M:%S IST"),
        "bars": bars,
        "feed": "fyers",
        "engineAsOf": engine.get("asOf"),
        "asOf": engine.get("asOf") or snap.get("asOf"),
        "tradingDate": engine.get("tradingDate") or snap.get("tradingDate"),
    })
    if chain:
        snap = apply_chain_to_snapshot(snap, chain)
    return snap
