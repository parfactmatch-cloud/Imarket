"""
market_data.py
Live market-data helpers: option quotes and the option chain (for GEX).

These are deliberately defensive. The Fyers client is an external API
whose response shapes are not pinned by this repo, so every parse
tolerates missing fields and returns ``None`` on failure instead of
raising or inventing a price. Callers must treat ``None`` as
"do not trade" -- never fall back to a spot price or a constant.

- ``get_option_quote``: the option's own LTP (premium), never the
  underlying spot. The old code priced options at the NIFTY spot
  (~24,000) instead of the premium (tens of rupees).
- ``compute_dealer_net_dex``: net dealer gamma from the live option
  chain via IndiaGEXEngine. Returns ``None`` when the chain cannot be
  fetched or parsed, in which case the lag engine must not signal
  (it treats a ``None`` dealer exposure as NEUTRAL). This replaces the
  old hardcoded ``dealer_net_dex=125000`` constant.
- ``compute_gex_snapshot``: the full GEX snapshot behind that number
  (call wall, put wall, gamma flip, regime). Same None contract.

Fyers option-chain shape (verified against Fyers' API docs, updated
14 Oct 2026, and run #56's live log, 23 Nov 2026):

- The response's ``optionsChain`` is a list of FLAT rows, one per
  option leg -- not one row per strike with nested ``call``/``put``
  dicts. Each row carries ``strike_price``, ``option_type`` (``CE`` /
  ``PE``), ``ltp``, ``oi``, ``oich``, ``volume`` and, when requested,
  a nested ``greeks`` object with ``delta``/``gamma``/``theta``/``vega``
  /``iv``. The old parser read only nested ``call``/``put`` dicts, so
  on the live feed it saw OI 0, IV 0 and LTP 0 on every row and net
  dealer GEX read exactly 0.00 on all 68 cycles of run #56 -- even
  though the chain itself (63 rows: the index row + 31 strikes x 2
  legs) was real. The parser below reads the flat shape, and still
  accepts the nested shape (used by the repo's own tests).
- The first row is the underlying index itself (``strike_price`` -1,
  empty ``option_type``, future price in ``fp``); it is skipped.
- ``greeks`` must be requested (``greeks=1``) or the rows carry no IV
  at all; without it the engine falls back to IV derived from LTP.
- ``timestamp`` pins the chain to one expiry (the expiry day's
  15:30 IST close as an epoch); without it Fyers returns its default
  "current expiry". A timestamped fetch that fails falls back to the
  untimestamped one, so a bad timestamp can never leave the engine
  with no chain at all.
"""

import json
import logging
import math
import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import List, Optional

from expiry import time_to_expiry_years
from india_gex_engine import GEXSnapshot, IndiaGEXEngine, OptionStrikeData
from lag_neutralized_engine import FastBar

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

INDIA_VIX_SYMBOL = "NSE:INDIAVIX-INDEX"

_CHAIN_SHAPE_LOGGED = False

_OPTION_TYPES = ("CE", "PE", "CALL", "PUT")


def _is_option_row(row) -> bool:
    return (isinstance(row, dict)
            and str(row.get("option_type") or "").strip().upper() in _OPTION_TYPES)


def _log_chain_shape_once(rows) -> None:
    """Log the raw chain's field names once per process (keys only, no values).

    This is how the next run proves which fields Fyers actually sends -- the
    17 Nov 2026 zero-GEX day was caused by an IV field the parser expected
    but the feed did not carry, and the 23 Nov 2026 zero-GEX day by a whole
    response shape (flat rows) the parser did not read at all. The sample
    row is an actual option leg, not the first row: row 0 is the underlying
    index itself, whose keys say nothing about the option rows.
    """
    global _CHAIN_SHAPE_LOGGED
    if _CHAIN_SHAPE_LOGGED or not isinstance(rows, list) or not rows:
        return
    _CHAIN_SHAPE_LOGGED = True
    sample = next((r for r in rows if _is_option_row(r)),
                  rows[0] if isinstance(rows[0], dict) else {})
    call = sample.get("call") if isinstance(sample.get("call"), dict) else {}
    put = sample.get("put") if isinstance(sample.get("put"), dict) else {}
    logger.info("Option chain shape: %d rows; sample row keys=%s; call keys=%s; "
                "put keys=%s; sample has oi=%s greeks=%s",
                len(rows), sorted(sample.keys()), sorted(call.keys()),
                sorted(put.keys()), "oi" in sample, "greeks" in sample)


