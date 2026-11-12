"""controller.py  --  BotController: bot thread lifecycle + pause/resume on login/logout."""
import threading
from .logger import log


class BotController:
    def __init__(self, bot):
        self.bot = bot
        self._thread = None
        self._active_sessions = set()
        self._lock = threading.Lock()
        self.bot.pause_event.set()
        self.bot.stop_event.clear()

    def start_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=self.bot.run_forever, daemon=True, name="bot-loop")
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
                self._pause_locked(reason=f"logout:{username} (last session)")

    def resume(self, reason: str = "manual"):
        with self._lock:
            self._resume_locked(reason=reason)

    def pause(self, reason: str = "manual"):
        with self._lock:
            self._pause_locked(reason=reason)

    def _resume_locked(self, reason: str):
        was_paused = self.bot.pause_event.is_set()
        self.bot.pause_event.clear()
        log("bot_resumed", reason=reason, active=len(self._active_sessions))
        if was_paused:
            try:
                self.bot.tg.message(f"▶️ Bot resumed ({reason}) — scanning for signals.")
            except Exception:
                pass

    def _pause_locked(self, reason: str):
        was_running = not self.bot.pause_event.is_set()
        self.bot.pause_event.set()
        log("bot_paused", reason=reason, active=len(self._active_sessions))
        if was_running:
            try:
                self.bot.tg.message(f"⏸ Bot paused ({reason}) — no new signals will be generated.")
            except Exception:
                pass

    def status(self) -> dict:
        return {
            "started": bool(self._thread and self._thread.is_alive()),
            "paused":  self.bot.pause_event.is_set(),
            "active_sessions": len(self._active_sessions),
        }

    def shutdown(self):
        self.bot.stop_event.set()
        log("bot_shutdown_requested")
