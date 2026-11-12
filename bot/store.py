"""
store.py  --  persistence (SQLite / Turso Cloud).
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
            print("[Store] Connecting to Turso Cloud Database...")
            self.conn = libsql.connect(turso_url, auth_token=turso_token)
        else:
            print(f"[Store] Using local SQLite database: {path}")
            self.conn = sqlite3.connect(path, check_same_thread=False)

        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, asset TEXT,
                decision TEXT, score INTEGER, gate TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS signals (id TEXT PRIMARY KEY, ts TEXT, asset TEXT, direction TEXT, step INTEGER,
                parent_id TEXT, status TEXT, pnl REAL, expiry_time TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login TEXT
            );
            """)
            self.conn.commit()

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            r = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                              (key, json.dumps(value, default=str)))
            self.conn.commit()

    def log_decision(self, asset: str, decision: str, score: int, gate: str, payload: Dict) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO decisions(ts,asset,decision,score,gate,payload) VALUES(?,?,?,?,?,?)",
                              (datetime.now(timezone.utc).isoformat(), asset, decision, score, gate,
                               json.dumps(payload, default=str)))
            self.conn.commit()

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

    def list_signals(self, limit: int = 50) -> List[Dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, ts, asset, direction, step, parent_id, status, pnl, expiry_time, payload "
                "FROM signals ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r[9]) if r[9] else {}
            except Exception:
                payload = {}
            out.append({
                "id": r[0], "ts": r[1], "asset": r[2], "direction": r[3], "step": r[4],
                "parent_id": r[5], "status": r[6], "pnl": r[7], "expiry_time": r[8],
                "entry_price": payload.get("entry_price"),
                "stake": payload.get("stake"),
                "score": payload.get("score"),
                "payout_pct": payload.get("payout_pct"),
            })
        return out

    def user_count(self) -> int:
        with self.lock:
            r = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(r[0]) if r else 0

    def get_user(self, username: str) -> Optional[Dict]:
        with self.lock:
            r = self.conn.execute(
                "SELECT username, password_hash, created_at, last_login FROM users WHERE username=?",
                (username,),
            ).fetchone()
        if not r:
            return None
        return {"username": r[0], "password_hash": r[1], "created_at": r[2], "last_login": r[3]}

    def create_user(self, username: str, password_hash: str) -> bool:
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
                    (username, password_hash, datetime.now(timezone.utc).isoformat()),
                )
                self.conn.commit()
                return True
            except Exception:
                return False

    def update_last_login(self, username: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE users SET last_login=? WHERE username=?",
                (datetime.now(timezone.utc).isoformat(), username),
            )
            self.conn.commit()
