"""
lag_neutralized_engine.py
Sliding Rolling Window and Sub-bar (260-sec) Early Execution Engine.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class FastBar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    elapsed_seconds: int  # Seconds passed in the current 5M bar


class LagNeutralizedEngine:
    def __init__(self, pdh: float, pdl: float, pdc: float):
        self.pdh = pdh
        self.pdl = pdl
        self.pdc = pdc

    def get_layer1_bias(self, current_price: float) -> str:
        return "BULLISH" if current_price >= self.pdc else "BEARISH"

    def get_layer2_synthetic_trend(
        self, bars_5m: List[FastBar], dealer_net_dex: Optional[float]
    ) -> str:
        # Unknown dealer exposure (e.g. the option chain could not be
        # fetched) is NEUTRAL: it must never be replaced by a constant,
        # and it must never produce a signal.
        if dealer_net_dex is None:
            return "NEUTRAL"
        if len(bars_5m) < 12:
            return "NEUTRAL"

        rolling_close = [b.close for b in bars_5m[-12:]]
        price_slope = rolling_close[-1] - rolling_close[0]

        if price_slope > 0 and dealer_net_dex > 0:
            return "UPTREND"
        if price_slope < 0 and dealer_net_dex < 0:
            return "DOWNTREND"
        return "CHOPPY"

    def get_layer3_rolling_boundaries(self, bars_5m: List[FastBar]) -> Dict[str, float]:
        recent_6 = bars_5m[-6:] if len(bars_5m) >= 6 else bars_5m
        if not recent_6:
            return {"ceiling": 0.0, "base": 0.0}
        ceiling = max(b.high for b in recent_6)
        base = min(b.low for b in recent_6)
        return {"ceiling": ceiling, "base": base}

    def evaluate_lag_free_execution(
        self,
        current_5m: FastBar,
        bars_5m_history: List[FastBar],
        dealer_net_dex: Optional[float],
        cvd_delta: float
    ) -> Dict[str, any]:
        daily_bias = self.get_layer1_bias(current_5m.close)
        trend = self.get_layer2_synthetic_trend(bars_5m_history, dealer_net_dex)
        boundaries = self.get_layer3_rolling_boundaries(bars_5m_history)
        ceiling = boundaries["ceiling"]
        base = boundaries["base"]

        # Trigger on 260th second of 300 second candle if breakout confirmed
        is_sub_bar_mature = current_5m.elapsed_seconds >= 260
        signal = "HOLD"

        if (
            daily_bias == "BULLISH"
            and trend == "UPTREND"
            and current_5m.close > ceiling
            and cvd_delta > 0
            and is_sub_bar_mature
        ):
            signal = "EARLY_BUY"
        elif (
            daily_bias == "BEARISH"
            and trend == "DOWNTREND"
            and current_5m.close < base
            and cvd_delta < 0
            and is_sub_bar_mature
        ):
            signal = "EARLY_SELL"

        return {
            "signal": signal,
            "trigger_price": current_5m.close,
            "ceiling": ceiling,
            "base": base,
            "lag_status": "NEUTRALIZED"
        }
