from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _data_dir() -> Path:
    return Path(os.getenv("BARAKHLO_DATA", "/data"))


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    target_chat_id: int
    source_chats: list[str]
    fuzzy_threshold: int
    startup_scan_hours: int
    data_dir: Path

    @property
    def session_path(self) -> Path:
        return self.data_dir / "barakhlo"

    @property
    def keywords_path(self) -> Path:
        return self.data_dir / "keywords.json"

    @property
    def blocklist_path(self) -> Path:
        return self.data_dir / "blocklist.json"

    @property
    def seen_db_path(self) -> Path:
        return self.data_dir / "seen.db"


def load() -> Config:
    data = _data_dir()
    data.mkdir(parents=True, exist_ok=True)
    sources = [s.strip() for s in os.getenv("SOURCE_CHATS", "").split(",") if s.strip()]
    return Config(
        api_id=int(os.environ["TG_API_ID"]),
        api_hash=os.environ["TG_API_HASH"],
        bot_token=os.environ["TG_BOT_TOKEN"],
        target_chat_id=int(os.environ["TARGET_CHAT_ID"]),
        source_chats=sources,
        fuzzy_threshold=int(os.getenv("FUZZY_THRESHOLD", "85")),
        startup_scan_hours=int(os.getenv("STARTUP_SCAN_HOURS", "24")),
        data_dir=data,
    )
