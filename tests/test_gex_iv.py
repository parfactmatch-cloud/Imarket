"""Tests for deriving option IV from market prices (zero-gamma fix).

On 17 Nov 2026 the live Fyers chain carried no usable IV, so every gamma
was 0 and net dealer GEX read 0.00 all day (run #51). These tests pin the
fix: IV is derived from each option's LTP when the chain omits it, with a
documented fallback, and a zero net GEX on a real chain is treated as
unavailable instead of a regime.
"""
import math
from datetime import date

from india_gex_engine import IndiaGEXEngine, OptionStrikeData
from market_data import compute_gex_snapshot


def _bs_price(spot, strike, tte, iv, is_call, r=0.07):
    """Independent Black-Scholes price (test-side, via math.erf)."""
    d1 = (math.log(spot / strike) + (r + 0.5 * iv ** 2) * tte) / (iv * math.sqrt(tte))
    d2 = d1 - iv * math.sqrt(tte)
    disc = math.exp(-r * tte)
    ncdf = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    if is_call:
        return spot * ncdf(d1) - strike * disc * ncdf(d2)
    return strike * disc * ncdf(-d2) - spot * ncdf(-d1)


def _chain(spot, tte, iv, ltp_iv=0.13):
    """A 7-strike chain around the spot, OIs like a real NIFTY chain."""
    strikes = [25300, 25350, 25400, 25450, 25500, 25550, 25600]
    call_oi = [80000, 120000, 200000, 350000, 500000, 300000, 150000]
    put_oi = [150000, 300000, 500000, 400000, 250000, 120000, 80000]
    rows = []
    for strike, c_oi, p_oi in zip(strikes, call_oi, put_oi):
        c_ltp = _bs_price(spot, strike, tte, ltp_iv, True) if ltp_iv else 0.0
        p_ltp = _bs_price(spot, strike, tte, ltp_iv, False) if ltp_iv else 0.0
        rows.append(OptionStrikeData(strike=float(strike), call_oi=c_oi, put_oi=p_oi,
                                     call_iv=iv, put_iv=iv,
                                     call_ltp=c_ltp, put_ltp=p_ltp))
    return rows


def test_iv_inversion_recovers_known_iv():
    eng = IndiaGEXEngine()
    for spot, strike, is_call in [(25455.0, 25450.0, True), (25455.0, 25450.0, False),
                                  (25455.0, 25600.0, True), (25455.0, 25300.0, False),
                                  (24000.0, 24000.0, True)]:
        price = _bs_price(spot, strike, 0.01, 0.13, is_call)
        recovered = eng.implied_vol_from_price(spot, strike, 0.01, price, is_call)
        assert recovered is not None
        assert abs(recovered - 0.13) < 0.005


def test_iv_inversion_returns_none_without_time_value():
    eng = IndiaGEXEngine()
    # Deep ITM call priced at intrinsic: no time value to invert.
    assert eng.implied_vol_from_price(25455.0, 25000.0, 0.01, 455.0, True) is None
    assert eng.implied_vol_from_price(25455.0, 25450.0, 0.01, 0.0, True) is None
    assert eng.implied_vol_from_price(25455.0, 25450.0, 0.01, None, True) is None


def test_missing_iv_with_ltp_gives_real_gex():
    # The 17 Nov 2026 failure, replayed: IV missing on every strike, LTPs present.
    eng = IndiaGEXEngine()
    spot, tte = 25455.90, 0.002
    with_iv = eng.process_chain("NIFTY", spot, tte, _chain(spot, tte, 0.13))
    without_iv = eng.process_chain("NIFTY", spot, tte, _chain(spot, tte, 0.0))
    assert with_iv.net_gex != 0.0
    assert without_iv.net_gex != 0.0
    # Derived from prices, so it lands within 2% of the chain-IV value.
    assert abs(without_iv.net_gex - with_iv.net_gex) / abs(with_iv.net_gex) < 0.02
    # A real flip level, not the spot echoing back.
    assert without_iv.gamma_flip_level != spot
    # Walls and PCR come from OI alone, so they are identical either way.
    assert without_iv.call_wall == with_iv.call_wall
    assert without_iv.pcr == with_iv.pcr


def test_missing_iv_and_ltp_uses_fallback():
    eng = IndiaGEXEngine()
    spot, tte = 25455.90, 0.002
    snap = eng.process_chain("NIFTY", spot, tte, _chain(spot, tte, 0.0, ltp_iv=None))
    assert snap.net_gex != 0.0


def test_resolve_iv_source_order():
    eng = IndiaGEXEngine()
    assert eng._resolve_iv(25455.0, 25450.0, 0.01, 0.14, 50.0, True)[1] == "chain"
    price = _bs_price(25455.0, 25450.0, 0.01, 0.13, True)
    assert eng._resolve_iv(25455.0, 25450.0, 0.01, 0.0, price, True)[1] == "ltp"
    assert eng._resolve_iv(25455.0, 25450.0, 0.01, 0.0, 0.0, True) == (0.13, "fallback")


def test_zero_gex_with_no_oi_is_unavailable():
    class ZeroOiChain:
        def optionchain(self, data):
            rows = [{"strike_price": s,
                     "call": {"oi": 0, "iv": 0, "ltp": 0},
                     "put": {"oi": 0, "iv": 0, "ltp": 0}}
                    for s in (23800, 23900, 24000, 24100, 24200)]
            return {"s": "ok", "data": {"optionsChain": rows}}

    assert compute_gex_snapshot(ZeroOiChain(), 24000.0, date(2026, 11, 26)) is None


def test_missing_iv_chain_via_snapshot_is_real():
    """End to end through the parser: IV absent, LTPs present, real GEX out."""
    class NoIvChain:
        def optionchain(self, data):
            spot, tte = 25455.90, 0.002
            rows = []
            for strike, c_oi, p_oi in [(25300, 80000, 150000), (25350, 120000, 300000),
                                       (25400, 200000, 500000), (25450, 350000, 400000),
                                       (25500, 500000, 250000), (25550, 300000, 120000),
                                       (25600, 150000, 80000)]:
                rows.append({"strike_price": strike,
                             "call": {"oi": c_oi,
                                      "ltp": _bs_price(spot, strike, tte, 0.13, True)},
                             "put": {"oi": p_oi,
                                     "ltp": _bs_price(spot, strike, tte, 0.13, False)}})
            return {"s": "ok", "data": {"optionsChain": rows}}

    snap = compute_gex_snapshot(NoIvChain(), 25455.90, date(2026, 11, 24))
    assert snap is not None
    assert snap.net_gex != 0.0
    assert snap.gamma_flip_level != 25455.90
