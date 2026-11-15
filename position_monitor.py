"""
position_monitor.py
Exit management for open positions.

The old code stored stop-loss / target levels on every position but
nothing ever read them: positions could only be closed by /panic (which
booked the exit at the entry price) or never. This monitor closes that
gap:

- Every check, each open position is re-priced at the *option's own*
  LTP (via ``quote_fn``). No quote -> no exit decision; a position is
  never exited blindly, and never at its entry price by default.
- LTP <= stop-loss: exit the full remaining quantity.
- LTP >= target 2: exit the full remaining quantity.
- LTP >= target 1: if the position is more than one lot, exit half
  (in whole lots); otherwise move the stop-loss to breakeven (entry)
  so the trade can no longer lose money, and keep running to T2.
- ``square_off_all``: end-of-day exit of everything at market, used
  before the 15:30 IST close.

All exits go through ExecutionRouter.close_position, which is the
single place that records fills and realised P&L.
"""

import logging
from typing import Callable, List, Optional

from config import JevConfig
from engine_state_store import EngineStateStore
from execution_broker_interface import ExecutionRouter

logger = logging.getLogger(__name__)


class PositionMonitor:
    def __init__(
        self,
        store: EngineStateStore,
        router: ExecutionRouter,
        quote_fn: Optional[Callable[[str], Optional[float]]] = None,
        config: Optional[JevConfig] = None,
    ):
        self.store = store
        self.router = router
        self.quote_fn = quote_fn
        self.config = config or JevConfig()

    def _quote(self, symbol: str) -> Optional[float]:
        if self.quote_fn is None:
            return None
        try:
            return self.quote_fn(symbol)
        except Exception as exc:  # a quote failure must not kill monitoring
            logger.warning("Quote failed for %s: %s", symbol, exc)
            return None

    def check_positions(self) -> List[str]:
        """Check every open position once. Returns a list of actions taken."""
        actions: List[str] = []
        for symbol, pos in self.store.load_open_positions().items():
            if pos.exit_order_id:
                # A live exit order is already pending for this position;
                # do not send a duplicate sell.
                continue
            ltp = self._quote(symbol)
            if ltp is None:
                logger.warning("No quote for %s; skipping exit check", symbol)
                continue

            if ltp <= pos.stop_loss:
                if self.router.close_position(symbol, pos.qty, ltp, reason="STOP_LOSS"):
                    actions.append(f"{symbol}: STOP_LOSS exit @ {ltp}")
                    logger.info("%s hit stop-loss %.2f (entry %.2f); exited",
                                symbol, ltp, pos.entry_price)
            elif ltp >= pos.target_2:
                if self.router.close_position(symbol, pos.qty, ltp, reason="TARGET_2"):
                    actions.append(f"{symbol}: TARGET_2 exit @ {ltp}")
                    logger.info("%s hit target 2 %.2f; exited", symbol, ltp)
            elif ltp >= pos.target_1 and not pos.sl_moved_to_breakeven:
                if pos.qty > self.config.lot_size:
                    half_lots = (pos.qty // self.config.lot_size) // 2
                    half_qty = half_lots * self.config.lot_size
                    if half_qty > 0 and self.router.close_position(
                        symbol, half_qty, ltp, reason="TARGET_1_PARTIAL"
                    ):
                        actions.append(f"{symbol}: TARGET_1 partial exit {half_qty} @ {ltp}")
                # Re-read the position: close_position may have reduced
                # its quantity, and mutating the stale copy would
                # overwrite that change.
                pos = self.store.load_open_positions().get(symbol)
                if pos is None:
                    continue
                # Move the stop to breakeven so the remainder cannot lose.
                pos.stop_loss = pos.entry_price
                pos.sl_moved_to_breakeven = True
                self.store.save_position(pos)
                actions.append(f"{symbol}: TARGET_1 hit; stop moved to breakeven")
                logger.info("%s hit target 1 %.2f; stop moved to breakeven %.2f",
                            symbol, ltp, pos.entry_price)
        return actions

    def square_off_all(self, reason: str = "END_OF_DAY") -> List[str]:
        """Close every open position at the current market price."""
        actions: List[str] = []
        for symbol, pos in self.store.load_open_positions().items():
            if pos.exit_order_id:
                continue
            ltp = self._quote(symbol)
            if ltp is None:
                logger.error("Cannot square off %s: no quote available; leaving open",
                             symbol)
                actions.append(f"{symbol}: SQUARE_OFF FAILED (no quote)")
                continue
            if self.router.close_position(symbol, pos.qty, ltp, reason=reason):
                actions.append(f"{symbol}: squared off @ {ltp}")
        return actions
