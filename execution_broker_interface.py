"""
execution_broker_interface.py
Dual-Mode Order Router: Real Broker (Fyers API) + Paper Trading Simulator.

Safety rules (read before changing):
- ``ltp`` is always the *option premium*, never the underlying spot.
  The old code passed the NIFTY spot (~24,000) as the option price,
  so a paper "fill" cost ~Rs 18 lakh against Rs 2 lakh virtual cash.
- Paper mode: an order is rejected -- not filled -- when its cost
  exceeds virtual cash, when the daily trade/loss limits are hit, or
  when a position is already open. Lot size must be a whole number of
  lots.
- Live mode: an order is only recorded as FILLED when the broker
  confirms it. A successfully placed order whose fill cannot be
  confirmed is PLACED, and the position stays OPEN with
  ``exit_order_id``/entry tracking so the monitor does not duplicate
  it. A rejected order records nothing.
- The Fyers client is wired in with ``set_fyers_client`` after login;
  the old code constructed the router with ``fyers_client=None`` and
  never updated it, so live execution was unreachable.
- ``emergency_panic_close_all`` exits at the *current market price*.
  If no price is available for a symbol it is left open and reported,
  never booked at its entry price.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

from config import JevConfig
from engine_state_store import EngineStateStore, PositionState

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    filled_price: float
    qty: int
    status: str  # "FILLED", "PLACED", "REJECTED"
    mode: str    # "PAPER", "LIVE"
    reason: str = ""


class ExecutionRouter:
    def __init__(
        self,
        fyers_client,
        state_store: EngineStateStore,
        paper_mode: bool = True,
        config: Optional[JevConfig] = None,
        quote_fn: Optional[Callable[[str], Optional[float]]] = None,
        depth_fn: Optional[Callable[[str], Optional[dict]]] = None,
    ):
        self.fyers = fyers_client
        self.store = state_store
        self.paper_mode = paper_mode
        self.config = config or JevConfig()
        self.quote_fn = quote_fn
        self.depth_fn = depth_fn
        self.virtual_cash = self.config.virtual_cash_initial

    # --- wiring -------------------------------------------------------
    def set_fyers_client(self, fyers_client) -> None:
        """Wire the authenticated Fyers client in after login."""
        self.fyers = fyers_client

    def set_quote_fn(self, quote_fn: Callable[[str], Optional[float]]) -> None:
        self.quote_fn = quote_fn

    def set_depth_fn(self, depth_fn: Callable[[str], Optional[dict]]) -> None:
        """Wire the market-depth function in after login (step 5)."""
        self.depth_fn = depth_fn

    # --- helpers ------------------------------------------------------
    def _today_ist(self) -> str:
        return datetime.now(IST).strftime("%Y-%m-%d")

    def _available_balance(self) -> Optional[float]:
        """Live account balance, or None when it cannot be determined.

        Tries the Fyers funds endpoint and tolerates its response
        shapes. None is treated by callers as "unknown" -- live orders
        are refused rather than placed blind.
        """
        if self.fyers is None:
            return None
        funds_fn = getattr(self.fyers, "funds", None)
        if not callable(funds_fn):
            return None
        try:
            res = funds_fn()
        except Exception as exc:
            logger.warning("Fyers funds() failed: %s", exc)
            return None
        try:
            data = res.get("fund_limit", []) if isinstance(res, dict) else []
            for entry in data:
                if isinstance(entry, dict):
                    for key in ("available_balance", "availableBalance", "balance"):
                        if key in entry and isinstance(entry[key], (int, float)):
                            return float(entry[key])
        except Exception as exc:
            logger.warning("Could not parse funds response: %s", exc)
        return None

    def _risk_check(self, symbol: str, qty: int, ltp: float) -> Optional[str]:
        """Return a rejection reason, or None when the order may proceed."""
        if qty <= 0 or qty % self.config.lot_size != 0:
            return f"qty {qty} is not a whole number of {self.config.lot_size}-lots"
        open_positions = self.store.load_open_positions()
        if len(open_positions) >= self.config.max_open_positions:
            return f"max open positions ({self.config.max_open_positions}) reached"
        if symbol in open_positions:
            return f"position already open in {symbol}"
        stats = self.store.get_daily_stats(self._today_ist())
        if stats["trades"] >= self.config.max_trades_per_day:
            return f"max trades per day ({self.config.max_trades_per_day}) reached"
        if stats["realized_pnl"] <= -self.config.max_daily_loss_rs:
            return (
                f"daily loss limit hit (realised Rs {stats['realized_pnl']:.2f} "
                f"<= -Rs {self.config.max_daily_loss_rs:.2f})"
            )
        cost = ltp * qty
        if self.paper_mode:
            if cost > self.virtual_cash:
                return f"insufficient virtual cash: cost Rs {cost:.2f} > Rs {self.virtual_cash:.2f}"
        else:
            balance = self._available_balance()
            if balance is None:
                return "live account balance unavailable; refusing order"
            if cost > balance:
                return f"insufficient account balance: cost Rs {cost:.2f} > Rs {balance:.2f}"
        return None

    def _depth_check(self, symbol: str) -> Optional[str]:
        """Spread guard from the live order book (step 5).

        Returns a rejection reason, or None when the trade may proceed.
        Fail-OPEN: when the depth fetch itself fails, returns nothing,
        or the router has no depth function, the trade proceeds — a
        data blip must not halt trading. Only a measured spread wider
        than the config threshold blocks.
        """
        if self.depth_fn is None:
            return None
        try:
            depth = self.depth_fn(symbol)
        except Exception as exc:
            logger.warning("Depth check failed for %s (%s); proceeding", symbol, exc)
            return None
        if not depth:
            logger.warning("No market depth for %s; proceeding", symbol)
            return None
        spread = depth.get("spread")
        if spread is None or spread <= 0:
            return None
        if spread > self.config.max_option_spread_rs:
            return (f"spread Rs {spread:.2f} > max "
                    f"Rs {self.config.max_option_spread_rs:.2f}")
        return None

    def _order_status(self, order_id: str) -> Optional[str]:
        """Best-effort fill check via the order book.

        Returns "FILLED", "REJECTED", or None when the status cannot be
        determined from the broker response.
        """
        if self.fyers is None or not order_id:
            return None
        orderbook_fn = getattr(self.fyers, "orderbook", None)
        if not callable(orderbook_fn):
            return None
        try:
            res = orderbook_fn()
        except Exception as exc:
            logger.warning("Order-book check failed for %s: %s", order_id, exc)
            return None
        orders = res.get("orderBook", []) if isinstance(res, dict) else []
        for order in orders:
            if not isinstance(order, dict):
                continue
            if str(order.get("id", "")) != str(order_id):
                continue
            status = str(order.get("status", "")).lower()
            if status in ("filled", "6", "complete", "completed"):
                return "FILLED"
            if status in ("rejected", "cancelled", "canceled"):
                return "REJECTED"
            return None
        return None

    # --- orders -------------------------------------------------------
    def execute_buy(self, symbol: str, qty: int, ltp: float, sl: float, t1: float, t2: float) -> OrderResult:
        """Buy ``qty`` of ``symbol`` at option premium ``ltp``.

        ``sl``/``t1``/``t2`` are premium levels, not spot levels.
        """
        if ltp is None or ltp <= 0:
            logger.error("Refusing buy of %s: no valid option premium", symbol)
            return OrderResult("", symbol, 0.0, qty, "REJECTED", "PAPER" if self.paper_mode else "LIVE",
                               "no valid option premium")
        mode = "PAPER" if (self.paper_mode or not self.fyers) else "LIVE"
        reason = self._risk_check(symbol, qty, ltp)
        if reason:
            logger.warning("Buy rejected for %s: %s", symbol, reason)
            return OrderResult("", symbol, 0.0, qty, "REJECTED", mode, reason)
        # Pre-trade depth check (step 5): the exact leg's live spread.
        # Applies in paper mode too — the paper fill models a real
        # entry, and a wide spread is a real entry cost. Exits are
        # never depth-checked: a square-off must not be blocked.
        reason = self._depth_check(symbol)
        if reason:
            logger.warning("Buy rejected for %s: %s", symbol, reason)
            return OrderResult("", symbol, 0.0, qty, "REJECTED", mode, reason)

        if mode == "PAPER":
            simulated_fill = round(ltp * 1.005, 2)
            order_id = f"PAPER_{symbol}_{int(simulated_fill)}"
            pos = PositionState(
                symbol=symbol, qty=qty, entry_price=simulated_fill,
                stop_loss=sl, target_1=t1, target_2=t2,
                is_paper=True, status="OPEN",
            )
            self.store.save_position(pos)
            self.store.record_trade(self._today_ist(), realized_pnl=0.0, trades=1)
            self.virtual_cash -= simulated_fill * qty
            logger.info("PAPER buy %s x%d @ %.2f (virtual cash %.2f)",
                        symbol, qty, simulated_fill, self.virtual_cash)
            return OrderResult(order_id, symbol, simulated_fill, qty, "FILLED", "PAPER")

        # Live marketable limit order (+2% ceiling protection).
        limit_ceiling = round(ltp * 1.02, 2)
        payload = {
            "symbol": symbol, "qty": qty, "type": 1, "side": 1,
            "productType": "INTRADAY", "limitPrice": limit_ceiling,
            "stopPrice": 0, "validity": "IOC", "disclosedQty": 0,
            "offlineOrder": False,
        }
        try:
            res = self.fyers.place_order(data=payload)
        except Exception as exc:
            logger.error("Live order failed for %s: %s", symbol, exc)
            return OrderResult("", symbol, 0.0, qty, "REJECTED", "LIVE", str(exc))
        if not isinstance(res, dict) or res.get("s") != "ok" or not res.get("id"):
            logger.error("Live order rejected for %s: %r", symbol, res)
            return OrderResult("", symbol, 0.0, qty, "REJECTED", "LIVE", f"broker response: {res!r}")
        order_id = str(res.get("id"))
        pos = PositionState(symbol, qty, limit_ceiling, sl, t1, t2, False, "OPEN")
        self.store.save_position(pos)
        self.store.record_trade(self._today_ist(), realized_pnl=0.0, trades=1)
        # Do NOT claim FILLED without confirmation from the order book.
        confirmed = self._order_status(order_id)
        if confirmed == "FILLED":
            logger.info("Live buy %s x%d filled @ %.2f", symbol, qty, limit_ceiling)
            return OrderResult(order_id, symbol, limit_ceiling, qty, "FILLED", "LIVE")
        if confirmed == "REJECTED":
            pos.status = "CLOSED"
            self.store.save_position(pos)
            return OrderResult(order_id, symbol, 0.0, qty, "REJECTED", "LIVE",
                               "broker reports order rejected")
        logger.info("Live buy %s placed (id %s); fill unconfirmed, status PLACED",
                    symbol, order_id)
        return OrderResult(order_id, symbol, limit_ceiling, qty, "PLACED", "LIVE",
                           "fill not yet confirmed by order book")

    def close_position(self, symbol: str, qty: int, ltp: float, reason: str = "") -> bool:
        """Exit ``qty`` of an open position at premium ``ltp``.

        Paper exits settle immediately and record realised P&L. Live
        exits place a sell order and only mark the position closed when
        the broker confirms the fill; otherwise the position stays OPEN
        with ``exit_order_id`` set so the monitor does not duplicate it.
        Returns True when the exit is settled or placed, else False.
        """
        positions = self.store.load_open_positions()
        pos = positions.get(symbol)
        if pos is None:
            logger.warning("close_position: no open position in %s", symbol)
            return False
        if qty <= 0 or qty > pos.qty:
            logger.warning("close_position: invalid qty %d for %s (open %d)", qty, symbol, pos.qty)
            return False
        if ltp is None or ltp <= 0:
            logger.error("close_position: no valid exit price for %s", symbol)
            return False

        realised = (ltp - pos.entry_price) * qty
        if self.paper_mode or not self.fyers:
            self.virtual_cash += ltp * qty
            self.store.record_trade(self._today_ist(), realized_pnl=realised, trades=0)
            if qty >= pos.qty:
                pos.status = "CLOSED"
            else:
                pos.qty -= qty
                pos.status = "PARTIAL_CLOSED"
            self.store.save_position(pos)
            logger.info("PAPER exit %s x%d @ %.2f (%s, realised %.2f)",
                        symbol, qty, ltp, reason, realised)
            return True

        try:
            res = self.fyers.place_order({
                "symbol": symbol, "qty": qty, "type": 2, "side": -1,
                "productType": "INTRADAY",
            })
        except Exception as exc:
            logger.error("Live exit failed for %s: %s", symbol, exc)
            return False
        if not isinstance(res, dict) or res.get("s") != "ok" or not res.get("id"):
            logger.error("Live exit rejected for %s: %r", symbol, res)
            return False
        order_id = str(res.get("id"))
        confirmed = self._order_status(order_id)
        if confirmed == "FILLED":
            self.store.record_trade(self._today_ist(), realized_pnl=realised, trades=0)
            if qty >= pos.qty:
                pos.status = "CLOSED"
            else:
                pos.qty -= qty
                pos.status = "PARTIAL_CLOSED"
            self.store.save_position(pos)
            return True
        # Fill unconfirmed: keep the position open, remember the order.
        pos.exit_order_id = order_id
        self.store.save_position(pos)
        logger.info("Live exit %s placed (id %s); fill unconfirmed", symbol, order_id)
        return True

    def emergency_panic_close_all(self, current_prices: Dict[str, float]):
        """Close all open positions at current market prices.

        Prices come from ``current_prices`` first, then from the
        router's quote function. A symbol with no available price is
        left OPEN and logged -- it is never booked at its entry price.
        """
        open_positions = self.store.load_open_positions()
        for sym, pos in open_positions.items():
            price = current_prices.get(sym) if current_prices else None
            if price is None and self.quote_fn is not None:
                try:
                    price = self.quote_fn(sym)
                except Exception as exc:
                    logger.warning("Panic quote failed for %s: %s", sym, exc)
                    price = None
            if price is None or price <= 0:
                logger.error(
                    "PANIC: no market price for %s; leaving position OPEN "
                    "(never booked at entry price)", sym)
                continue
            self.close_position(sym, pos.qty, float(price), reason="PANIC")