def _extract_ltp(payload) -> Optional[float]:
    """Extract an LTP from the several shapes Fyers quotes can take."""
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, dict):
        for key in ("ltp", "lp", "last_price", "close"):
            if key in payload and isinstance(payload[key], (int, float)):
                return float(payload[key])
        if isinstance(payload.get("v"), dict):
            return _extract_ltp(payload["v"])
        if isinstance(payload.get("d"), list) and payload["d"]:
            return _extract_ltp(payload["d"][0])
        if isinstance(payload.get("data"), list) and payload["data"]:
            return _extract_ltp(payload["data"][0])
        return None
    if isinstance(payload, list) and payload:
        return _extract_ltp(payload[0])
    return None


def get_option_quote(fyers_client, symbol: str) -> Optional[float]:
    """Return the option premium (LTP) for ``symbol``, or None if unknown.

    Never returns the underlying spot price. A return of None means the
    caller must not place an order for that symbol.
    """
    if fyers_client is None or not symbol:
        return None
    try:
        response = fyers_client.quotes({"symbols": symbol})
    except AttributeError:
        logger.warning("Fyers client has no quotes() method; cannot price %s", symbol)
        return None
    except Exception as exc:  # network / API failure: no price, no trade
        logger.warning("Quote fetch failed for %s: %s", symbol, exc)
        return None
    ltp = _extract_ltp(response)
    if ltp is None or not math.isfinite(ltp) or ltp <= 0:
        logger.warning("No usable quote for %s (response: %r)", symbol, response)
        return None
    return float(ltp)


def get_batch_quotes(fyers_client, symbols) -> dict:
    """LTP per symbol for up to 50 symbols in ONE quotes call, or {}.

    One GET /data/quotes call with a comma-separated symbol list (the
    SDK's ``quotes()`` takes up to 50). This is the fallback /
    verification path when the socket drops: the orchestrator asks for
    the leg + spot + VIX together instead of one REST call per symbol.
    ``get_option_quote`` stays the primary single-symbol path and keeps
    working unchanged. Symbols without a usable LTP are simply absent
    from the result — callers must not invent a price for them.
    """
    if fyers_client is None or not symbols:
        return {}
    wanted = list(dict.fromkeys(s for s in symbols if s))[:50]
    if not wanted:
        return {}
    try:
        response = fyers_client.quotes({"symbols": ",".join(wanted)})
    except AttributeError:
        logger.warning("Fyers client has no quotes() method; batch quotes unavailable")
        return {}
    except Exception as exc:  # network / API failure: no prices, no trade
        logger.warning("Batch quote fetch failed: %s", exc)
        return {}
    out = {}
    entries = None
    if isinstance(response, dict):
        entries = response.get("d") or response.get("data")
    if isinstance(entries, dict):
        items = list(entries.items())
    elif isinstance(entries, list):
        items = [(None, entry) for entry in entries]
    else:
        # A single-quote-shaped response: fall back to _extract_ltp.
        ltp = _extract_ltp(response)
        if ltp is not None and math.isfinite(ltp) and ltp > 0:
            out[wanted[0]] = float(ltp)
        return out
    for key, entry in items:
        if not isinstance(entry, dict):
            continue
        sym = entry.get("symbol") or entry.get("n") or entry.get("sym") or key
        if not sym:
            continue
        ltp = _extract_ltp(entry)
        if ltp is not None and math.isfinite(ltp) and ltp > 0:
            out[str(sym)] = float(ltp)
    return out


