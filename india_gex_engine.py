"""
india_gex_engine.py
Institutional Dealer Positioning & Gamma Exposure (GEX) Engine
Designed for NIFTY 50, FINNIFTY, and SENSEX.
"""

import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class OptionStrikeData:
    strike: float
    call_oi: int
    put_oi: int
    call_iv: float  # As decimal: e.g., 0.14 for 14%
    put_iv: float
    call_ltp: float
    put_ltp: float
    # Fields Fyers already sends on every chain row (step 1, 25 Nov 2026):
    # OI change / previous OI / volume / bid / ask per leg, plus the full
    # Greeks when greeks=1 was requested. All default to 0 so older
    # callers (and the nested-shape tests) keep working; 0 means "not
    # carried by this feed", never a measured zero.
    call_oich: int = 0
    put_oich: int = 0
    call_oichp: float = 0.0
    put_oichp: float = 0.0
    call_prev_oi: int = 0
    put_prev_oi: int = 0
    call_volume: int = 0
    put_volume: int = 0
    call_bid: float = 0.0
    put_bid: float = 0.0
    call_ask: float = 0.0
    put_ask: float = 0.0
    call_delta: float = 0.0
    put_delta: float = 0.0
    call_gamma: float = 0.0
    put_gamma: float = 0.0
    call_theta: float = 0.0
    put_theta: float = 0.0
    call_vega: float = 0.0
    put_vega: float = 0.0


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
    max_pain: float = 0.0     # Strike minimising total option payout at expiry
    pcr: float = 0.0          # Total put OI / total call OI
    pcr_oi_change: float = 0.0  # Total put OI change / total call OI change
    atm_call_spread: float = 0.0  # ask-bid of the CE leg nearest spot (0 = unknown)
    atm_put_spread: float = 0.0   # ask-bid of the PE leg nearest spot (0 = unknown)
    india_vix: Optional[float] = None  # From the chain response, if carried
    fyers_gamma_legs: int = 0  # Legs whose gamma came from Fyers, not BSM
    expiry_dates: tuple = ()  # Expiry dates Fyers lists for this underlying
    strikes: tuple = ()  # Strikes on the fetched chain (for validation)


