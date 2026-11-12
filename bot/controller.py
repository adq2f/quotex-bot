"""controller.py  --  BotController: lifecycle + persistence + keepalive."""
import os
import threading
import time

import requests

from .logger import log


class BotController:
    KEEPALIVE_HOURS = 1.0
    PING_INTERVAL = 300

    def __init__(self, bot):
        self.bot = bot
        self._thread = None
        self._active_sessions = set()
        self._lock = threading.Lock()
        self.bot.pause_event.set()
        self.bot.stop_event.clear()

        desired = self.bot.store.kv_get("bot_desired_running", False)
        if desired:
            self.bot.pause_event.clear()
            log("bot_resumed_from_persisted_state")
        else:
            log("bot_started_paused")

        self._keepalive_stop = threading.Event()
        self._start_keepalive()

    def start_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=self.bot.run_forever,
                                            daemon=True, name="bot-loop")
            self._thread.start()
            log("bot_thread_started")
            return True

    def on_login(self, session_id: str, username: str = ""):
        with self._lock:
            self._active_sessions.add(session_id)
            self._resume_locked(reason=f"login:{username}")

    def on_logout(self, session_id: str, username: str = ""):
        with self._lock:
            self._active_sessions.discard(session_id)
            if not self._active_sessions:
                self._pause_locked(reason=f"logout:{username}")

    def resume(self, reason: str = "manual"):
        with self._lock:
            self._resume_locked(reason=reason)

    def pause(self, reason: str = "manual"):
        with self._lock:
            self._pause_locked(reason=reason)

    def _resume_locked(self, reason: str):
        self.bot.pause_event.clear()
        self.bot.store.kv_set("bot_desired_running", True)
        log("bot_resumed", reason=reason, active=len(self._active_sessions))

    def _pause_locked(self, reason: str):
        self.bot.pause_event.set()
        self.bot.store.kv_set("bot_desired_running", False)
        log("bot_paused", reason=reason, active=len(self._active_sessions))

    def touch_keepalive(self, hours: float = None):
        hours = hours or self.KEEPALIVE_HOURS
        until = time.time() + hours * 3600
        self.bot.store.kv_set("keepalive_until", until)

    def _start_keepalive(self):
        threading.Thread(target=self._keepalive_loop,
                         daemon=True, name="keepalive").start()

    def _keepalive_loop(self):
        while not self._keepalive_stop.is_set():
            try:
                self._keepalive_stop.wait(self.PING_INTERVAL)
                if self._keepalive_stop.is_set(): break
                until = self.bot.store.kv_get("keepalive_until", 0)
                if time.time() < until:
                    self._ping_self()
            except Exception as e:
                log("keepalive_error", error=str(e)[:200])

    def _ping_self(self):
        url = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL") or "").rstrip("/")
        if not url: return
        try:
            requests.get(f"{url}/health", timeout=30)
            log("keepalive_ping", url=url)
        except Exception as e:
            log("keepalive_ping_failed", error=str(e)[:200])

    def status(self) -> dict:
        until = self.bot.store.kv_get("keepalive_until", 0)
        remaining = max(0, int(until - time.time()))
        return {
            "started": bool(self._thread and self._thread.is_alive()),
            "paused": self.bot.pause_event.is_set(),
            "active_sessions": len(self._active_sessions),
            "keepalive_remaining_seconds": remaining,
        }

    def shutdown(self):
        self.bot.stop_event.set()
        self._keepalive_stop.set()
        log("bot_shutdown_requested")
