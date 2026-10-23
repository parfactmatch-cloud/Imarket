"""
india_gex_engine.py
Institutional Dealer Positioning & Gamma Exposure (GEX) Engine
Designed for NIFTY 50, FINNIFTY, and SENSEX.
"""

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import numpy as np


@dataclass
class OptionStrikeData:
    strike: float
    call_oi: int
    put_oi: int
    call_iv: float  # As decimal: e.g., 0.14 for 14%
    put_iv: float
    call_ltp: float
    put_ltp: float


@dataclass
class GEXSnapshot:
    underlying: str
    spot_price: float
    total_call_gex: float
    total_put_gex: float
    net_gex: float
    call_wall: float      # Strike with highest Call Open Interest / Absolute Call GEX
    put_wall: float       # Strike with highest Put Open Interest / Absolute Put GEX
    gamma_flip_level: float  # Estimated Spot level where Net GEX crosses zero
    market_regime: str    # "LONG_GAMMA_SUPPRESSION" or "SHORT_GAMMA_EXPANSION"
    zero_dte_squeeze_risk: bool


class IndiaGEXEngine:
    def __init__(self, risk_free_rate: float = 0.07):
        """
        risk_free_rate: 7.0% default (RBI 91-Day T-Bill benchmark)
        """
        self.r = risk_free_rate
        self.lot_sizes = {
            "NIFTY": 75,
            "FINNIFTY": 65,
            "BANKNIFTY": 30,
            "SENSEX": 20
        }

    def _norm_pdf(self, x: float) -> float:
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

    def calculate_bs_gamma(self, spot: float, strike: float, tte_years: float, iv: float) -> float:
        """
        Calculates Black-Scholes Gamma for a single option.
        """
        if spot <= 0 or strike <= 0 or tte_years <= 0 or iv <= 0.001:
            return 0.0

        d1 = (math.log(spot / strike) + (self.r + 0.5 * iv ** 2) * tte_years) / (iv * math.sqrt(tte_years))
        gamma = self._norm_pdf(d1) / (spot * iv * math.sqrt(tte_years))
        return gamma

    def process_chain(
        self,
        underlying: str,
        spot_price: float,
        tte_years: float,
        chain: List[OptionStrikeData]
    ) -> GEXSnapshot:
        lot_size = self.lot_sizes.get(underlying.upper(), 75)

        strikes = []
        call_gex_per_strike = []
        put_gex_per_strike = []
        net_gex_per_strike = []
        call_ois = []
        put_ois = []

        for data in chain:
            c_gamma = self.calculate_bs_gamma(spot_price, data.strike, tte_years, data.call_iv)
            p_gamma = self.calculate_bs_gamma(spot_price, data.strike, tte_years, data.put_iv)

            # Cash-Normalized Dealer Gamma (1% move impact)
            # Call side: Retail Buy -> Dealer Short Gamma (-1)
            # Put side: Retail Buy -> Dealer Long Gamma (+1)
            call_gex = -(c_gamma * data.call_oi * lot_size * spot_price * 0.01)
            put_gex = +(p_gamma * data.put_oi * lot_size * spot_price * 0.01)
            net_gex = call_gex + put_gex

            strikes.append(data.strike)
            call_gex_per_strike.append(call_gex)
            put_gex_per_strike.append(put_gex)
            net_gex_per_strike.append(net_gex)
            call_ois.append(data.call_oi)
            put_ois.append(data.put_oi)

        total_call_gex = float(np.sum(call_gex_per_strike))
        total_put_gex = float(np.sum(put_gex_per_strike))
        aggregate_net_gex = total_call_gex + total_put_gex

        call_wall = strikes[int(np.argmax(call_ois))] if call_ois else spot_price
        put_wall = strikes[int(np.argmax(put_ois))] if put_ois else spot_price

        gamma_flip = self._find_gamma_flip(strikes, net_gex_per_strike, spot_price)
        regime = "LONG_GAMMA_SUPPRESSION" if aggregate_net_gex > 0 else "SHORT_GAMMA_EXPANSION"

        hours_to_expiry = tte_years * 252 * 6.25
        dist_to_call_wall = abs(spot_price - call_wall) / spot_price if spot_price > 0 else 0
        dist_to_put_wall = abs(spot_price - put_wall) / spot_price if spot_price > 0 else 0
        is_0dte_squeeze = (hours_to_expiry <= 2.5) and (dist_to_call_wall < 0.0035 or dist_to_put_wall < 0.0035)

        return GEXSnapshot(
            underlying=underlying,
            spot_price=spot_price,
            total_call_gex=round(total_call_gex, 2),
            total_put_gex=round(total_put_gex, 2),
            net_gex=round(aggregate_net_gex, 2),
            call_wall=call_wall,
            put_wall=put_wall,
            gamma_flip_level=round(gamma_flip, 2),
            market_regime=regime,
            zero_dte_squeeze_risk=is_0dte_squeeze
        )

    def _find_gamma_flip(self, strikes: List[float], net_gex: List[float], spot: float) -> float:
        if not net_gex or not strikes:
            return spot
        arr_gex = np.array(net_gex)
        arr_strikes = np.array(strikes)

        zero_crossings = np.where(np.diff(np.sign(arr_gex)))[0]
        if len(zero_crossings) == 0:
            return spot

        closest_crossing = zero_crossings[np.argmin(np.abs(arr_strikes[zero_crossings] - spot))]
        s1, s2 = arr_strikes[closest_crossing], arr_strikes[closest_crossing + 1]
        g1, g2 = arr_gex[closest_crossing], arr_gex[closest_crossing + 1]

        if g2 == g1:
            return float(s1)
        return float(s1 - g1 * ((s2 - s1) / (g2 - g1)))
