"""Tests for deriving option IV from market prices (zero-gamma fix).

On 17 Nov 2026 the live Fyers chain carried no usable IV, so every gamma
was 0 and net dealer GEX read 0.00 all day (run #51). These tests pin the
fix: IV is derived from each option's LTP when the chain omits it, with a
documented fallback, and a zero net GEX on a real chain is treated as
unavailable instead of a regime.

On 23 Nov 2026 a second zero-GEX day (run #56) showed the parser itself
was blind: Fyers returns FLAT rows, one per option leg, but the parser
only read nested ``call``/``put`` dicts per strike, so it saw OI 0, IV 0
and LTP 0 on all 63 rows. The flat-chain tests below pin that fix: the
index row is skipped, CE/PE legs group by strike, IV comes from the
nested ``greeks`` object (requested with ``greeks=1``), and the chain is
pinned to the engine's expiry via ``timestamp`` with a fallback.
"""
import math
from datetime import date, datetime, timedelta, timezone

from india_gex_engine import IndiaGEXEngine, OptionStrikeData
from market_data import _parse_chain_rows, compute_gex_snapshot

IST = timezone(timedelta(hours=5, minutes=30))


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


# ---------------------------------------------------------------- flat Fyers rows (run #56)
def _flat_rows(spot, tte, iv=0.13, greeks=True):
    """Fyers-style flat rows, as run #56 actually returned them: the index
    row first (strike -1, empty option_type), then one CE and one PE leg
    per strike, each with oi / ltp and, when greeks=True, a nested
    ``greeks`` object carrying iv."""
    rows = [{"strike_price": -1, "option_type": "", "symbol": "NSE:NIFTY50-INDEX",
             "fp": spot, "ltp": spot, "ask": spot, "bid": spot}]
    for strike, c_oi, p_oi in [(25300, 80000, 150000), (25350, 120000, 300000),
                               (25400, 200000, 500000), (25450, 350000, 400000),
                               (25500, 500000, 250000), (25550, 300000, 120000),
                               (25600, 150000, 80000)]:
        for opt, oi in (("CE", c_oi), ("PE", p_oi)):
            is_call = opt == "CE"
            ltp = _bs_price(spot, strike, tte, 0.13, is_call)
            row = {"strike_price": strike, "option_type": opt,
                   "symbol": f"NSE:NIFTY26N1725300{opt}",
                   "oi": oi, "oich": oi // 10, "oichp": 10.0,
                   "prev_oi": oi - oi // 10, "volume": oi // 5,
                   "ltp": ltp, "bid": max(0.05, round(ltp - 0.25, 2)),
                   "ask": round(ltp + 0.25, 2)}
            if greeks:
                row["greeks"] = {"iv": iv, "delta": 0.5, "gamma": 0.01,
                                 "theta": -1.0, "vega": 1.0}
            elif iv is not None:
                row["iv"] = iv
            rows.append(row)
    return rows


def test_flat_fyers_chain_parses_to_real_gex():
    """The run #56 failure, replayed: flat rows, greeks IV present."""
    class FlatChain:
        def optionchain(self, data):
            return {"s": "ok", "data": {"optionsChain": _flat_rows(25455.90, 0.002)}}

    snap = compute_gex_snapshot(FlatChain(), 25455.90, date(2026, 11, 24))
    assert snap is not None
    assert snap.net_gex != 0.0
    assert snap.call_wall == 25500  # highest call OI
    assert snap.put_wall == 25400   # highest put OI
    assert snap.pcr == 1.06         # put OI 1,800,000 / call OI 1,700,000
    assert snap.max_pain in (25300, 25350, 25400, 25450, 25500, 25550, 25600)
    assert snap.gamma_flip_level != 25455.90


def test_flat_chain_index_row_skipped_and_legs_grouped():
    parsed = _parse_chain_rows(_flat_rows(25455.90, 0.002))
    assert [d.strike for d in parsed] == [25300, 25350, 25400, 25450, 25500, 25550, 25600]
    atm = parsed[3]
    assert atm.call_oi == 350000 and atm.put_oi == 400000
    assert atm.call_iv == 0.13 and atm.put_iv == 0.13
    assert atm.call_ltp > 0 and atm.put_ltp > 0


def test_flat_chain_without_greeks_derives_iv_from_ltp():
    """No greeks object at all: IV is derived from each leg's LTP."""
    class FlatNoGreeks:
        def optionchain(self, data):
            return {"s": "ok", "data": {"optionsChain": _flat_rows(25455.90, 0.002, greeks=False)}}

    snap = compute_gex_snapshot(FlatNoGreeks(), 25455.90, date(2026, 11, 24))
    assert snap is not None
    assert snap.net_gex != 0.0


