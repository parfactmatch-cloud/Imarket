"""
execution_broker_interface.py
Dual-Mode Order Router: Real Broker (Fyers API) + Paper Trading Simulator.
"""

from dataclasses import dataclass
from typing import Dict
from engine_state_store import EngineStateStore, PositionState


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    filled_price: float
    qty: int
    status: str  # "FILLED", "REJECTED"
    mode: str    # "PAPER", "LIVE"


class ExecutionRouter:
    def __init__(self, fyers_client, state_store: EngineStateStore, paper_mode: bool = True):
        self.fyers = fyers_client
        self.store = state_store
        self.paper_mode = paper_mode
        self.virtual_cash = 200000.0

    def execute_buy(self, symbol: str, qty: int, ltp: float, sl: float, t1: float, t2: float) -> OrderResult:
        if self.paper_mode or not self.fyers:
            simulated_fill = round(ltp * 1.005, 2)
            order_id = f"PAPER_{symbol}_{int(simulated_fill)}"
            pos = PositionState(
                symbol=symbol,
                qty=qty,
                entry_price=simulated_fill,
                stop_loss=sl,
                target_1=t1,
                target_2=t2,
                is_paper=True,
                status="OPEN"
            )
            self.store.save_position(pos)
            self.virtual_cash -= (simulated_fill * qty)
            return OrderResult(order_id, symbol, simulated_fill, qty, "FILLED", "PAPER")

        # Live Marketable Limit Order (+2% ceiling protection)
        limit_ceiling = round(ltp * 1.02, 2)
        payload = {
            "symbol": symbol,
            "qty": qty,
            "type": 1,
            "side": 1,
            "productType": "INTRADAY",
            "limitPrice": limit_ceiling,
            "stopPrice": 0,
            "validity": "IOC",
            "disclosedQty": 0,
            "offlineOrder": False
        }
        res = self.fyers.place_order(data=payload)
        if res.get("s") == "ok":
            order_id = res.get("id", "")
            pos = PositionState(symbol, qty, limit_ceiling, sl, t1, t2, False, "OPEN")
            self.store.save_position(pos)
            return OrderResult(order_id, symbol, limit_ceiling, qty, "FILLED", "LIVE")

        return OrderResult("", symbol, 0.0, 0, "REJECTED", "LIVE")

    def emergency_panic_close_all(self, current_prices: Dict[str, float]):
        open_positions = self.store.load_open_positions()
        for sym, pos in open_positions.items():
            exit_price = current_prices.get(sym, pos.entry_price)
            if self.paper_mode or not self.fyers:
                self.virtual_cash += (exit_price * pos.qty)
            else:
                self.fyers.place_order({
                    "symbol": sym,
                    "qty": pos.qty,
                    "type": 2,
                    "side": -1,
                    "productType": "INTRADAY"
                })
            pos.status = "CLOSED"
            self.store.save_position(pos)
