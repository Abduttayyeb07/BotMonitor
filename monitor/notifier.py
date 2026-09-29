from __future__ import annotations

import logging
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
                    json={"chat_id": chat_id, "text": text}, timeout=10)
                response.raise_for_status()
            except requests.RequestException:
                delivered = False
                log.exception("Telegram delivery failed for chat %s", chat_id)
        return delivered