def test_flat_chain_requests_greeks_and_expiry_timestamp():
    """The fetch asks for greeks=1 and pins the chain to the expiry."""
    seen = []

    class RecordingChain:
        def optionchain(self, data):
            seen.append(dict(data))
            return {"s": "ok", "data": {"optionsChain": _flat_rows(25455.90, 0.002)}}

    snap = compute_gex_snapshot(RecordingChain(), 25455.90, date(2026, 11, 24))
    assert snap is not None
    assert seen, "optionchain was never called"
    first = seen[0]
    assert first["greeks"] == "1"
    expected_ts = str(int(datetime(2026, 11, 24, 15, 30, tzinfo=IST).timestamp()))
    assert first["timestamp"] == expected_ts


def test_flat_chain_timestamp_failure_falls_back_to_default_expiry():
    """A rejected timestamp must not leave the engine with no chain."""
    class FlakyChain:
        def optionchain(self, data):
            if "timestamp" in data:
                raise RuntimeError("Please provide valid inputs")
            return {"s": "ok", "data": {"optionsChain": _flat_rows(25455.90, 0.002)}}

    snap = compute_gex_snapshot(FlakyChain(), 25455.90, date(2026, 11, 24))
    assert snap is not None
    assert snap.net_gex != 0.0


# ------------------------------------------------- step 1: chain fields (25 Nov 2026)
def test_flat_chain_carries_oich_volume_bid_ask_and_greeks():
    """The fields Fyers already sends are parsed, not dropped."""
    parsed = _parse_chain_rows(_flat_rows(25455.90, 0.002))
    atm = parsed[3]  # 25450, nearest spot
    assert atm.call_oich == 35000 and atm.put_oich == 40000
    assert atm.call_prev_oi == 315000 and atm.put_prev_oi == 360000
    assert atm.call_volume == 70000 and atm.put_volume == 80000
    assert atm.call_bid > 0 and atm.call_ask > atm.call_bid
    assert atm.call_delta == 0.5 and atm.call_gamma == 0.01
    assert atm.call_theta == -1.0 and atm.call_vega == 1.0


def test_oi_change_pcr_and_atm_spreads():
    """OI-change PCR from oich; ATM spreads from bid/ask (0.50 each)."""
    class FlatChain:
        def optionchain(self, data):
            return {"s": "ok", "data": {"optionsChain": _flat_rows(25455.90, 0.002)}}

    snap = compute_gex_snapshot(FlatChain(), 25455.90, date(2026, 11, 24))
    assert snap is not None
    # put oich 180,000 / call oich 170,000
    assert snap.pcr_oi_change == 1.06
    assert snap.atm_call_spread == 0.5
    assert snap.atm_put_spread == 0.5


def test_fyers_gamma_used_when_present_and_bsm_otherwise():
    """Fyers' own gamma replaces the BSM estimate; without it BSM runs."""
    class FlatChain:
        def optionchain(self, data):
            return {"s": "ok", "data": {"optionsChain": _flat_rows(25455.90, 0.002)}}

    snap = compute_gex_snapshot(FlatChain(), 25455.90, date(2026, 11, 24))
    assert snap.fyers_gamma_legs == 14  # 7 strikes x 2 legs
    # gamma 0.01 on every leg: net = 0.01*75*spot*0.01*(put_oi-call_oi)
    # = 190.92 * 100,000 ≈ +19.09m — far outside any BSM-scale reading.
    assert snap.net_gex > 10_000_000

    class FlatNoGamma:
        def optionchain(self, data):
            rows = _flat_rows(25455.90, 0.002)
            for row in rows:
                row.get("greeks", {}).pop("gamma", None)
            return {"s": "ok", "data": {"optionsChain": rows}}

    snap_bsm = compute_gex_snapshot(FlatNoGamma(), 25455.90, date(2026, 11, 24))
    assert snap_bsm.fyers_gamma_legs == 0
    assert snap_bsm.net_gex != 0.0
    assert abs(snap_bsm.net_gex) < 5_000_000  # BSM-scale gamma


def test_india_vix_and_expiry_dates_carried_on_snapshot():
    """indiavixData and expiryData from the response land on the snapshot."""
    class FlatChain:
        def optionchain(self, data):
            return {"s": "ok", "data": {
                "optionsChain": _flat_rows(25455.90, 0.002),
                "indiavixData": {"ltp": 17.31},
                "expiryData": [{"date": "01-12-2026", "expiry": 1796104200},
                               {"date": "08-12-2026", "expiry": 1796709000}]}}

    snap = compute_gex_snapshot(FlatChain(), 25455.90, date(2026, 11, 24))
    assert snap is not None
    assert snap.india_vix == 17.31
    assert snap.expiry_dates == (date(2026, 12, 1), date(2026, 12, 8))


def test_spread_blocks_trade_only_when_measured_and_too_wide():
    from market_data import spread_blocks_trade
    assert spread_blocks_trade(0.0, 1.0) is False   # unknown spread: never blocks
    assert spread_blocks_trade(0.5, 1.0) is False
    assert spread_blocks_trade(1.0, 1.0) is False   # at the threshold: allowed
    assert spread_blocks_trade(1.5, 1.0) is True
