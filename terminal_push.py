"""Push the engine's live Fyers snapshot to the Jev Terminal.

The terminal used to show Yahoo Finance snapshots only. The engine is the
one with the real feed — Fyers WebSocket ticks plus the live option chain —
so once per 5-minute bar (and once at session start) it POSTs its snapshot
to the terminal's ``/api/engine`` endpoint:

    {spot, asOf, tradingDate, pdh, pdl, pdc, weekPoc,
     callWall, putWall, gammaFlip, regime, maxPain, pcr, expiryDate,
     bars: [{t, o, h, l, c}, ...]}

The terminal shows that data with a "Fyers engine feed" label while it is
fresh (under 10 minutes old) and falls back to its Yahoo snapshots when
the engine is not running. Nothing here is guessed: a missing GEX
snapshot simply leaves the walls out of the payload.

Setup (all three are needed before the feed goes live):
  * GitHub repo secret ``JEV_TERMINAL_URL`` — the terminal's public URL,
    e.g. https://imarket-3.onrender.com
  * GitHub repo secret ``JEV_TERMINAL_TOKEN`` — any long random string.
  * The same token as the ``JEV_TERMINAL_TOKEN`` env var on the Render
    service (Render redeploys when it changes).
  * The workflow's ``env:`` block must pass both through (see README).

A failed push never interrupts the run: ``push_engine_snapshot`` catches
everything and returns ``(False, reason)``; the caller logs a warning
and the terminal keeps its last snapshot.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
ENGINE_PUSH_TIMEOUT_SECONDS = 10
MAX_PUSH_BARS = 100


def bars_to_chart_bars(bars, trading_date) -> list:
    """Convert FastBar objects (timestamp 'HH:MM') to chart bars.

    Each bar becomes {"t": epoch seconds, "o", "h", "l", "c"} — the same
    shape the terminal's chart already uses. Bars with an unparseable
    timestamp are skipped, never fatal.
    """
    out = []
    for bar in bars or []:
        try:
            hour, minute = str(bar.timestamp).split(":")
            moment = datetime.combine(
                trading_date, dtime(int(hour), int(minute)), tzinfo=IST)
            out.append({
                "t": int(moment.timestamp()),
                "o": float(bar.open),
                "h": float(bar.high),
                "l": float(bar.low),
                "c": float(bar.close),
            })
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def build_engine_payload(*, spot, pdh, pdl, pdc, week_poc,
                         gex_snapshot, expiry, bars,
                         trading_date) -> dict:
    """Assemble the /api/engine payload. ``None`` values are omitted."""
    payload = {
        "spot": round(float(spot), 2),
        "asOf": datetime.now(timezone.utc).isoformat(),
        "tradingDate": trading_date.isoformat(),
        "pdh": round(float(pdh), 2) if pdh is not None else None,
        "pdl": round(float(pdl), 2) if pdl is not None else None,
        "pdc": round(float(pdc), 2) if pdc is not None else None,
        "weekPoc": round(float(week_poc), 2) if week_poc is not None else None,
        "expiryDate": expiry.isoformat() if expiry is not None else None,
    }
    if gex_snapshot is not None:
        payload["callWall"] = float(gex_snapshot.call_wall)
        payload["putWall"] = float(gex_snapshot.put_wall)
        payload["gammaFlip"] = float(gex_snapshot.gamma_flip_level)
        payload["regime"] = str(gex_snapshot.market_regime)
        payload["maxPain"] = float(getattr(gex_snapshot, "max_pain", 0.0) or 0.0)
        payload["pcr"] = round(float(getattr(gex_snapshot, "pcr", 0.0) or 0.0), 2)
    chart_bars = bars_to_chart_bars(bars, trading_date)[-MAX_PUSH_BARS:]
    if chart_bars:
        payload["bars"] = chart_bars
    return {k: v for k, v in payload.items() if v is not None}


def push_engine_snapshot(url: str, token: Optional[str], payload: dict,
                         timeout: int = ENGINE_PUSH_TIMEOUT_SECONDS):
    """POST ``payload`` to ``<url>/api/engine``.

    Returns (True, None) on a 2xx response, else (False, reason).
    Never raises — a push failure must never break the live engine.
    """
    if not url:
        return False, "no terminal URL configured"
    try:
        request = urllib.request.Request(
            url.rstrip("/") + "/api/engine",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Jev-Token": token or ""},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            resp.read()
        return True, None
    except Exception as exc:  # network, HTTP error, timeout — all non-fatal
        return False, str(exc)
