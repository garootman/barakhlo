from __future__ import annotations

import json
import logging

import httpx


log = logging.getLogger(__name__)

MediaItem = tuple[bytes, str, str]  # (data, kind, filename) — kind in {photo,video,document}


class Forwarder:
    """Sends text / media / media groups to a target chat via the Telegram Bot API."""

    def __init__(self, bot_token: str, target_chat_id: int) -> None:
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self.target = target_chat_id
        self._client = httpx.AsyncClient(timeout=60.0)

    async def send(self, text: str) -> bool:
        try:
            r = await self._client.post(
                f"{self._base}/sendMessage",
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

    async def send_media(self, data: bytes, kind: str, filename: str, caption: str) -> bool:
        method = {"photo": "sendPhoto", "video": "sendVideo"}.get(kind, "sendDocument")
        field = {"photo": "photo", "video": "video"}.get(kind, "document")
        form = {
            "chat_id": str(self.target),
            "caption": caption[:1024],
            "parse_mode": "HTML",
        }
        try:
            r = await self._client.post(
                f"{self._base}/{method}",
                data=form,
                files={field: (filename, data)},
            )
            if r.status_code >= 400:
                log.warning("bot %s failed: %s %s", method, r.status_code, r.text[:300])
                return False
            return True
        except httpx.HTTPError as e:
            log.warning("bot %s error: %s", method, e)
            return False

    async def send_media_group(self, items: list[MediaItem], caption: str) -> bool:
        if not items:
            return False
        if len(items) == 1:
            data, kind, filename = items[0]
            return await self.send_media(data, kind, filename, caption)
        files: dict[str, tuple[str, bytes]] = {}
        descriptors: list[dict] = []
        for i, (data, kind, filename) in enumerate(items[:10]):
            key = f"file{i}"
            files[key] = (filename, data)
            t = kind if kind in ("photo", "video", "document") else "document"
            desc: dict = {"type": t, "media": f"attach://{key}"}
            if i == 0 and caption:
                desc["caption"] = caption[:1024]
                desc["parse_mode"] = "HTML"
            descriptors.append(desc)
        form = {"chat_id": str(self.target), "media": json.dumps(descriptors)}
        try:
            r = await self._client.post(
                f"{self._base}/sendMediaGroup", data=form, files=files
            )
            if r.status_code >= 400:
                log.warning(
                    "bot sendMediaGroup failed: %s %s", r.status_code, r.text[:300]
                )
                return False
            return True
        except httpx.HTTPError as e:
            log.warning("bot sendMediaGroup error: %s", e)
            return False

    async def close(self) -> None:
        await self._client.aclose()