class IndiaGEXEngine:
    def __init__(self, risk_free_rate: float = 0.07, fallback_iv: float = 0.13):
        """
        risk_free_rate: 7.0% default (RBI 91-Day T-Bill benchmark)
        fallback_iv: IV used only when a chain row carries neither a usable
            IV nor a market price to derive one from (recent India VIX
            level, ~13%). A live VIX quote would replace it; it is never
            used silently -- see the IV summary logged by process_chain.
        """
        self.r = risk_free_rate
        self.fallback_iv = fallback_iv
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

    @staticmethod
    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _bs_price(self, spot: float, strike: float, tte_years: float, iv: float, is_call: bool) -> float:
        """Black-Scholes price of a European option (same conventions as gamma)."""
        if spot <= 0 or strike <= 0 or tte_years <= 0 or iv <= 0:
            return 0.0
        d1 = (math.log(spot / strike) + (self.r + 0.5 * iv ** 2) * tte_years) / (iv * math.sqrt(tte_years))
        d2 = d1 - iv * math.sqrt(tte_years)
        disc = math.exp(-self.r * tte_years)
        if is_call:
            return spot * self._norm_cdf(d1) - strike * disc * self._norm_cdf(d2)
        return strike * disc * self._norm_cdf(-d2) - spot * self._norm_cdf(-d1)

    def implied_vol_from_price(self, spot: float, strike: float, tte_years: float,
                               price: float, is_call: bool) -> Optional[float]:
        """IV implied by an option's market price, or None if it cannot be found.

        Bisection on IV: the Black-Scholes price rises monotonically with IV.
        Returns None when the price carries no time value (at/below intrinsic,
        or zero/missing), so the caller can fall back to the configured
        fallback IV instead of silently pricing gamma at zero.
        """
        if spot <= 0 or strike <= 0 or tte_years <= 0 or price is None or price <= 0:
            return None
        intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
        if price <= intrinsic:
            return None
        lo, hi = 0.001, 5.0
        if self._bs_price(spot, strike, tte_years, hi, is_call) < price:
            return None  # even 500% IV cannot reach this price
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if self._bs_price(spot, strike, tte_years, mid, is_call) < price:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    def _resolve_iv(self, spot: float, strike: float, tte_years: float,
                    iv: float, ltp: float, is_call: bool) -> Tuple[float, str]:
        """IV for one option, plus where it came from: "chain", "ltp" or
        "fallback".

        The chain's own IV when present; else the IV implied by its market
        price; else the configured fallback IV. The live Fyers chain on
        17 Nov 2026 carried no usable IV on any strike, which zeroed every
        gamma and the whole net GEX reading. Deriving IV from the price
        the chain does carry keeps gamma real.
        """
        if iv is not None and iv > 0.001:
            return iv, "chain"
        derived = self.implied_vol_from_price(spot, strike, tte_years, ltp, is_call)
        if derived is not None:
            return derived, "ltp"
        return self.fallback_iv, "fallback"

    def process_chain(
        self,
        underlying: str,
        spot_price: float,
        tte_years: float,
        chain: List[OptionStrikeData],
        india_vix: Optional[float] = None,
        expiry_dates=(),
    ) -> GEXSnapshot:
        lot_size = self.lot_sizes.get(underlying.upper(), 75)

        strikes = []
        call_gex_per_strike = []
        put_gex_per_strike = []
        net_gex_per_strike = []
        call_ois = []
        put_ois = []
        call_oichs = []
        put_oichs = []

        iv_sources = {"chain": 0, "ltp": 0, "fallback": 0}
        fyers_gamma_legs = 0

        for data in chain:
            call_iv, call_src = self._resolve_iv(spot_price, data.strike, tte_years,
                                                 data.call_iv, data.call_ltp, is_call=True)
            put_iv, put_src = self._resolve_iv(spot_price, data.strike, tte_years,
                                               data.put_iv, data.put_ltp, is_call=False)
            iv_sources[call_src] += 1
            iv_sources[put_src] += 1
            # Fyers' own gamma when the chain carries it (greeks=1);
            # BSM gamma otherwise. Fyers gamma is the exchange's own
            # model output for that leg, so it wins over our estimate.
            if data.call_gamma and data.call_gamma > 0:
                c_gamma = data.call_gamma
                fyers_gamma_legs += 1
            else:
                c_gamma = self.calculate_bs_gamma(spot_price, data.strike, tte_years, call_iv)
            if data.put_gamma and data.put_gamma > 0:
                p_gamma = data.put_gamma
                fyers_gamma_legs += 1
            else:
                p_gamma = self.calculate_bs_gamma(spot_price, data.strike, tte_years, put_iv)

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
            call_oichs.append(data.call_oich)
            put_oichs.append(data.put_oich)

        total_call_gex = float(np.sum(call_gex_per_strike))
        total_put_gex = float(np.sum(put_gex_per_strike))
        aggregate_net_gex = total_call_gex + total_put_gex

        if fyers_gamma_legs:
            logger.info("GEX gamma: %d/%d legs from Fyers greeks, %d BSM",
                        fyers_gamma_legs, len(chain) * 2,
                        len(chain) * 2 - fyers_gamma_legs)

        if iv_sources["chain"] < len(chain) * 2:
            # Any leg not from the chain means the feed is omitting IV --
            # say so once per snapshot instead of silently trusting it.
            logger.info(
                "GEX IV: %d/%d legs from chain, %d derived from LTP, "
                "%d on fallback IV %.2f",
                iv_sources["chain"], len(chain) * 2, iv_sources["ltp"],
                iv_sources["fallback"], self.fallback_iv)

        call_wall = strikes[int(np.argmax(call_ois))] if call_ois else spot_price
        put_wall = strikes[int(np.argmax(put_ois))] if put_ois else spot_price

        gamma_flip = self._find_gamma_flip(strikes, net_gex_per_strike, spot_price)
        regime = "LONG_GAMMA_SUPPRESSION" if aggregate_net_gex > 0 else "SHORT_GAMMA_EXPANSION"

        total_call_oi = float(sum(call_ois))
        total_put_oi = float(sum(put_ois))
        pcr = (total_put_oi / total_call_oi) if total_call_oi > 0 else 0.0
        max_pain = self._max_pain_strike(strikes, call_ois, put_ois)

        # OI-change PCR: fresh positioning, not the standing stock of OI.
        # Signed sums: a day when puts are being added and calls closed
        # reads very differently from the plain PCR. Zero when the chain
        # carries no OI change at all (or the call change nets to zero).
        total_call_oich = float(sum(call_oichs))
        total_put_oich = float(sum(put_oichs))
        pcr_oi_change = ((total_put_oich / total_call_oich)
                         if total_call_oich != 0 else 0.0)

        # ATM spreads (ask-bid on the strike nearest spot). 0.0 means
        # the chain carried no bid/ask -- unknown, not a zero spread.
        def _spread(bid: float, ask: float) -> float:
            return round(ask - bid, 2) if bid > 0 and ask > bid else 0.0

        atm_call_spread = atm_put_spread = 0.0
        if chain:
            atm_idx = min(range(len(chain)),
                          key=lambda i: abs(chain[i].strike - spot_price))
            atm = chain[atm_idx]
            atm_call_spread = _spread(atm.call_bid, atm.call_ask)
            atm_put_spread = _spread(atm.put_bid, atm.put_ask)

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
            zero_dte_squeeze_risk=is_0dte_squeeze,
            max_pain=round(max_pain, 2),
            pcr=round(pcr, 2),
            pcr_oi_change=round(pcr_oi_change, 2),
            atm_call_spread=atm_call_spread,
            atm_put_spread=atm_put_spread,
            india_vix=india_vix,
            fyers_gamma_legs=fyers_gamma_legs,
            expiry_dates=tuple(expiry_dates),
            strikes=tuple(strikes),
        )

    @staticmethod
    def _max_pain_strike(strikes: List[float], call_ois: List[int],
                         put_ois: List[int]) -> float:
        """Strike where the total intrinsic payout at expiry is smallest.

        For each candidate expiry price S (each listed strike), the payout
        is sum over calls of max(0, S-K) * call_oi plus sum over puts of
        max(0, K-S) * put_oi. The strike minimising it is max pain.
        """
        if not strikes:
            return 0.0
        best_strike, best_payout = strikes[0], None
        for s in strikes:
            payout = sum(max(0.0, s - k) * oi
                         for k, oi in zip(strikes, call_ois))
            payout += sum(max(0.0, k - s) * oi
                          for k, oi in zip(strikes, put_ois))
            if best_payout is None or payout < best_payout:
                best_strike, best_payout = s, payout
        return float(best_strike)

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
