from __future__ import annotations

import hashlib
import time
from pathlib import Path

import aiosqlite

DEFAULT_TTL_SECONDS = 7 * 24 * 3600
GC_INTERVAL_SECONDS = 3600


class Dedup:
    """Remembers (chat_id, msg_id) and (chat_id, normalized_text_hash) for TTL seconds."""

    def __init__(self, path: Path, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.path = path
        self.ttl = ttl_seconds
        self._db: aiosqlite.Connection | None = None
        self._last_gc = 0.0

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        # WAL + NORMAL sync: commit-per-message no longer means fsync-per-message
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts INTEGER NOT NULL)"
        )
        await self._db.execute("CREATE INDEX IF NOT EXISTS seen_ts ON seen(ts)")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @staticmethod
    def _text_key(chat_id: int, text: str) -> str:
        norm = " ".join(text.strip().lower().split())
        digest = hashlib.sha256(f"{chat_id}::{norm}".encode()).hexdigest()[:16]
        return f"t:{digest}"

    @staticmethod
    def _msg_key(chat_id: int, msg_id: int) -> str:
        return f"m:{chat_id}:{msg_id}"

    async def seen_or_mark(self, chat_id: int, msg_id: int, text: str) -> bool:
        assert self._db is not None, "Dedup.open() not called"
        now = int(time.time())
        cutoff = now - self.ttl
        msg_k = self._msg_key(chat_id, msg_id)
        text_k = self._text_key(chat_id, text) if text else None

        keys: list[str] = [msg_k]
        if text_k:
            keys.append(text_k)
        placeholders = ",".join("?" * len(keys))
        cur = await self._db.execute(
            f"SELECT 1 FROM seen WHERE key IN ({placeholders}) AND ts > ? LIMIT 1",
            (*keys, cutoff),
        )
        row = await cur.fetchone()
        await cur.close()
        if row:
            return True

        rows = [(msg_k, now)]
        if text_k:
            rows.append((text_k, now))
        await self._db.executemany("INSERT OR REPLACE INTO seen(key, ts) VALUES (?, ?)", rows)
        if now - self._last_gc >= GC_INTERVAL_SECONDS:
            await self._db.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
            self._last_gc = now
        await self._db.commit()
        return False
