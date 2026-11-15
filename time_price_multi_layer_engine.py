"""
time_price_multi_layer_engine.py
Top-Down Multi-Timeframe (1D -> 2H -> 30M -> 15M -> 5M) Execution Engine.

NOTE: NOT wired into the live path. The orchestrator's live signal
comes from LagNeutralizedEngine + AdvancedAuctionFootprintEngine.
Kept as a standalone library module; see README "What is not in the
live path".
"""

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class OHLCV:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class TradeSignal:
    timestamp: str
    action: str              # "BUY", "SELL", "HOLD"
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    timeframe_confluence: str


class MultiLayerTimePriceEngine:
    def __init__(self, underlying: str = "NIFTY"):
        self.underlying = underlying

    def process_layer1_daily(self, prev_day_bar: OHLCV, today_open: float) -> Dict[str, any]:
        daily_bias = "BULLISH" if today_open >= prev_day_bar.close else "BEARISH"
        return {
            "pdh": prev_day_bar.high,
            "pdl": prev_day_bar.low,
            "pdc": prev_day_bar.close,
            "bias": daily_bias
        }

    def process_layer2_trend(self, bars_2h: List[OHLCV]) -> str:
        if len(bars_2h) < 3:
            return "NEUTRAL"
        is_uptrend = (bars_2h[-1].close > bars_2h[-2].close > bars_2h[-3].close)
        is_downtrend = (bars_2h[-1].close < bars_2h[-2].close < bars_2h[-3].close)
        return "UPTREND" if is_uptrend else ("DOWNTREND" if is_downtrend else "CHOPPY")

    def process_layer3_boundaries(self, bars_30m: List[OHLCV]) -> Dict[str, float]:
        recent = bars_30m[-4:] if len(bars_30m) >= 4 else bars_30m
        ceiling = max(b.high for b in recent) if recent else 0.0
        base = min(b.low for b in recent) if recent else 0.0
        return {"ceiling": ceiling, "base": base, "spread": ceiling - base}

    def process_layer4_behavior(self, bars_15m: List[OHLCV]) -> str:
        if len(bars_15m) < 4:
            return "NORMAL"
        ranges = [b.high - b.low for b in bars_15m[-4:]]
        if ranges[-1] == min(ranges):
            return "COMPRESSION_NR4"

        last_bar = bars_15m[-1]
        body = abs(last_bar.close - last_bar.open)
        upper_wick = last_bar.high - max(last_bar.open, last_bar.close)
        lower_wick = min(last_bar.open, last_bar.close) - last_bar.low

        if lower_wick > body * 1.5:
            return "BULLISH_REJECTION"
        if upper_wick > body * 1.5:
            return "BEARISH_REJECTION"
        return "MOMENTUM"

    def evaluate_5m_execution(
        self,
        current_5m: OHLCV,
        layer1: Dict[str, any],
        trend_2h: str,
        layer3: Dict[str, float],
        behavior_15m: str,
        atr_14: float
    ) -> TradeSignal:
        ceiling = layer3["ceiling"]
        base = layer3["base"]
        bias_1d = layer1["bias"]

        # Long Trigger
        if (
            bias_1d == "BULLISH"
            and trend_2h in ["UPTREND", "CHOPPY"]
            and current_5m.close > ceiling
            and behavior_15m in ["COMPRESSION_NR4", "MOMENTUM", "BULLISH_REJECTION"]
        ):
            sl = base if (current_5m.close - base) <= (2 * atr_14) else (current_5m.close - 1.25 * atr_14)
            risk = current_5m.close - sl
            return TradeSignal(
                timestamp=current_5m.timestamp,
                action="BUY",
                entry_price=current_5m.close,
                stop_loss=round(sl, 2),
                target_1=round(current_5m.close + (risk * 1.5), 2),
                target_2=round(current_5m.close + (risk * 3.0), 2),
                timeframe_confluence="1D_BULL + 2H_UP + 30M_CEILING_BREAK + 15M_EXPANSION"
            )

        # Short Trigger
        if (
            bias_1d == "BEARISH"
            and trend_2h in ["DOWNTREND", "CHOPPY"]
            and current_5m.close < base
            and behavior_15m in ["COMPRESSION_NR4", "MOMENTUM", "BEARISH_REJECTION"]
        ):
            sl = ceiling if (ceiling - current_5m.close) <= (2 * atr_14) else (current_5m.close + 1.25 * atr_14)
            risk = sl - current_5m.close
            return TradeSignal(
                timestamp=current_5m.timestamp,
                action="SELL",
                entry_price=current_5m.close,
                stop_loss=round(sl, 2),
                target_1=round(current_5m.close - (risk * 1.5), 2),
                target_2=round(current_5m.close - (risk * 3.0), 2),
                timeframe_confluence="1D_BEAR + 2H_DOWN + 30M_BASE_BREAKDOWN + 15M_EXPANSION"
            )

        return TradeSignal(
            timestamp=current_5m.timestamp,
            action="HOLD",
            entry_price=0.0,
            stop_loss=0.0,
            target_1=0.0,
            target_2=0.0,
            timeframe_confluence="CONFLUENCE_NOT_MET"
        )
