"""telegram_out.py -- delivery via Telegram Bot API with verbose logging."""
import html
import time
import requests
from .logger import log

class Telegram:
    def __init__(self, cfg):
        self.token = (cfg.telegram_token or "").strip()
        self.chat = (cfg.telegram_chat_id or "").strip()
        self.enabled = bool(self.token and self.chat)
        masked = (self.token[:8] + "..." + self.token[-4:]) if len(self.token) > 12 else "<empty>"
        log("telegram_init", enabled=self.enabled, token_prefix=masked, chat_id=self.chat or "<empty>")

    def _post(self, method: str, timeout: int = 25, **kw):
        if not self.enabled:
            log("telegram_skipped", reason="token_or_chat_missing", method=method)
            return False

        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in range(3):
            try:
                r = requests.post(url, timeout=timeout, **kw)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("ok") is False:
                        log("telegram_api_error", method=method, description=str(data.get("description", ""))[:200])
                        return False
                    return True
                if r.status_code == 429:
                    retry = r.json().get("parameters", {}).get("retry_after", 3)
                    log("telegram_rate_limited", method=method, retry_after=retry)
                    time.sleep(retry + 1)
                    continue
                if r.status_code >= 500:
                    time.sleep(2 + attempt)
                    continue
                body_snippet = (r.text or "")[:300]
                log("telegram_error", method=method, status=r.status_code, body=body_snippet)
                return False
            except requests.exceptions.Timeout:
                log("telegram_timeout", method=method, attempt=attempt + 1)
                time.sleep(1.5)
            except requests.exceptions.ConnectionError as e:
                log("telegram_connection_error", method=method, error=str(e)[:200])
                time.sleep(1.5)
            except Exception as e:
                log("telegram_exception", method=method, error=str(e)[:200])
                time.sleep(1.0)
        return False

    def message(self, text: str, pre: bool = False) -> bool:
        body = f"<pre>{html.escape(text)}</pre>" if pre else text
        return self._post("sendMessage", data={
            "chat_id": self.chat,
            "text": body,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })

    def photo(self, path: str, caption: str = "") -> bool:
        try:
            with open(path, "rb") as f:
                return self._post("sendPhoto", data={
                    "chat_id": self.chat,
                    "caption": caption[:1020], # Telegram caption limit is 1024 chars
                    "parse_mode": "HTML",
                }, files={"photo": f})
        except FileNotFoundError:
            log("telegram_photo_missing", path=path)
            return False
        except Exception as e:
            log("telegram_photo_failed", error=str(e)[:200])
            return False

    def test(self) -> dict:
        if not self.enabled:
            return {"ok": False, "reason": "token_or_chat_missing", "token_set": bool(self.token), "chat_set": bool(self.chat)}
        try:
            url = f"https://api.telegram.org/bot{self.token}/getMe"
            r = requests.get(url, timeout=15)
            me = r.json()
            bot_username = (me.get("result") or {}).get("username", "?")
            bot_ok = bool(me.get("ok"))

            test_text = (
                f"🔔 <b>Test message from Quotex Signal Bot</b>\n"
                f"Bot: @{bot_username}\n"
                f"If you see this, Telegram delivery works ✅"
            )
            sent = self._post("sendMessage", data={
                "chat_id": self.chat,
                "text": test_text,
                "parse_mode": "HTML",
            })
            return {"ok": bot_ok and sent, "bot_ok": bot_ok, "bot_username": bot_username, "send_ok": sent, "chat_id": self.chat}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
