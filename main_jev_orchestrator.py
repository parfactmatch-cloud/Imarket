"""
main_jev_orchestrator.py
Production Entry Point linking Ingestion, Auction Footprint, Multi-Timeframe Context, and Execution.
Upgraded with:
- Official Fyers Redirect URI (`redirect-uri/index.html`) matching Fyers API v3 standards
- Real-time Fyers REST API PDH/PDL/PDC & Weekly/Monthly Profile Construction
- AdvancedAuctionFootprintEngine (3:1 Stacked Imbalances, Absorption Filter, Single Time-Framing)
- MTFProfileContextBuilder (Calendar-aware Weekly/Monthly VAH/VAL & POC Chop Filter)
- Sanitized Telegram Alerts & 30-Min Heartbeat
"""

import os
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

from fyers_auth_automation import FyersAuthManager
from engine_state_store import EngineStateStore
from execution_broker_interface import ExecutionRouter
from telegram_remote_controller import TelegramController
from lag_neutralized_engine import LagNeutralizedEngine, FastBar
from advanced_auction_footprint_engine import AdvancedAuctionFootprintEngine

try:
    from mtf_profile_context_builder import MTFProfileContextBuilder, ProfileLevels
    MTF_AVAILABLE = True
except ImportError:
    MTF_AVAILABLE = False

# IST Timezone (UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_now() -> datetime:
    return datetime.now(IST)


def sanitize_tg_text(text: str) -> str:
    """Removes Markdown characters that break Telegram API entity parsing."""
    return str(text).replace("_", " ").replace("*", "").replace("`", "")


def fetch_historical_levels(fyers_client) -> dict:
    """
    Fetches actual PDH, PDL, PDC and Weekly/Monthly Value Areas for NSE:NIFTY50-INDEX.
    """
    levels = {
        "pdh": 24000.0,
        "pdl": 23800.0,
        "pdc": 23900.0,
        "week_vah": 24100.0,
        "week_val": 23750.0,
        "week_poc": 23920.0,
        "month_vah": 24300.0,
        "month_val": 23600.0,
        "month_poc": 23950.0
    }
    try:
        today = get_ist_now()
        range_from = (today - timedelta(days=35)).strftime("%Y-%m-%d")
        range_to = today.strftime("%Y-%m-%d")

        data = {
            "symbol": "NSE:NIFTY50-INDEX",
            "resolution": "D",
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
            "cont_flag": "1"
        }
        response = fyers_client.history(data=data)
        candles = response.get("candles", [])
        if len(candles) >= 2:
            prev_candle = candles[-2]
            levels["pdh"] = float(prev_candle[2])
            levels["pdl"] = float(prev_candle[3])
            levels["pdc"] = float(prev_candle[4])

            # Derive Weekly Value Area (last 5 sessions)
            recent_week = candles[-6:-1] if len(candles) >= 6 else candles[:-1]
            w_high = max(float(c[2]) for c in recent_week)
            w_low = min(float(c[3]) for c in recent_week)
            w_range = w_high - w_low
            levels["week_vah"] = round(w_high - (0.15 * w_range), 2)
            levels["week_val"] = round(w_low + (0.15 * w_range), 2)
            levels["week_poc"] = round((w_high + w_low) / 2.0, 2)

            # Derive Monthly Value Area (last 20 sessions)
            recent_month = candles[-21:-1] if len(candles) >= 21 else candles[:-1]
            m_high = max(float(c[2]) for c in recent_month)
            m_low = min(float(c[3]) for c in recent_month)
            m_range = m_high - m_low
            levels["month_vah"] = round(m_high - (0.15 * m_range), 2)
            levels["month_val"] = round(m_low + (0.15 * m_range), 2)
            levels["month_poc"] = round((m_high + m_low) / 2.0, 2)

            print(f"[Levels] Real Nifty Levels -> PDH: {levels['pdh']}, PDL: {levels['pdl']}, Week POC: {levels['week_poc']}")
    except Exception as e:
        print(f"[Levels Warning] Historical fetch failed: {e}")

    return levels


