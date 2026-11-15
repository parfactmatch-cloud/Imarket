"""
india_order_flow_engine.py
Real-time Cumulative Volume Delta (CVD) and Microstructure Imbalance Engine.

NOTE: NOT wired into the live path. The orchestrator uses
AdvancedAuctionFootprintEngine for CVD instead. Kept as a standalone
library module; see README "What is not in the live path".
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TickData:
    timestamp: str
    ltp: float
    qty: int
    best_bid: float
    best_ask: float


@dataclass
class BarDelta:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    delta: int          # Buy Volume minus Sell Volume
    cumulative_delta: int
    absorption_flag: Optional[str]  # "BULLISH_ABSORPTION", "BEARISH_EXHAUSTION", None


class IndiaOrderFlowEngine:
    def __init__(self):
        self.cumulative_delta = 0
        self.bar_ticks: List[TickData] = []
        self.delta_history: List[BarDelta] = []

    def process_tick(self, tick: TickData) -> int:
        """
        Lee-Ready Tick Rule for Indian Retail Feeds:
        LTP >= Best Ask -> Aggressive Buyer (+Delta)
        LTP <= Best Bid -> Aggressive Seller (-Delta)
        """
        if tick.ltp >= tick.best_ask and tick.best_ask > 0:
            tick_delta = tick.qty
        elif tick.ltp <= tick.best_bid and tick.best_bid > 0:
            tick_delta = -tick.qty
        else:
            tick_delta = 0

        self.cumulative_delta += tick_delta
        self.bar_ticks.append(tick)
        return tick_delta

    def complete_bar(self, timestamp: str) -> Optional[BarDelta]:
        if not self.bar_ticks:
            return None

        prices = [t.ltp for t in self.bar_ticks]
        total_vol = sum(t.qty for t in self.bar_ticks)

        bar_delta_val = sum(
            t.qty if t.ltp >= t.best_ask else (-t.qty if t.ltp <= t.best_bid else 0)
            for t in self.bar_ticks
        )

        b_open = prices[0]
        b_high = max(prices)
        b_low = min(prices)
        b_close = prices[-1]

        # Absorption Divergence Detection
        absorption = None
        if len(self.delta_history) >= 2:
            prev_bar = self.delta_history[-1]
            # Bullish Absorption: Price makes Lower Low, but Bar Delta makes Higher Low / Positive Delta
            if b_low < prev_bar.low and bar_delta_val > prev_bar.delta and bar_delta_val > 0:
                absorption = "BULLISH_ABSORPTION"
            # Bearish Exhaustion: Price makes Higher High, but Delta collapses
            elif b_high > prev_bar.high and bar_delta_val < prev_bar.delta and bar_delta_val < 0:
                absorption = "BEARISH_EXHAUSTION"

        bar = BarDelta(
            timestamp=timestamp,
            open=b_open,
            high=b_high,
            low=b_low,
            close=b_close,
            volume=total_vol,
            delta=bar_delta_val,
            cumulative_delta=self.cumulative_delta,
            absorption_flag=absorption
        )

        self.delta_history.append(bar)
        self.bar_ticks.clear()
        return bar
