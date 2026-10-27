"""
main_jev_orchestrator.py
Production Entry Point linking Ingestion, Greeks, Multi-Timeframe, and Execution.
Updated to dynamically read GitHub Secrets & Environment Variables.
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


def start_system():
    print("=" * 60)
    print(">>> Jev Engine (Indian Equity Derivatives Edition) Starting...")
    print("=" * 60)

    # 1. Fetch Credentials securely from Environment Variables / GitHub Secrets
    fyers_client_id = os.environ.get("FYERS_APP_ID", "")
    fyers_secret_key = os.environ.get("FYERS_SECRET_KEY", "")
    fyers_id = os.environ.get("FYERS_ID", "")
    fyers_pin = os.environ.get("FYERS_PIN", "")
    fyers_totp_key = os.environ.get("FYERS_TOTP_KEY", "")

    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    # Optional: Execution Mode toggle via environment (Default: True for Paper Mode)
    paper_mode_env = os.environ.get("PAPER_MODE", "True").lower() in ("true", "1", "yes")

    # 2. Basic Configuration Validation
    if not telegram_bot_token or not telegram_chat_id:
        print("[WARNING] Telegram credentials not fully set in environment. Alerts might not be delivered.")

    # 3. Initialize State Store
    state_store = EngineStateStore("engine_state.db")

    # 4. Headless Daily Authentication Setup
    auth_mgr = FyersAuthManager(
        client_id=fyers_client_id,
        secret_key=fyers_secret_key,
        redirect_uri="https://trade.fyers.in/api-login/redirect-uri/index.html",
        fyers_id=fyers_id,
        pin=fyers_pin,
        totp_key=fyers_totp_key
    )

    # Note: When paper testing without live Fyers API connection, fallback safely
    fyers_client = None
    if not paper_mode_env and fyers_client_id and fyers_totp_key:
        try:
            token = auth_mgr.get_valid_token()
            print("[Auth] Fyers token acquired successfully.")
        except Exception as e:
            print(f"[Auth Error] Failed to generate Fyers token: {e}")

    # 5. Execution Router
    router = ExecutionRouter(fyers_client=fyers_client, state_store=state_store, paper_mode=paper_mode_env)

    # 6. Telegram Controller Setup
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

    # 7. Core Run-Loop with Telemetry Baseline
    engine = LagNeutralizedEngine(pdh=23250.0, pdl=23050.0, pdc=23140.0)

    print(f"[Ready] Engine initialized ({mode_label}). Monitoring Indian Derivatives...")
    try:
        # For scheduled GitHub Actions, run a fixed batch of cycles
        # In a dedicated 24/7 server/VPS, change to: while True
        iteration = 0
        max_iterations = int(os.environ.get("MAX_ITERATIONS", "10"))

        while iteration < max_iterations:
            if state_store.get_system_flag("SYSTEM_PAUSED", "0") == "1":
                time.sleep(2)
                continue

            # In production, replace with real WebSocket live tick feed
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
