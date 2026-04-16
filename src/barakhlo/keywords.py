from __future__ import annotations

import json
import threading
from pathlib import Path


DEFAULT_KEYWORDS = ["микроволновка", "аэрогриль", "диван"]


class Keywords:
    """Thread/async-safe (single-process) JSON-backed keyword list."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write(DEFAULT_KEYWORDS)
        self._list = self._read()

    def _read(self) -> list[str]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        kws = data.get("keywords", [])
        return [k for k in (str(x).strip() for x in kws) if k]

    def _write(self, kws: list[str]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"keywords": kws}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def all(self) -> list[str]:
        with self._lock:
            return list(self._list)

    def add(self, kw: str) -> bool:
        kw = kw.strip().lower()
        if not kw:
            return False
        with self._lock:
            if kw in self._list:
                return False
            self._list.append(kw)
            self._write(self._list)
            return True

    def remove(self, kw: str) -> bool:
        kw = kw.strip().lower()
        with self._lock:
            if kw not in self._list:
                return False
            self._list.remove(kw)
            self._write(self._list)
            return True

    def reload(self) -> list[str]:
        with self._lock:
            self._list = self._read()
            return list(self._list)
