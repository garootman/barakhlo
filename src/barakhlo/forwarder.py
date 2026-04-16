from __future__ import annotations

import logging

import httpx


log = logging.getLogger(__name__)


class Forwarder:
    """Sends HTML messages to a target chat using the Telegram Bot API."""

    def __init__(self, bot_token: str, target_chat_id: int) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.target = target_chat_id
        self._client = httpx.AsyncClient(timeout=15.0)

    async def send(self, text: str) -> bool:
        try:
            r = await self._client.post(
                self._url,
                json={
                    "chat_id": self.target,
                    "text": text[:4096],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if r.status_code >= 400:
                log.warning("bot send failed: %s %s", r.status_code, r.text[:300])
                return False
            return True
        except httpx.HTTPError as e:
            log.warning("bot send error: %s", e)
            return False

    async def close(self) -> None:
        await self._client.aclose()
