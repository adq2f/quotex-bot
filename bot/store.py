"""
store.py  --  persistence (SQLite / Turso Cloud). Section 10: every decision + signal outcome is logged for audit/backtest.
On Render Free tier (no persistent disk), set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN to persist data in Turso cloud.
If those env vars are missing, it falls back to local SQLite (for local testing).

FIX LOG v2.4:
  - Switched from 'libsql-experimental' to the stable 'libsql' package to fix Render Python 3.14 build errors.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Try to import libsql for Turso Cloud
try:
    import libsql
except ImportError:
    libsql = None


class Store:
    def __init__(self, path: str):
        self.lock = threading.Lock()
        turso_url = os.getenv("TURSO_DATABASE_URL")
        turso_token = os.getenv("TURSO_AUTH_TOKEN")

        if turso_url and turso_token and libsql:
            # Connect to Turso Cloud
            print("[Store] Connecting to Turso Cloud Database...")
            self.conn = libsql.connect(turso_url, auth_token=turso_token)
        else:
            # Fallback to Local SQLite (for local testing or if Turso vars are missing)
            print(f"[Store] Using local SQLite database: {path}")
            self.conn = sqlite3.connect(path, check_same_thread=False)

        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, asset TEXT,
                decision TEXT, score INTEGER, gate TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS signals (id TEXT PRIMARY KEY, ts TEXT, asset TEXT, direction TEXT, step INTEGER,
                parent_id TEXT, status TEXT, pnl REAL, expiry_time TEXT, payload TEXT);
            """)
            self.conn.commit()

    # kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            r = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(value, default=str)))
            self.conn.commit()

    # decisions
    def log_decision(self, asset: str, decision: str, score: int, gate: str, payload: Dict) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO decisions(ts,asset,decision,score,gate,payload) VALUES(?,?,?,?,?,?)",
                              (datetime.now(timezone.utc).isoformat(), asset, decision, score, gate, json.dumps(payload, default=str)))
            self.conn.commit()

    # signals
    def save_signal(self, sig: Dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO signals(id,ts,asset,direction,step,parent_id,status,pnl,expiry_time,payload) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, pnl=excluded.pnl, payload=excluded.payload",
                (sig["id"], str(sig["entry_time"]), sig["asset"], sig["direction"], sig["step"], sig.get("parent_id"),
                 sig["status"], sig.get("pnl", 0.0), str(sig["expiry_time"]), json.dumps(sig, default=str)))
            self.conn.commit()

    def load_pending(self) -> List[Dict]:
        with self.lock:
            rows = self.conn.execute("SELECT payload FROM signals WHERE status='PENDING'").fetchall()
        return [json.loads(r[0]) for r in rows]

    def signal_rows(self, since_iso: Optional[str] = None) -> List[Dict]:
        q, args = "SELECT step,status,pnl,asset,ts FROM signals WHERE status IN ('WIN','LOSS','DRAW')", ()
        if since_iso:
            q, args = q + " AND ts>=?", (since_iso,)
        with self.lock:
            rows = self.conn.execute(q, args).fetchall()
        return [dict(step=r[0], status=r[1], pnl=r[2], asset=r[3], ts=r[4]) for r in rows]
