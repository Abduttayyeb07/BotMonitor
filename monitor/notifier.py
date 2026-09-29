from __future__ import annotations

import html as html_lib
import logging
import re
import requests

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str | None, chat_ids: list[str], enabled: bool = True):
        self.token, self.chat_ids, self.enabled = token, chat_ids, enabled

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("Telegram disabled: %s", text.replace("\n", " | "))
            return True
        if not self.token or not self.chat_ids:
            log.error("Telegram is enabled but credentials are missing")
            return False
        delivered = True
        for chat_id in self.chat_ids:
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=10)
                if response.ok:
                    continue
                if response.status_code == 400:
                    plain = html_lib.unescape(re.sub(r"<[^>]+>", "", text))
                    fallback = requests.post(
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        json={"chat_id": chat_id, "text": plain, "disable_web_page_preview": True}, timeout=10)
                    if fallback.ok:
                        log.warning("Telegram HTML rejected for chat %s; delivered plain-text fallback", chat_id)
                        continue
                    response = fallback
                delivered = False
                log.error("Telegram delivery failed for chat %s: HTTP %s: %s",
                          chat_id, response.status_code, response.text[:500])
            except requests.RequestException as exc:
                delivered = False
                safe_error = re.sub(r"/bot[^/\s]+", "/bot<redacted>", str(exc))
                log.error("Telegram delivery failed for chat %s: %s", chat_id, safe_error)
        return delivered
