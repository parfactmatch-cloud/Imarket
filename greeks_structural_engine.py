"""
greeks_structural_engine.py
Integrates Option Greeks, NR4/NR7 Volatility Compression, and Range Levels.
"""

from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class BarData:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class GreeksState:
    net_dex: float         # Net Dealer Delta Exposure
    net_gex: float         # Net Dealer Gamma Exposure
    atm_vega: float        # ATM Vega Sensitivity
    iv_skew: float         # Put IV minus Call IV (Fear Skew)


@dataclass
class StructuralSignal:
    timestamp: str
    compression_state: str  # "NR4", "NR7", "NORMAL"
    regime: str             # "SHORT_GAMMA_EXPANSION", "LONG_GAMMA_PINNING"
    setup_trigger: str      # "MOMENTUM_SHORT", "MEAN_REVERSION_FADE", "NO_TRADE"
    invalidation_level: float


class GreeksStructuralEngine:
    def __init__(self, lot_size: int = 75):
        self.lot_size = lot_size

    def check_nr_compression(self, bars: List[BarData]) -> str:
        if len(bars) < 7:
            return "NORMAL"

        ranges = [b.high - b.low for b in bars[-7:]]
        current_range = ranges[-1]

        if current_range == min(ranges):
            return "NR7"
        if current_range == min(ranges[-4:]):
            return "NR4"

        return "NORMAL"

    def evaluate_auction_levels(self, bars: List[BarData]) -> Tuple[float, float]:
        if len(bars) < 3:
            return (bars[-1].high, bars[-1].low) if bars else (0.0, 0.0)

        opening_bars = bars[:3]
        ah = max(b.high for b in opening_bars)
        al = min(b.low for b in opening_bars)
        return ah, al

    def process_confluence(
        self,
        current_bar: BarData,
        recent_bars: List[BarData],
        greeks: GreeksState
    ) -> StructuralSignal:
        nr_state = self.check_nr_compression(recent_bars)
        ah, al = self.evaluate_auction_levels(recent_bars)

        trigger = "NO_TRADE"
        invalidation = current_bar.close

        # Short Gamma Cascade (Spot breaks AL + Dealers Short Gamma + IV Skew expands)
        if current_bar.close < al and greeks.net_gex < 0 and greeks.iv_skew > 0:
            regime = "SHORT_GAMMA_EXPANSION"
            trigger = "MOMENTUM_SHORT"
            invalidation = ah
        elif nr_state in ["NR4", "NR7"] and greeks.net_gex > 0:
            regime = "LONG_GAMMA_PINNING"
            if current_bar.high >= ah and current_bar.close < ah:
                trigger = "MEAN_REVERSION_FADE"
                invalidation = current_bar.high + (current_bar.high - current_bar.low) * 0.5
            else:
                trigger = "PREPARE_BREAKOUT"
                invalidation = current_bar.low
        else:
            regime = "LONG_GAMMA_PINNING" if greeks.net_gex > 0 else "SHORT_GAMMA_EXPANSION"

        return StructuralSignal(
            timestamp=current_bar.timestamp,
            compression_state=nr_state,
            regime=regime,
            setup_trigger=trigger,
            invalidation_level=round(invalidation, 2)
        )
