"""
gamma_blast_detector.py
Zero-to-Hero (20x-50x Gamma Cascade) Trigger Detector for Indian 0DTE Expiry.
Active window: 2:00 PM - 3:08 PM IST (matches the code below).

NOTE: this detector is NOT wired into the live path (see README).
It is a standalone library module; the orchestrator does not call it.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class StrikeTelemetry:
    strike: float
    option_type: str        # 'CE' or 'PE'
    ltp: float              # e.g., 2.0 to 8.0
    oi: int
    delta_oi_15m: int       # Change in OI in last 15 mins
    iv: float
    calculated_gamma: float
    dealer_net_gex: float


@dataclass
class BlastSignal:
    triggered: bool
    underlying: str
    target_strike: float
    option_type: str
    entry_price: float
    estimated_stop_loss: float
    reason: str


class GammaBlastDetector:
    def __init__(self, underlying: str = "NIFTY"):
        self.underlying = underlying

    def scan_for_blast_setup(
        self,
        current_time_str: str,  # "HH:MM" e.g., "14:15"
        spot_price: float,
        cvd_slope_positive: bool,
        strike_data: StrikeTelemetry,
        wall_strike: float
    ) -> Optional[BlastSignal]:
        # 1. Time Window: 2:00 PM to 3:08 PM (before RMS square-off)
        hour, minute = map(int, current_time_str.split(":"))
        is_expiry_blast_window = (hour == 14 and minute >= 0) or (hour == 15 and minute <= 8)
        if not is_expiry_blast_window:
            return None

        # 2. Cheap Asymmetry Premium Range (Rs 2.0 - Rs 9.0)
        if not (2.0 <= strike_data.ltp <= 9.0):
            return None

        # 3. Distance to Strike (Within 0.35%)
        dist_pct = abs(spot_price - strike_data.strike) / spot_price
        if dist_pct > 0.0035:
            return None

        # 4. Major Wall & Dealers Short Gamma
        is_major_wall = (strike_data.strike == wall_strike)
        is_dealers_short_gamma = strike_data.dealer_net_gex < 0

        # 5. Short Covering Unwinding Started (Negative Delta OI)
        is_oi_unwinding = strike_data.delta_oi_15m < 0

        if is_major_wall and is_dealers_short_gamma and is_oi_unwinding and cvd_slope_positive:
            sl = max(0.50, strike_data.ltp * 0.50)
            return BlastSignal(
                triggered=True,
                underlying=self.underlying,
                target_strike=strike_data.strike,
                option_type=strike_data.option_type,
                entry_price=strike_data.ltp,
                estimated_stop_loss=round(sl, 2),
                reason="DEALER_SHORT_GAMMA_UNWINDING_CASCADE"
            )

        return None
