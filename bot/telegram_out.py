"""telegram_out.py -- delivery via Telegram Bot API (requests only)."""
import html
import time

import requests

from .logger import log


class Telegram:
    def __init__(self, cfg):
        self.token, self.chat = cfg.telegram_token, cfg.telegram_chat_id
        self.enabled = bool(self.token and self.chat)

    def _post(self, method: str, **kw):
        if not self.enabled:
            log("telegram_disabled", method=method)
            return False
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in range(3):
            try:
                r = requests.post(url, timeout=25, **kw)
                if r.status_code == 200:
                    return True
                log("telegram_error", status=r.status_code, body=r.text[:200])
                if r.status_code == 429:
                    time.sleep(2 + attempt * 2)
                    continue
                return False
            except Exception as e:
                log("telegram_exception", error=str(e)[:200])
                time.sleep(1.5)
        return False

    def message(self, text: str, pre: bool = False) -> bool:
        body = f"<pre>{html.escape(text)}</pre>" if pre else text
        return self._post("sendMessage", data={"chat_id": self.chat, "text": body, "parse_mode": "HTML"})

    def photo(self, path: str, caption: str = "") -> bool:
        try:
            with open(path, "rb") as f:
                return self._post("sendPhoto", data={"chat_id": self.chat, "caption": caption[:1000], "parse_mode": "HTML"}, files={"photo": f})
        except Exception as e:
            log("telegram_photo_failed", error=str(e)[:200])
            return False