def _leg_fields(row: dict) -> dict:
    """OI / IV / LTP / Greeks / depth of one flat Fyers option leg.

    IV and the Greeks live in the nested ``greeks`` object when
    ``greeks=1`` was requested, else (rarely) top-level; either is
    accepted. Every field Fyers sends on the row is kept: ``oich`` /
    ``oichp`` / ``prev_oi`` / ``volume`` / ``bid`` / ``ask`` cost
    nothing extra — they arrive in the same response the engine
    already fetches — and they are what step 1 of the Fyers data gaps
    (25 Nov 2026) is built on. A missing field reads as 0, meaning
    "not carried", never a measured zero.
    """
    greeks = row.get("greeks") if isinstance(row.get("greeks"), dict) else {}
    iv = row.get("iv", greeks.get("iv", 0)) or 0

    def _f(*names, default=0.0):
        for name in names:
            value = row.get(name, greeks.get(name, None))
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return default

    def _i(*names, default=0):
        for name in names:
            value = row.get(name, None)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return default

    try:
        return {
            "oi": _i("oi", "open_interest"),
            "iv": float(iv),
            "ltp": _f("ltp", "lp"),
            "oich": _i("oich"),
            "oichp": _f("oichp"),
            "prev_oi": _i("prev_oi"),
            "volume": _i("volume", "vol"),
            "bid": _f("bid"),
            "ask": _f("ask"),
            "delta": _f("delta"),
            "gamma": _f("gamma"),
            "theta": _f("theta"),
            "vega": _f("vega"),
        }
    except (TypeError, ValueError):
        return {"oi": 0, "iv": 0.0, "ltp": 0.0, "oich": 0, "oichp": 0.0,
                "prev_oi": 0, "volume": 0, "bid": 0.0, "ask": 0.0,
                "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}


def _strike_fields(entry: dict) -> Optional[OptionStrikeData]:
    """Parse one NESTED option-chain row into OptionStrikeData, or None.

    Nested shape: one row per strike with ``call``/``put`` dicts. This is
    the shape the repo's own tests use; the live Fyers feed does not
    return it (see module docstring), but it is kept working.
    """
    if not isinstance(entry, dict):
        return None
    strike = entry.get("strike_price", entry.get("strike"))
    if strike is None:
        return None

    def _side(name: str) -> dict:
        side = entry.get(name, {})
        if not isinstance(side, dict):
            return {}
        return side

    call = _side("call")
    put = _side("put")

    def _side_leg(side: dict) -> dict:
        """Same fields as _leg_fields, for one nested call/put dict."""
        greeks = side.get("greeks") if isinstance(side.get("greeks"), dict) else {}
        out = {"oi": 0, "iv": 0.0, "ltp": 0.0, "oich": 0, "oichp": 0.0,
               "prev_oi": 0, "volume": 0, "bid": 0.0, "ask": 0.0,
               "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        for key in ("oi", "oich", "prev_oi", "volume"):
            raw = side.get(key)
            if raw is None and key == "oi":
                raw = side.get("open_interest")
            try:
                out[key] = int(raw or 0)
            except (TypeError, ValueError):
                out[key] = 0
        for key in ("iv", "oichp", "ltp", "bid", "ask",
                    "delta", "gamma", "theta", "vega"):
            raw = side.get(key, greeks.get(key, 0))
            if key == "ltp":
                raw = side.get("ltp", side.get("lp", greeks.get("ltp", 0)))
            try:
                out[key] = float(raw or 0)
            except (TypeError, ValueError):
                out[key] = 0.0
        return out

    c = _side_leg(call)
    p = _side_leg(put)
    try:
        return OptionStrikeData(
            strike=float(strike),
            call_oi=c["oi"], put_oi=p["oi"],
            call_iv=c["iv"], put_iv=p["iv"],
            call_ltp=c["ltp"], put_ltp=p["ltp"],
            call_oich=c["oich"], put_oich=p["oich"],
            call_oichp=c["oichp"], put_oichp=p["oichp"],
            call_prev_oi=c["prev_oi"], put_prev_oi=p["prev_oi"],
            call_volume=c["volume"], put_volume=p["volume"],
            call_bid=c["bid"], put_bid=p["bid"],
            call_ask=c["ask"], put_ask=p["ask"],
            call_delta=c["delta"], put_delta=p["delta"],
            call_gamma=c["gamma"], put_gamma=p["gamma"],
            call_theta=c["theta"], put_theta=p["theta"],
            call_vega=c["vega"], put_vega=p["vega"],
        )
    except (TypeError, ValueError):
        return None


def _parse_chain_rows(rows) -> List[OptionStrikeData]:
    """Parse raw chain rows (flat Fyers legs and/or nested rows) into
    one OptionStrikeData per strike, sorted by strike.

    Flat rows are grouped by ``strike_price``; a row whose
    ``option_type`` is not CE/PE (the index row, ``strike_price`` -1,
    has an empty one) is skipped. A strike with only one leg still
    parses, with the missing side at zero.
    """
    by_strike: dict = {}
    for entry in rows or []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("call"), dict) or isinstance(entry.get("put"), dict):
            parsed = _strike_fields(entry)
            if parsed is not None:
                by_strike[parsed.strike] = parsed
            continue
        if not _is_option_row(entry):
            continue
        strike = entry.get("strike_price", entry.get("strike"))
        if strike is None:
            continue
        try:
            strike = float(strike)
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        leg = _leg_fields(entry)
        slot = by_strike.setdefault(strike, {"call": None, "put": None})
        if str(entry.get("option_type")).strip().upper() in ("CE", "CALL"):
            slot["call"] = leg
        else:
            slot["put"] = leg
    out = []
    for strike in sorted(by_strike):
        slot = by_strike[strike]
        if isinstance(slot, OptionStrikeData):
            out.append(slot)
            continue
        zero = {"oi": 0, "iv": 0.0, "ltp": 0.0, "oich": 0, "oichp": 0.0,
                "prev_oi": 0, "volume": 0, "bid": 0.0, "ask": 0.0,
                "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        call = slot["call"] or dict(zero)
        put = slot["put"] or dict(zero)
        out.append(OptionStrikeData(
            strike=float(strike),
            call_oi=call["oi"], put_oi=put["oi"],
            call_iv=call["iv"], put_iv=put["iv"],
            call_ltp=call["ltp"], put_ltp=put["ltp"],
            call_oich=call["oich"], put_oich=put["oich"],
            call_oichp=call["oichp"], put_oichp=put["oichp"],
            call_prev_oi=call["prev_oi"], put_prev_oi=put["prev_oi"],
            call_volume=call["volume"], put_volume=put["volume"],
            call_bid=call["bid"], put_bid=put["bid"],
            call_ask=call["ask"], put_ask=put["ask"],
            call_delta=call["delta"], put_delta=put["delta"],
            call_gamma=call["gamma"], put_gamma=put["gamma"],
            call_theta=call["theta"], put_theta=put["theta"],
            call_vega=call["vega"], put_vega=put["vega"],
        ))
    return out


def _expiry_timestamp(expiry) -> str:
    """Epoch seconds of the expiry day's 15:30 IST close, as Fyers expects
    for the option-chain ``timestamp`` parameter."""
    moment = datetime.combine(expiry, dtime(15, 30), tzinfo=IST)
    return str(int(moment.timestamp()))


def _extract_india_vix(data) -> Optional[float]:
    """India VIX from the chain response's ``indiavixData``, if carried.

    The docs do not publish the sub-fields, so this parses defensively:
    a bare number, or a dict/list holding one under ltp/value/vix/close.
    None means "not carried", never a measured zero.
    """
    raw = data.get("indiavixData") if isinstance(data, dict) else None
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    if isinstance(raw, dict):
        for key in ("ltp", "value", "vix", "close", "price"):
            value = raw.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
        return None
    if isinstance(raw, list) and raw:
        return _extract_india_vix({"indiavixData": raw[0]})
    return None


def _parse_expiry_dates(data) -> list:
    """Expiry dates Fyers lists for this underlying (``expiryData[]``).

    Each entry is {"date": "DD-MM-YYYY", "expiry": <epoch>}. Unparseable
    entries are skipped; an empty list means "not carried".
    """
    out = []
    if not isinstance(data, dict):
        return out
    for entry in data.get("expiryData") or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("date")
        if isinstance(raw, str):
            try:
                day, month, year = (int(part) for part in raw.split("-"))
                out.append(date(year, month, day))
            except (TypeError, ValueError):
                continue
        elif isinstance(entry.get("expiry"), (int, float)):
            try:
                out.append(datetime.fromtimestamp(entry["expiry"], tz=IST).date())
            except (TypeError, ValueError, OverflowError, OSError):
                continue
    return out


def check_market_open(fyers_client):
    """Market status for the NSE session.

    Returns ("open" | "closed" | "unknown", detail). "open" covers
    OPEN and PREOPEN (the session is about to start). "closed" only
    when Fyers positively reports CLOSE for an NSE NORMAL session.
    Anything unparseable, or a failed call, is "unknown" — callers
    proceed with a warning rather than blocking a run on a status
    they cannot read. Needs the Transactions Info permission; without
    it the call fails and the answer is "unknown".
    """
    if fyers_client is None:
        return "unknown", "no Fyers client"
    method = getattr(fyers_client, "market_status", None)
    if not callable(method):
        return "unknown", "no market_status() method"
    try:
        response = method()
    except Exception as exc:
        logger.warning("Market status fetch failed: %s", exc)
        return "unknown", f"fetch failed: {exc}"
    rows = None
    if isinstance(response, dict):
        rows = (response.get("marketStatus") or response.get("data")
                or response.get("d"))
    if not isinstance(rows, list):
        return "unknown", "unparseable market-status response"
    statuses = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("market_type", "")).upper() != "NORMAL":
            continue
        exchange = str(row.get("exchange", "")).upper()
        if exchange and exchange not in ("NSE", "NSECM", "NSEFO"):
            continue
        statuses.add(str(row.get("status", "")).upper())
    if "OPEN" in statuses or "PREOPEN" in statuses:
        return "open", "NSE NORMAL " + "/".join(sorted(statuses))
    if "CLOSE" in statuses or "CLOSED" in statuses:
        return "closed", "NSE NORMAL " + "/".join(sorted(statuses))
    return "unknown", "no NSE NORMAL session row found"


def validate_weekly_symbol(snapshot, symbol: str, expiry, strike: float):
    """Check a built option symbol against the live chain (step 6).

    Returns (ok, reason). The expiry must be one Fyers lists in
    expiryData[] and the strike must be on the chain just fetched.
    When the snapshot carries neither (no chain, old feed), validation
    is skipped and the string-formatted symbol stands — the formatted
    symbol is the fallback, not the thing being replaced.
    """
    if snapshot is None:
        return True, "no snapshot; skipping validation"
    expiry_dates = getattr(snapshot, "expiry_dates", ()) or ()
    if expiry_dates and expiry not in expiry_dates:
        return False, (f"expiry {expiry} not in Fyers expiry list "
                       f"{sorted(expiry_dates)}")
    strikes = getattr(snapshot, "strikes", ()) or ()
    if strikes and float(strike) not in [float(s) for s in strikes]:
        return False, f"strike {strike} not on the fetched chain"
    return True, "validated against the live chain"


def spread_blocks_trade(spread: float, max_spread: float) -> bool:
    """True when a measured bid/ask spread is too wide to trade on.

    A spread of 0.0 means the feed carried no bid/ask — unknown, not a
    zero spread — so it never blocks. The live depth check (step 5) is
    the guard for that case.
    """
    return spread > 0 and spread > max_spread


def fetch_option_chain(fyers_client, symbol: str = "NSE:NIFTY50-INDEX",
                       expiry=None, strikecount: int = 15) -> Optional[List[OptionStrikeData]]:
    """Fetch the option chain for ``symbol``; None if unavailable.

    Tries the Fyers option-chain methods if the client exposes them.
    ``greeks=1`` is always requested so the rows carry IV. When
    ``expiry`` is given the chain is pinned to that expiry via
    ``timestamp``; if that fetch fails or parses to too few strikes,
    it falls back to Fyers' default (current) expiry, so a bad
    timestamp can never leave the engine with no chain at all.
    Returns None (not an empty chain) when the chain cannot be fetched
    or yields too few strikes to be meaningful.
    """
    chain, _meta = fetch_option_chain_with_meta(
        fyers_client, symbol=symbol, expiry=expiry, strikecount=strikecount)
    return chain


def fetch_option_chain_with_meta(fyers_client, symbol: str = "NSE:NIFTY50-INDEX",
                                 expiry=None, strikecount: int = 15):
    """(chain, meta) for the option chain; chain is None when unavailable.

    ``meta`` carries what the response says beyond the rows: ``india_vix``
    (float or None), ``expiry_dates`` (list of date), ``call_oi`` /
    ``put_oi`` totals. Same fetch + fallback behaviour as
    ``fetch_option_chain``, which is a thin wrapper over this.
    """
    if fyers_client is None:
        return None, {"india_vix": None, "expiry_dates": [],
                      "call_oi": 0, "put_oi": 0}
    method = None
    for name in ("optionchain", "get_option_chain", "option_chain"):
        method = getattr(fyers_client, name, None)
        if callable(method):
            break
    if method is None:
        logger.warning("Fyers client exposes no option-chain method; GEX unavailable")
        return None, {"india_vix": None, "expiry_dates": [],
                      "call_oi": 0, "put_oi": 0}
    payloads = []
    if expiry is not None:
        try:
            payloads.append({"symbol": symbol, "strikecount": strikecount,
                             "timestamp": _expiry_timestamp(expiry), "greeks": "1"})
        except Exception as exc:  # bad expiry value: fall through to default
            logger.warning("Could not build expiry timestamp (%s); using default expiry", exc)
    payloads.append({"symbol": symbol, "strikecount": strikecount, "greeks": "1"})
    for payload in payloads:
        try:
            response = method(payload)
        except Exception as exc:
            logger.warning("Option-chain fetch failed: %s", exc)
            continue
        data = response.get("data", response) if isinstance(response, dict) else None
        rows = None
        if isinstance(data, dict):
            rows = data.get("optionsChain") or data.get("optionChain") or data.get("chain")
        _log_chain_shape_once(rows)
        if not isinstance(rows, list) or len(rows) < 5:
            logger.warning("Option chain unusable (rows=%s); GEX unavailable",
                           len(rows) if isinstance(rows, list) else rows)
            continue
        chain = _parse_chain_rows(rows)
        if len(chain) < 5:
            logger.warning("Option chain parsed to only %d strikes; GEX unavailable", len(chain))
            continue
        meta = {
            "india_vix": _extract_india_vix(data),
            "expiry_dates": _parse_expiry_dates(data),
            "call_oi": int(data.get("callOi", 0) or 0) if isinstance(data, dict) else 0,
            "put_oi": int(data.get("putOi", 0) or 0) if isinstance(data, dict) else 0,
        }
        return chain, meta
    return None, {"india_vix": None, "expiry_dates": [],
                  "call_oi": 0, "put_oi": 0}


def compute_gex_snapshot(
    fyers_client,
    spot_price: float,
    expiry,
    underlying: str = "NIFTY",
    symbol: str = "NSE:NIFTY50-INDEX",
    strikecount: int = 15,
    archive: bool = False,
) -> Optional[GEXSnapshot]:
    """Full GEX snapshot (walls, flip level, regime) from the live chain.

    Returns None when the chain cannot be fetched or parsed, in which
    case callers must not signal -- never fall back to a constant.
    ``strikecount`` is the strikes per side of ATM (15 = 31 strikes;
    50 = the wide chain in step 7). ``archive`` appends the fetched
    chain to the local JSONL archive (step 4); it defaults to False
    so tests and one-off callers do not write files — the live
    orchestrator passes True.
    """
    if spot_price is None or spot_price <= 0:
        return None
    chain, meta = fetch_option_chain_with_meta(
        fyers_client, symbol=symbol, expiry=expiry, strikecount=strikecount)
    if not chain:
        return None
    tte = time_to_expiry_years(expiry)
    try:
        snapshot = IndiaGEXEngine().process_chain(
            underlying=underlying,
            spot_price=float(spot_price),
            tte_years=tte,
            chain=chain,
            india_vix=meta.get("india_vix"),
            expiry_dates=meta.get("expiry_dates") or (),
        )
    except Exception as exc:  # GEX maths must never crash the live loop
        logger.warning("GEX computation failed: %s", exc)
        return None
    if archive:
        # Archive the raw chain even when net GEX is zero: a zero-GEX
        # chain is still history, and the archive is the only
        # historical-chain dataset the engine will ever have.
        archive_chain_snapshot(chain, spot=spot_price, expiry=expiry,
                               snapshot=snapshot, vix=meta.get("india_vix"))
    logger.info("GEX net_dex=%.2f regime=%s flip=%.2f call_wall=%.0f put_wall=%.0f "
                "max_pain=%.0f pcr=%.2f pcr_oi_chg=%.2f vix=%s",
                snapshot.net_gex, snapshot.market_regime, snapshot.gamma_flip_level,
                snapshot.call_wall, snapshot.put_wall, snapshot.max_pain, snapshot.pcr,
                snapshot.pcr_oi_change,
                f"{snapshot.india_vix:.2f}" if snapshot.india_vix else "n/a")
    if snapshot.net_gex == 0.0:
        # A real chain never nets to exactly zero: with real open interest
        # the call/put gammas cannot cancel to the paise. Zero means the
        # chain carried no usable gamma (no IV and no price to derive it
        # from, or no open interest at all). Report it as unavailable --
        # the engine HOLDs -- rather than a regime and flip the engine
        # would trade on.
        total_oi = sum(d.call_oi + d.put_oi for d in chain)
        logger.warning(
            "GEX is zero on a %d-strike chain (total OI %d); treating "
            "dealer gamma as unavailable -- no signal on GEX",
            len(chain), total_oi)
        return None
    return snapshot


def compute_dealer_net_dex(
    fyers_client,
    spot_price: float,
    expiry,
    underlying: str = "NIFTY",
    symbol: str = "NSE:NIFTY50-INDEX",
) -> Optional[float]:
    """Net dealer gamma exposure from the live option chain, or None.

    None means "unknown" -- callers must not signal on it. It is never
    replaced with a hardcoded constant.
    """
    snapshot = compute_gex_snapshot(fyers_client, spot_price, expiry,
                                    underlying=underlying, symbol=symbol)
    return float(snapshot.net_gex) if snapshot is not None else None


# ---------------------------------------------------------------- step 4
def fetch_option_history(fyers_client, symbol: str, day=None,
                         resolution: str = "5"):
    """Premium bars for one ACTIVE option symbol, from the open.

    History with ``oi_flag=1`` so each candle also carries open
    interest. Returns {"bars": [FastBar], "vwap": float|None,
    "atr": float|None} or None when the fetch fails or returns no
    candles. Only works for active contracts: an expired symbol
    returns Fyers error -300, which surfaces here as None (use
    ``fetch_expired_option_history``, step 8, for expired symbols).
    VWAP is volume-weighted on typical price; ATR is the mean true
    range of the returned bars.
    """
    if fyers_client is None or not symbol:
        return None
    day = day or datetime.now(IST).date()
    day_str = day.isoformat() if hasattr(day, "isoformat") else str(day)
    try:
        response = fyers_client.history(data={
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": day_str,
            "range_to": day_str,
            "cont_flag": "1",
            "oi_flag": "1",
        })
    except AttributeError:
        logger.warning("Fyers client has no history() method; option history unavailable")
        return None
    except Exception as exc:
        logger.warning("Option history fetch failed for %s: %s", symbol, exc)
        return None
    candles = response.get("candles", []) if isinstance(response, dict) else []
    bars = []
    try:
        bar_seconds = int(resolution) * 60
    except (TypeError, ValueError):
        bar_seconds = 300
    for candle in candles:
        try:
            ts, open_p, high_p, low_p, close_p, volume = candle[:6]
        except (TypeError, ValueError):
            continue
        ts_str = datetime.fromtimestamp(ts, tz=IST).strftime("%H:%M")
        bars.append(FastBar(ts_str, float(open_p), float(high_p), float(low_p),
                            float(close_p), int(volume), bar_seconds))
    if not bars:
        return None
    total_vol = sum(b.volume for b in bars)
    vwap = None
    if total_vol > 0:
        vwap = round(sum(((b.high + b.low + b.close) / 3.0) * b.volume
                         for b in bars) / total_vol, 2)
    true_ranges = []
    prev_close = None
    for b in bars:
        if prev_close is None:
            true_ranges.append(b.high - b.low)
        else:
            true_ranges.append(max(b.high - b.low,
                                   abs(b.high - prev_close),
                                   abs(b.low - prev_close)))
        prev_close = b.close
    atr = round(sum(true_ranges) / len(true_ranges), 2) if true_ranges else None
    logger.info("Option history %s: %d bars, VWAP %s, ATR %s",
                symbol, len(bars),
                f"{vwap:.2f}" if vwap is not None else "n/a",
                f"{atr:.2f}" if atr is not None else "n/a")
    return {"bars": bars, "vwap": vwap, "atr": atr}


def archive_chain_snapshot(chain, *, spot, expiry, snapshot=None,
                           vix=None, archive_dir=None) -> Optional[str]:
    """Append one chain snapshot as a JSON line; returns the file path.

    One file per trading day: ``chain_YYYY-MM-DD.jsonl`` under
    CHAIN_ARCHIVE_DIR (env) or ``chain_archive/`` under the working
    dir. Never raises — archiving must never break the live loop; a
    failure logs a warning and returns None. This is the only
    historical-chain dataset the engine will ever have: Fyers serves
    no historical option chains, so snapshots not saved today are
    gone forever. On GitHub Actions the directory lives only for the
    run unless the workflow uploads it as an artifact (a workflow
    edit is out of scope for this PR — see the PR body).
    """
    if not chain:
        return None
    try:
        base = archive_dir or os.environ.get("CHAIN_ARCHIVE_DIR", "chain_archive")
        os.makedirs(base, exist_ok=True)
        day = datetime.now(IST).date().isoformat()
        path = os.path.join(base, f"chain_{day}.jsonl")
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "trading_date": day,
            "expiry": expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry),
            "spot": round(float(spot), 2),
            "vix": vix,
            "call_wall": getattr(snapshot, "call_wall", None),
            "put_wall": getattr(snapshot, "put_wall", None),
            "max_pain": getattr(snapshot, "max_pain", None),
            "pcr": getattr(snapshot, "pcr", None),
            "pcr_oi_change": getattr(snapshot, "pcr_oi_change", None),
            "strikes": [{
                "strike": d.strike,
                "call_oi": d.call_oi, "put_oi": d.put_oi,
                "call_oich": d.call_oich, "put_oich": d.put_oich,
                "call_iv": d.call_iv, "put_iv": d.put_iv,
                "call_ltp": d.call_ltp, "put_ltp": d.put_ltp,
                "call_bid": d.call_bid, "put_bid": d.put_bid,
                "call_ask": d.call_ask, "put_ask": d.put_ask,
                "call_gamma": d.call_gamma, "put_gamma": d.put_gamma,
                "call_delta": d.call_delta, "put_delta": d.put_delta,
                "call_volume": d.call_volume, "put_volume": d.put_volume,
            } for d in chain],
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return path
    except Exception as exc:  # disk full, read-only FS, bad path — non-fatal
        logger.warning("Chain archive failed: %s", exc)
        return None