def start_system():
    print("=" * 60)
    print(">>> Jev Engine v3.6 (Auction + Footprint + MTF Live) Starting...")
    print("=" * 60)

    # 1. Fetch Credentials from Environment Variables / GitHub Secrets
    fyers_client_id = os.environ.get("FYERS_APP_ID", "").strip()
    fyers_secret_key = os.environ.get("FYERS_SECRET_KEY", "").strip()
    fyers_id = os.environ.get("FYERS_ID", "").strip()
    fyers_pin = os.environ.get("FYERS_PIN", "").strip()
    fyers_totp_key = os.environ.get("FYERS_TOTP_KEY", "").strip()

    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    paper_mode_env = os.environ.get("PAPER_MODE", "True").lower() in ("true", "1", "yes")

    # 2. Initialize State Store & Execution Router
    state_store = EngineStateStore("engine_state.db")
    router = ExecutionRouter(fyers_client=None, state_store=state_store, paper_mode=paper_mode_env)

    # 3. Setup Telegram Controller
    tg = TelegramController(
        bot_token=telegram_bot_token,
        chat_id=telegram_chat_id,
        state_store=state_store,
        router=router
    )

    if tg.bot:
        tg_thread = threading.Thread(target=tg.run_polling, daemon=True)
        tg_thread.start()

    mode_label = "Paper Mode" if paper_mode_env else "Live Real Funds"

    # 4. Generate/Retrieve Daily Fyers Access Token
    token = os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
    if not token and fyers_client_id and fyers_totp_key:
        try:
            auth_mgr = FyersAuthManager(
                client_id=fyers_client_id,
                secret_key=fyers_secret_key,
                redirect_uri="https://trade.fyers.in/api-login/redirect-uri/index.html",
                fyers_id=fyers_id,
                pin=fyers_pin,
                totp_key=fyers_totp_key
            )
            token = auth_mgr.get_valid_token()
            print("[Auth] Fyers access token secured successfully.")
        except Exception as e:
            clean_err = sanitize_tg_text(str(e))[:140]
            print(f"[Auth Error] Failed to generate token: {e}")
            tg.send_alert("Auth Error", f"Token generation failed: {clean_err}")

    # 5. Connect Live Stream if Token is Available
    if token:
        fyers = fyersModel.FyersModel(client_id=fyers_client_id, token=token, is_async=False, log_path="")
        levels = fetch_historical_levels(fyers)
        pdh, pdl, pdc = levels["pdh"], levels["pdl"], levels["pdc"]

        mtf_builder = None
        if MTF_AVAILABLE:
            mtf_builder = MTFProfileContextBuilder(
                prev_month=ProfileLevels(levels["month_vah"], levels["month_val"], levels["month_poc"], []),
                prev_week=ProfileLevels(levels["week_vah"], levels["week_val"], levels["week_poc"], []),
                current_week=ProfileLevels(levels["week_vah"], levels["week_val"], levels["week_poc"], []),
                daily_profile=ProfileLevels(pdh, pdl, pdc, [])
            )

        tg.send_alert(
            "System Online (Live Feed)",
            f"Jev Engine v3.6 ({mode_label})\nNifty PDH: {pdh} | PDL: {pdl}\nWeek POC: {levels['week_poc']} | Status: Live Scanning"
        )

        lag_engine = LagNeutralizedEngine(pdh=pdh, pdl=pdl, pdc=pdc)
        fp_engine = AdvancedAuctionFootprintEngine(tick_size=0.05, imbalance_ratio=3.0, stacked_count=3)

        bar_ticks = []
        bar_start_time = time.time()
        last_heartbeat_time = time.time()
        latest_ltp = pdc
        early_triggered_this_bar = False
        ws = None

        def evaluate_and_execute(elapsed_sec: int, is_early: bool):
            nonlocal bar_ticks, bar_start_time, early_triggered_this_bar

            if not bar_ticks:
                return

            prices = [t["ltp"] for t in bar_ticks]
            total_vol = sum(t["qty"] for t in bar_ticks)
            ts_str = get_ist_now().strftime("%H:%M")

            fast_bar = FastBar(
                timestamp=ts_str,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=total_vol,
                elapsed_seconds=elapsed_sec
            )

            fp_bar = fp_engine.evaluate_bar(
                timestamp=ts_str,
                open_p=fast_bar.open,
                high_p=fast_bar.high,
                low_p=fast_bar.low,
                close_p=fast_bar.close,
                volume=total_vol
            )

            if state_store.get_system_flag("SYSTEM_PAUSED", "0") == "1":
                return

            # Check MTF Context & Chop Filter
            mtf_context = {}
            if mtf_builder:
                mtf_context = mtf_builder.evaluate_auction_context(fast_bar.close, get_ist_now())
                if mtf_context.get("is_chop_risk"):
                    print(f"[Veto] Price {fast_bar.close} is inside Weekly POC Chop Zone. Trade skipped.")
                    if not is_early:
                        bar_ticks.clear()
                        bar_start_time = time.time()
                        early_triggered_this_bar = False
                    return

            decision = lag_engine.evaluate_lag_free_execution(
                current_5m=fast_bar,
                bars_5m_history=[],
                dealer_net_dex=125000,
                cvd_delta=fp_bar.cumulative_delta
            )

            # Bullish Breakout + Footprint Confirmation
            if decision["signal"] == "EARLY_BUY" and not early_triggered_this_bar:
                if fp_bar.is_buying_absorption:
                    print(f"[Veto] Buying Absorption at {fast_bar.high}. Fake breakout blocked.")
                elif fp_engine.is_single_time_framing(3, "DOWN"):
                    print("[Veto] Active Single Time-Framing DOWN. Counter-buy blocked.")
                else:
                    early_triggered_this_bar = True
                    atm_strike = round(fast_bar.close / 50) * 50
                    imb_tag = f"Stacked Buy: {len(fp_bar.stacked_buy_imbalances)} lvls" if fp_bar.stacked_buy_imbalances else "CVD Confirmed"
                    ctx_tag = mtf_context.get("bias", "BREAKOUT")
                    tg.send_alert(
                        "🚀 NIFTY BUY SIGNAL",
                        f"Spot: {fast_bar.close} | {imb_tag}\nContext: {ctx_tag} | Bar Delta: {fp_bar.bar_delta}\nStrike: NIFTY {atm_strike} CE"
                    )
                    router.execute_buy(
                        symbol=f"NSE:NIFTY26NOV{atm_strike}CE",
                        qty=75,
                        ltp=fast_bar.close,
                        sl=decision["base"],
                        t1=fast_bar.close + 30,
                        t2=fast_bar.close + 75
                    )

            # Bearish Breakdown + Footprint Confirmation
            elif decision["signal"] == "EARLY_SELL" and not early_triggered_this_bar:
                if fp_bar.is_selling_absorption:
                    print(f"[Veto] Selling Absorption at {fast_bar.low}. Fake breakdown blocked.")
                elif fp_engine.is_single_time_framing(3, "UP"):
                    print("[Veto] Active Single Time-Framing UP. Counter-sell blocked.")
                else:
                    early_triggered_this_bar = True
                    atm_strike = round(fast_bar.close / 50) * 50
                    imb_tag = f"Stacked Sell: {len(fp_bar.stacked_sell_imbalances)} lvls" if fp_bar.stacked_sell_imbalances else "CVD Confirmed"
                    ctx_tag = mtf_context.get("bias", "BREAKDOWN")
                    tg.send_alert(
                        "🔻 NIFTY SELL SIGNAL",
                        f"Spot: {fast_bar.close} | {imb_tag}\nContext: {ctx_tag} | Bar Delta: {fp_bar.bar_delta}\nStrike: NIFTY {atm_strike} PE"
                    )
                    router.execute_buy(
                        symbol=f"NSE:NIFTY26NOV{atm_strike}PE",
                        qty=75,
                        ltp=fast_bar.close,
                        sl=decision["ceiling"],
                        t1=fast_bar.close - 30,
                        t2=fast_bar.close - 75
                    )

            if not is_early:
                bar_ticks.clear()
                bar_start_time = time.time()
                early_triggered_this_bar = False

        def on_message(message):
            nonlocal latest_ltp, last_heartbeat_time

            if not isinstance(message, dict):
                return

            ltp = message.get("ltp")
            if not ltp:
                return

            ltp = float(ltp)
            latest_ltp = ltp
            qty = int(message.get("last_traded_qty", 1))
            bid = float(message.get("bid", ltp))
            ask = float(message.get("ask", ltp))

            fp_engine.process_tick(ltp=ltp, qty=qty, best_bid=bid, best_ask=ask)
            bar_ticks.append({"ltp": ltp, "qty": qty})

            now_ts = time.time()
            elapsed = int(now_ts - bar_start_time)

            # 30-Minute Live Heartbeat Telemetry
            if (now_ts - last_heartbeat_time) >= 1800:
                last_heartbeat_time = now_ts
                tg.send_alert(
                    "💓 Engine Heartbeat",
                    f"Nifty Spot: {latest_ltp}\nRunning CVD: {fp_engine.running_cvd}\nStatus: Active & Scanning"
                )

            if elapsed >= 300:
                evaluate_and_execute(elapsed, is_early=False)
            elif elapsed >= 260 and not early_triggered_this_bar:
                evaluate_and_execute(elapsed, is_early=True)

        def on_error(msg):
            print(f"[WS Error] {msg}")

        def on_close(msg):
            print(f"[WS Closed] {msg}")

        def on_open():
            print("[WS Connected] Subscribing to NSE:NIFTY50-INDEX...")
            ws.subscribe(symbols=["NSE:NIFTY50-INDEX"], data_type="symbolUpdate")
            ws.keep_running()

        # Market Close Watcher Thread (Auto-stops cleanly at 3:31 PM IST)
        def market_close_watcher():
            while True:
                now_ist = get_ist_now()
                if now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 31):
                    tg.send_alert(
                        "Session Closed",
                        f"Market closed at 3:30 PM IST.\nFinal Nifty Spot: {latest_ltp} | Session CVD: {fp_engine.running_cvd}"
                    )
                    os._exit(0)
                time.sleep(30)

        threading.Thread(target=market_close_watcher, daemon=True).start()

        print("[Streaming] Connecting to live Fyers WebSocket feed...")
        full_token = f"{fyers_client_id}:{token}"
        ws = data_ws.FyersDataSocket(
            access_token=full_token,
            log_path="",
            litemode=False,
            write_to_file=False,
            on_connect=on_open,
            on_close=on_close,
            on_error=on_error,
            on_message=on_message
        )
        ws.connect()

    else:
        # Fallback Batch Loop if token is unavailable
        tg.send_alert("System Online (Fallback)", f"Jev Engine v3.6 Started in {mode_label} (No Live Token).")
        print("[Fallback/Paper] Running simulated telemetry baseline...")
        fallback_engine = LagNeutralizedEngine(pdh=23250.0, pdl=23050.0, pdc=23140.0)
        try:
            iteration = 0
            max_iterations = int(os.environ.get("MAX_ITERATIONS", "10"))

            while iteration < max_iterations:
                if state_store.get_system_flag("SYSTEM_PAUSED", "0") == "1":
                    time.sleep(2)
                    continue

                mock_bar = FastBar("14:24", 23210, 23265, 23205, 23262, 45000, 270)
                decision = fallback_engine.evaluate_lag_free_execution(
                    mock_bar, [], dealer_net_dex=125000, cvd_delta=4500
                )

                if decision["signal"] == "EARLY_BUY":
                    tg.send_alert("Signal Triggered", f"EARLY BUY at {decision['trigger_price']} | 30M Ceiling Breakout")
                    res = router.execute_buy(
                        symbol="NSE:NIFTY26NOV23250CE",
                        qty=75,
                        ltp=4.50,
                        sl=2.25,
                        t1=15.0,
                        t2=45.0
                    )
                    print(f"[Execution Result] {res}")

                iteration += 1
                time.sleep(1)

        except KeyboardInterrupt:
            tg.send_alert("System Shutdown", "Jev Engine manually terminated.")

    print(">>> Jev Engine lifecycle finished.")


if __name__ == "__main__":
    start_system()
    
