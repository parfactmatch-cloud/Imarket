"""
engine_state_store.py
Persistent Low-Latency In-Memory State Store with SQLite WAL Fallback.
"""

import sqlite3
import json
from dataclasses import dataclass, asdict
from typing import Dict


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


class EngineStateStore:
    def __init__(self, db_path: str = "engine_state.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_db()

    def _init_db(self):
        with self.conn:
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

    def save_position(self, pos: PositionState):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO positions (symbol, data) VALUES (?, ?);",
                (pos.symbol, json.dumps(asdict(pos)))
            )

    def load_open_positions(self) -> Dict[str, PositionState]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT symbol, data FROM positions WHERE json_extract(data, '$.status') != 'CLOSED';")
        rows = cursor.fetchall()
        positions = {}
        for sym, d_str in rows:
            d = json.loads(d_str)
            positions[sym] = PositionState(**d)
        return positions

    def set_system_flag(self, key: str, val: str):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO system_flags (key, val) VALUES (?, ?);", (key, val))

    def get_system_flag(self, key: str, default: str = "") -> str:
        cursor = self.conn.cursor()
        cursor.execute("SELECT val FROM system_flags WHERE key = ?;", (key,))
        row = cursor.fetchone()
        return row[0] if row else default
