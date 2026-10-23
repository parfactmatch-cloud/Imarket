# Jev Engine - Indian Equity Derivatives Edition

Production-grade Quantitative and Microstructure Execution Architecture for **NIFTY 50, FINNIFTY, and SENSEX**.

## Architecture Components:
1. `india_gex_engine.py`: Net Dealer Gamma, Call/Put Walls, Gamma Flip Level.
2. `india_order_flow_engine.py`: Cumulative Volume Delta (CVD) and Absorption Divergence.
3. `greeks_structural_engine.py`: Multi-Greek Alignment with NR4/NR7 Volatility Compression.
4. `gamma_blast_detector.py`: 0DTE 2:00 PM - 3:05 PM Tail-Risk Gamma Squeeze Detector.
5. `time_price_multi_layer_engine.py`: 1D -> 2H -> 30M -> 15M -> 5M Top-Down Confluence.
6. `lag_neutralized_engine.py`: Zero-Lag Rolling Window and Sub-bar Early Trigger.
7. `fyers_auth_automation.py`: Headless TOTP Daily Auth.
8. `engine_state_store.py`: SQLite WAL Persistent Fallback Store.
9. `execution_broker_interface.py`: Marketable Limit Router & Paper Simulator.
10. `telegram_remote_controller.py`: Remote Telemetry & `/panic` Kill-Switch.
11. `main_jev_orchestrator.py`: Master Entry Point.
