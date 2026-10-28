"""
main_jev_orchestrator.py
Production Entry Point linking Ingestion, Greeks, Multi-Timeframe, and Execution.
Updated to support real-time WebSocket tick streaming via Fyers API v3.
"""

import os
import sys
import time
import threading
from fyers_auth_automation import FyersAuthManager
from engine_state_store import EngineStateStore
from execution_broker_interface import ExecutionRouter
from telegram_remote_controller import TelegramController
from lag_neutralized_engine import LagNeutralizedEngine, FastBar

# Attempt importing the live websocket streamer
try:
    from fyers_live_feed import FyersLiveStreamer
    STREAMER_AVAILABLE = True
except ImportError:
    STREAMER_AVAILABLE = False


def start_system():
    print("=" * 60)
    print(">>> Jev Engine (Indian Equity Derivatives Edition) Starting...")
    print("=" * 60)

    # 1. Credentials from Environment Variables / GitHub Secrets
    fyers_client_id = os.environ.get("FYERS_APP_ID", "")
    fyers_secret_key = os.environ.get("FYERS_SECRET_KEY", "")
    fyers_id = os.environ.get("FYERS_ID", "")
    fyers_pin = os.environ.get("FYERS_PIN", "")
    fyers_totp_key = os.environ.get("FYERS_TOTP_KEY", "")

    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

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
    tg.send_alert("System Online", f"Jev Engine v3.0 (Indian Derivatives) Started in {mode_label}.")

    # 4. Generate/Retrieve Daily Fyers Access Token
    token = ""
    if fyers_client_id and fyers_totp_key:
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
            print(f"[Auth Error] Failed to generate token: {e}")

    # 5. Core Multi-Timeframe Engine Setup
    engine = LagNeutralizedEngine(pdh=23250.0, pdl=23050.0, pdc=23140.0)

    # Callback executed when a 5M / Sub-bar threshold is crossed
    def handle_live_bar(fast_bar: FastBar, cumulative_cvd: int, is_early: bool):
        if state_store.get_system_flag("SYSTEM_PAUSED", "0") == "1":
            return

        decision = engine.evaluate_lag_free_execution(
            current_5m=fast_bar,
            bars_5m_history=[],
            dealer_net_dex=125000,
            cvd_delta=cumulative_cvd
        )

        if decision["signal"] == "EARLY_BUY":
            tg.send_alert("🚀 Live Signal Triggered", f"EARLY BUY at {decision['trigger_price']} | 30M Ceiling Breakout")
            res = router.execute_buy(
                symbol="NSE:NIFTY26OCT23250CE",
                qty=75,
                ltp=decision["trigger_price"],
                sl=decision["base"],
                t1=decision["trigger_price"] + 25,
                t2=decision["trigger_price"] + 60
            )
            print(f"[Execution Result] {res}")

        elif decision["signal"] == "EARLY_SELL":
            tg.send_alert("🔻 Live Signal Triggered", f"EARLY SELL at {decision['trigger_price']} | 30M Base Breakdown")
            res = router.execute_buy(
                symbol="NSE:NIFTY26OCT23250PE",
                qty=75,
                ltp=decision["trigger_price"],
                sl=decision["ceiling"],
                t1=decision["trigger_price"] + 25,
                t2=decision["trigger_price"] + 60
            )
            print(f"[Execution Result] {res}")

    # 6. Mode Execution Router
    # If live token is present and streamer file exists, use live WebSocket
    if token and STREAMER_AVAILABLE:
        print("[Streaming] Connecting to live Fyers WebSocket feed...")
        try:
            streamer = FyersLiveStreamer(
                access_token=token,
                client_id=fyers_client_id,
                on_bar_completed_callback=handle_live_bar
            )
            streamer.start_streaming()
        except Exception as e:
            print(f"[Streamer Error] {e}. Falling back to standard cycle.")

    else:
        # Fallback Batch Loop for Scheduled Actions / Testing
        print(f"[Fallback/Paper] Running simulated telemetry baseline...")
        try:
            iteration = 0
            max_iterations = int(os.environ.get("MAX_ITERATIONS", "10"))

            while iteration < max_iterations:
                if state_store.get_system_flag("SYSTEM_PAUSED", "0") == "1":
                    time.sleep(2)
                    continue

                mock_bar = FastBar("14:24", 23210, 23265, 23205, 23262, 45000, 270)
                decision = engine.evaluate_lag_free_execution(mock_bar, [], dealer_net_dex=125000, cvd_delta=4500)

                if decision["signal"] == "EARLY_BUY":
                    tg.send_alert("Signal Triggered", f"EARLY BUY at {decision['trigger_price']} | 30M Ceiling Breakout")
                    res = router.execute_buy(
                        symbol="NSE:NIFTY26OCT23250CE",
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
