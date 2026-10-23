"""
main_jev_orchestrator.py
Production Entry Point linking Ingestion, Greeks, Multi-Timeframe, and Execution.
"""

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

    # 1. Initialize State Store
    state_store = EngineStateStore("engine_state.db")

    # 2. Authentication
    auth_mgr = FyersAuthManager(
        client_id="YOUR_FYERS_APP_ID",
        secret_key="YOUR_FYERS_SECRET_KEY",
        redirect_uri="https://trade.fyers.in/api-login/redirect-uri/index.html",
        fyers_id="YOUR_FYERS_ID",
        pin="YOUR_PIN",
        totp_key="YOUR_TOTP_KEY"
    )

    # 3. Router (Default: Paper Mode True)
    router = ExecutionRouter(fyers_client=None, state_store=state_store, paper_mode=True)

    # 4. Telegram Controller
    tg = TelegramController(
        bot_token="YOUR_TELEGRAM_BOT_TOKEN",
        chat_id="YOUR_TELEGRAM_CHAT_ID",
        state_store=state_store,
        router=router
    )

    if tg.bot:
        tg_thread = threading.Thread(target=tg.run_polling, daemon=True)
        tg_thread.start()

    tg.send_alert("System Online", "Jev Engine v3.0 (Indian Derivatives) Started in Paper Mode.")

    # 5. Core Run-Loop with Mock Telemetry Baseline
    engine = LagNeutralizedEngine(pdh=23250.0, pdl=23050.0, pdc=23140.0)

    print("[Ready] Engine initialized. Monitoring Indian Derivatives...")
    try:
        iteration = 0
        while iteration < 5:  # Demo loop count, run indefinitely in production
            if state_store.get_system_flag("SYSTEM_PAUSED", "0") == "1":
                time.sleep(2)
                continue

            # In production, replace with real WebSocket tick feed
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
