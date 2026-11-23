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

import logging
import math
from datetime import datetime, time as dtime, timedelta, timezone
from typing import List, Optional

from expiry import time_to_expiry_years
from india_gex_engine import GEXSnapshot, IndiaGEXEngine, OptionStrikeData

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

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


def _leg_fields(row: dict) -> dict:
    """OI / IV / LTP of one flat Fyers option leg.

    IV lives in the nested ``greeks`` object when ``greeks=1`` was
    requested, else (rarely) top-level; either is accepted.
    """
    greeks = row.get("greeks") if isinstance(row.get("greeks"), dict) else {}
    iv = row.get("iv", greeks.get("iv", 0)) or 0
    try:
        return {
            "oi": int(row.get("oi", row.get("open_interest", 0)) or 0),
            "iv": float(iv),
            "ltp": float(row.get("ltp", row.get("lp", 0)) or 0),
        }
    except (TypeError, ValueError):
        return {"oi": 0, "iv": 0.0, "ltp": 0.0}


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
    try:
        return OptionStrikeData(
            strike=float(strike),
            call_oi=int(call.get("oi", call.get("open_interest", 0)) or 0),
            put_oi=int(put.get("oi", put.get("open_interest", 0)) or 0),
            call_iv=float(call.get("iv", 0) or 0),
            put_iv=float(put.get("iv", 0) or 0),
            call_ltp=float(call.get("ltp", call.get("lp", 0)) or 0),
            put_ltp=float(put.get("ltp", put.get("lp", 0)) or 0),
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
        call = slot["call"] or {"oi": 0, "iv": 0.0, "ltp": 0.0}
        put = slot["put"] or {"oi": 0, "iv": 0.0, "ltp": 0.0}
        out.append(OptionStrikeData(
            strike=float(strike),
            call_oi=call["oi"], put_oi=put["oi"],
            call_iv=call["iv"], put_iv=put["iv"],
            call_ltp=call["ltp"], put_ltp=put["ltp"],
        ))
    return out


def _expiry_timestamp(expiry) -> str:
    """Epoch seconds of the expiry day's 15:30 IST close, as Fyers expects
    for the option-chain ``timestamp`` parameter."""
    moment = datetime.combine(expiry, dtime(15, 30), tzinfo=IST)
    return str(int(moment.timestamp()))


def fetch_option_chain(fyers_client, symbol: str = "NSE:NIFTY50-INDEX",
                       expiry=None) -> Optional[List[OptionStrikeData]]:
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
    if fyers_client is None:
        return None
    method = None
    for name in ("optionchain", "get_option_chain", "option_chain"):
        method = getattr(fyers_client, name, None)
        if callable(method):
            break
    if method is None:
        logger.warning("Fyers client exposes no option-chain method; GEX unavailable")
        return None
    payloads = []
    if expiry is not None:
        try:
            payloads.append({"symbol": symbol, "strikecount": 15,
                             "timestamp": _expiry_timestamp(expiry), "greeks": "1"})
        except Exception as exc:  # bad expiry value: fall through to default
            logger.warning("Could not build expiry timestamp (%s); using default expiry", exc)
    payloads.append({"symbol": symbol, "strikecount": 15, "greeks": "1"})
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
        return chain
    return None


def compute_gex_snapshot(
    fyers_client,
    spot_price: float,
    expiry,
    underlying: str = "NIFTY",
    symbol: str = "NSE:NIFTY50-INDEX",
) -> Optional[GEXSnapshot]:
    """Full GEX snapshot (walls, flip level, regime) from the live chain.

    Returns None when the chain cannot be fetched or parsed, in which
    case callers must not signal -- never fall back to a constant.
    """
    if spot_price is None or spot_price <= 0:
        return None
    chain = fetch_option_chain(fyers_client, symbol=symbol, expiry=expiry)
    if not chain:
        return None
    tte = time_to_expiry_years(expiry)
    try:
        snapshot = IndiaGEXEngine().process_chain(
            underlying=underlying,
            spot_price=float(spot_price),
            tte_years=tte,
            chain=chain,
        )
    except Exception as exc:  # GEX maths must never crash the live loop
        logger.warning("GEX computation failed: %s", exc)
        return None
    logger.info("GEX net_dex=%.2f regime=%s flip=%.2f call_wall=%.0f put_wall=%.0f "
                "max_pain=%.0f pcr=%.2f",
                snapshot.net_gex, snapshot.market_regime, snapshot.gamma_flip_level,
                snapshot.call_wall, snapshot.put_wall, snapshot.max_pain, snapshot.pcr)
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
