"""
engine_state_store.py
Persistent Low-Latency In-Memory State Store with SQLite WAL Fallback.

Thread safety: one SQLite connection is shared between the main thread
and the Telegram polling thread, so every access is guarded by a
re-entrant lock. Call ``close()`` on graceful shutdown to checkpoint
the WAL and close the connection; never kill the process mid-write.

Also tracks per-day trading statistics (trades taken and realised
P&L) in a ``daily_stats`` table, which the risk controls in
ExecutionRouter read before allowing a new order.
"""

import logging
import sqlite3
import json
import threading
from dataclasses import dataclass, asdict
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    symbol: str
    qty: int
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    is_paper: bool
    status: str  # "OPEN", "PARTIAL_CLOSED", "CLOSED"
    # Set when target 1 is hit and the stop is moved to breakeven.
    sl_moved_to_breakeven: bool = False
    # Set when a live exit order has been placed but its fill is not
    # yet confirmed; the monitor must not send a duplicate sell.
    exit_order_id: str = ""


class EngineStateStore:
    def __init__(self, db_path: str = "engine_state.db"):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_db()

    def _init_db(self):
        with self._lock, self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS system_flags (
                    key TEXT PRIMARY KEY,
                    val TEXT NOT NULL
                );
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    trade_date TEXT PRIMARY KEY,
                    trades INTEGER NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0.0
                );
            """)

    def save_position(self, pos: PositionState):
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO positions (symbol, data) VALUES (?, ?);",
                (pos.symbol, json.dumps(asdict(pos)))
            )

    def load_open_positions(self) -> Dict[str, PositionState]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT symbol, data FROM positions WHERE json_extract(data, '$.status') != 'CLOSED';")
            rows = cursor.fetchall()
        positions = {}
        for sym, d_str in rows:
            d = json.loads(d_str)
            # Drop unknown keys so rows written by older versions still load.
            known = {f for f in PositionState.__dataclass_fields__}
            positions[sym] = PositionState(**{k: v for k, v in d.items() if k in known})
        return positions

    def set_system_flag(self, key: str, val: str):
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO system_flags (key, val) VALUES (?, ?);", (key, val))

    def get_system_flag(self, key: str, default: str = "") -> str:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT val FROM system_flags WHERE key = ?;", (key,))
            row = cursor.fetchone()
        return row[0] if row else default

    def record_trade(self, trade_date: str, realized_pnl: float = 0.0, trades: int = 1):
        """Record completed trades and realised P&L for one IST date."""
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO daily_stats (trade_date, trades, realized_pnl)
                   VALUES (?, ?, ?)
                   ON CONFLICT(trade_date) DO UPDATE SET
                     trades = trades + excluded.trades,
                     realized_pnl = realized_pnl + excluded.realized_pnl;""",
                (trade_date, trades, realized_pnl),
            )

    def get_daily_stats(self, trade_date: str) -> Dict[str, float]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT trades, realized_pnl FROM daily_stats WHERE trade_date = ?;",
                (trade_date,),
            )
            row = cursor.fetchone()
        if not row:
            return {"trades": 0, "realized_pnl": 0.0}
        return {"trades": int(row[0]), "realized_pnl": float(row[1])}

    def close(self):
        """Checkpoint the WAL and close the connection (graceful shutdown)."""
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.warning("WAL checkpoint failed during close: %s", exc)
            finally:
                self.conn.close()