# ---------------------------------------------------------------- step 5
def get_market_depth(fyers_client, symbol: str) -> Optional[dict]:
    """Best bid/ask + book totals for one symbol, or None.

    GET /data/depth (1 symbol per call, ohlcv_flag=1). Returns
    {"bid", "ask", "spread", "ltp", "total_buy_qty",
    "total_sell_qty", "oi"} — a missing/empty book reads as None (no
    spread to judge), never a fabricated zero spread.
    """
    if fyers_client is None or not symbol:
        return None
    method = getattr(fyers_client, "depth", None)
    if not callable(method):
        logger.warning("Fyers client has no depth() method; depth unavailable")
        return None
    try:
        response = method({"symbol": symbol, "ohlcv_flag": 1})
    except Exception as exc:
        logger.warning("Market depth fetch failed for %s: %s", symbol, exc)
        return None
    depth = None
    if isinstance(response, dict):
        raw = response.get("d") or response.get("data")
        if isinstance(raw, dict) and symbol in raw:
            depth = raw[symbol]
        elif isinstance(raw, dict):
            depth = raw
        elif isinstance(raw, list) and raw:
            depth = raw[0]
    if not isinstance(depth, dict):
        return None

    def _best(side):
        rows = depth.get(side)
        if isinstance(rows, list) and rows:
            first = rows[0]
            if isinstance(first, (list, tuple)) and first:
                return float(first[0])
            if isinstance(first, dict):
                for key in ("price", "p"):
                    if isinstance(first.get(key), (int, float)):
                        return float(first[key])
            if isinstance(first, (int, float)):
                return float(first)
        if isinstance(rows, (int, float)):
            return float(rows)
        return None

    bid = _best("bids") or _best("bid")
    ask = _best("ask") or _best("asks")
    if bid is None or ask is None or ask <= bid:
        return None
    try:
        return {
            "bid": bid,
            "ask": ask,
            "spread": round(ask - bid, 2),
            "ltp": float(depth["ltp"]) if isinstance(depth.get("ltp"), (int, float)) else None,
            "total_buy_qty": int(depth.get("totalbuyqty", 0) or 0),
            "total_sell_qty": int(depth.get("totalsellqty", 0) or 0),
            "oi": int(depth.get("oi", 0) or 0),
        }
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- step 8
def fetch_expired_option_history(fyers_client, symbol: str,
                                 range_from: str, range_to: str,
                                 resolution: str = "60",
                                 include_oi: int = 1,
                                 include_greeks: int = 1):
    """Candles for an EXPIRED F&O symbol — pipeline validation only.

    Uses the expired-data endpoint (``fno_historical_data``) with
    ``include_oi``/``include_greeks``. Returns a list of dicts
    {"ts","open","high","low","close","volume","oi","greeks"} or None
    when the fetch fails. Standard plan caps intraday expired requests
    at 10/day — enough to validate the pipeline, not to backtest a
    season (see the PR body for the Prime decision).
    """
    if fyers_client is None or not symbol:
        return None
    method = getattr(fyers_client, "fno_historical_data", None)
    if not callable(method):
        logger.warning("Fyers client has no fno_historical_data() method")
        return None
    try:
        response = method({
            "symbol": symbol,
            "resolution": resolution,
            "date_format": 1,
            "range_from": range_from,
            "range_to": range_to,
            "include_oi": include_oi,
            "include_greeks": include_greeks,
        })
    except Exception as exc:
        logger.warning("Expired F&O fetch failed for %s: %s", symbol, exc)
        return None
    candles = response.get("candles", []) if isinstance(response, dict) else []
    out = []
    for candle in candles:
        try:
            ts, open_p, high_p, low_p, close_p, volume = candle[:6]
        except (TypeError, ValueError):
            continue
        row = {
            "ts": ts,
            "open": float(open_p), "high": float(high_p),
            "low": float(low_p), "close": float(close_p),
            "volume": int(volume),
            "oi": None, "greeks": None,
        }
        if len(candle) > 6 and isinstance(candle[6], (int, float)):
            row["oi"] = int(candle[6])
        if len(candle) > 7 and isinstance(candle[7], dict):
            row["greeks"] = candle[7]
        out.append(row)
    return out or None
