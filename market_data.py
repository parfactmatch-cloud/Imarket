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
"""

import logging
import math
from typing import List, Optional

from expiry import time_to_expiry_years
from india_gex_engine import GEXSnapshot, IndiaGEXEngine, OptionStrikeData

logger = logging.getLogger(__name__)


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


def _strike_fields(entry: dict) -> Optional[OptionStrikeData]:
    """Parse one option-chain row into OptionStrikeData, or None."""
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


def fetch_option_chain(fyers_client, symbol: str = "NSE:NIFTY50-INDEX") -> Optional[List[OptionStrikeData]]:
    """Fetch the option chain for ``symbol``; None if unavailable.

    Tries the Fyers option-chain methods if the client exposes them.
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
    try:
        response = method({"symbol": symbol, "strikecount": 15})
    except Exception as exc:
        logger.warning("Option-chain fetch failed: %s", exc)
        return None
    data = response.get("data", response) if isinstance(response, dict) else None
    rows = None
    if isinstance(data, dict):
        rows = data.get("optionsChain") or data.get("optionChain") or data.get("chain")
    if not isinstance(rows, list) or len(rows) < 5:
        logger.warning("Option chain unusable (rows=%s); GEX unavailable",
                       len(rows) if isinstance(rows, list) else rows)
        return None
    chain = [row for row in (_strike_fields(r) for r in rows) if row is not None]
    if len(chain) < 5:
        logger.warning("Option chain parsed to only %d strikes; GEX unavailable", len(chain))
        return None
    return chain


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
    chain = fetch_option_chain(fyers_client, symbol=symbol)
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
    logger.info("GEX net_dex=%.2f regime=%s flip=%.2f",
                snapshot.net_gex, snapshot.market_regime, snapshot.gamma_flip_level)
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
