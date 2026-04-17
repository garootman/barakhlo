from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

import httpx

from .keywords import Keywords


log = logging.getLogger(__name__)

_CMD_RE = re.compile(r"^/(\w+)(?:@\w+)?(?:\s+(.+?))?\s*$")

HELP_TEXT = (
    "barakhlo — Telegram keyword watcher\n"
    "\n"
    "KEYWORDS (forward messages containing any of these)\n"
    "  /kw             list keywords\n"
    "  /kwadd word     add keyword\n"
    "  /kwrm word      remove keyword\n"
    "\n"
    "BLOCKLIST (ignore messages containing any of these — overrides keywords)\n"
    "  /bl             list blocked words\n"
    "  /bladd word     add blocked word\n"
    "  /blrm word      remove blocked word\n"
    "\n"
    "OTHER\n"
    "  /scan [days]    rescan sources for last N days (default 7)\n"
    "  /ping           check alive\n"
    "  /help           this help\n"
)

# Registered with Telegram so they appear in the / menu.
_MENU_COMMANDS = [
    ("start", "show help"),
    ("help", "show help"),
    ("kw", "list keywords"),
    ("kwadd", "add keyword: /kwadd word"),
    ("kwrm", "remove keyword: /kwrm word"),
    ("bl", "list blocked words"),
    ("bladd", "add blocked word: /bladd word"),
    ("blrm", "remove blocked word: /blrm word"),
    ("scan", "rescan history: /scan [days]"),
    ("ping", "check alive"),
]


class BotCommands:
    """Polls the Bot API for commands in the configured target chat and handles them."""

    def __init__(
        self,
        token: str,
        owner_chat_id: int,
        keywords: Keywords,
        blocklist: Keywords,
        trigger_scan: Callable[[int], Awaitable[None]],
    ) -> None:
        self._url = f"https://api.telegram.org/bot{token}"
        self._owner = owner_chat_id
        self._keywords = keywords
        self._blocklist = blocklist
        self._trigger_scan = trigger_scan
        self._client = httpx.AsyncClient(timeout=70.0)
        self._offset: int | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def setup_menu(self) -> None:
        try:
            r = await self._client.post(
                f"{self._url}/setMyCommands",
                json={
                    "commands": [
                        {"command": c, "description": d} for c, d in _MENU_COMMANDS
                    ]
                },
                timeout=10,
            )
            if r.status_code >= 400:
                log.warning("setMyCommands failed: %s %s", r.status_code, r.text[:200])
        except Exception:
            log.exception("setMyCommands error")

    async def greet(self) -> None:
        await self._reply(self._owner, f"barakhlo started\n\n{HELP_TEXT}")

    async def poll(self) -> None:
        """Long-poll getUpdates; dispatch commands from the owner chat."""
        # Skip any queued updates from before we started so old commands don't replay.
        try:
            r = await self._client.get(
                f"{self._url}/getUpdates",
                params={"offset": -1, "timeout": 0},
                timeout=10,
            )
            data = r.json()
            if data.get("ok") and data.get("result"):
                self._offset = data["result"][-1]["update_id"] + 1
        except Exception:
            log.exception("getUpdates bootstrap failed")

        while True:
            params: dict[str, int] = {"timeout": 50}
            if self._offset is not None:
                params["offset"] = self._offset
            try:
                r = await self._client.get(
                    f"{self._url}/getUpdates", params=params, timeout=65
                )
                data = r.json()
            except httpx.ReadTimeout:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("getUpdates error")
                await asyncio.sleep(2)
                continue

            if not data.get("ok"):
                log.warning("getUpdates not ok: %s", data)
                await asyncio.sleep(2)
                continue

            for upd in data.get("result", []):
                self._offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = msg.get("chat", {}).get("id")
                if chat_id != self._owner:
                    continue
                text = msg.get("text") or ""
                try:
                    await self._handle(chat_id, text)
                except Exception:
                    log.exception("command handler failed")

    async def _reply(self, chat_id: int, text: str) -> None:
        try:
            await self._client.post(
                f"{self._url}/sendMessage",
                data={"chat_id": chat_id, "text": text[:4096]},
                timeout=10,
            )
        except Exception:
            log.exception("reply failed")

    async def _handle(self, chat_id: int, text: str) -> None:
        m = _CMD_RE.match(text.strip())
        if not m:
            return
        cmd = m.group(1).lower()
        arg = (m.group(2) or "").strip()

        if cmd in ("start", "help"):
            await self._reply(chat_id, HELP_TEXT)
        elif cmd == "ping":
            await self._reply(chat_id, "pong")
        elif cmd in ("kw", "keywords"):
            await self._reply(chat_id, _fmt_list("keywords", self._keywords.all()))
        elif cmd == "kwadd":
            await self._reply(chat_id, _add(self._keywords, arg, "/kwadd"))
        elif cmd in ("kwrm", "kwremove", "kwdel"):
            await self._reply(chat_id, _rm(self._keywords, arg, "/kwrm"))
        elif cmd in ("bl", "blocklist"):
            await self._reply(chat_id, _fmt_list("blocklist", self._blocklist.all()))
        elif cmd == "bladd":
            await self._reply(chat_id, _add(self._blocklist, arg, "/bladd"))
        elif cmd in ("blrm", "blremove", "bldel"):
            await self._reply(chat_id, _rm(self._blocklist, arg, "/blrm"))
        elif cmd == "scan":
            days = 7
            if arg:
                try:
                    days = max(1, min(90, int(arg)))
                except ValueError:
                    await self._reply(chat_id, f"bad days: {arg}")
                    return
            await self._reply(chat_id, f"scan started: last {days} days")
            await self._trigger_scan(days)
        else:
            await self._reply(chat_id, f"unknown command: /{cmd}\n\n{HELP_TEXT}")


def _fmt_list(name: str, items: list[str]) -> str:
    if not items:
        return f"{name}: (empty)"
    return f"{name} ({len(items)}):\n" + "\n".join(f"- {k}" for k in items)


def _add(lst: Keywords, arg: str, usage: str) -> str:
    if not arg:
        return f"usage: {usage} word"
    return f"{'added' if lst.add(arg) else 'already exists'}: {arg}"


def _rm(lst: Keywords, arg: str, usage: str) -> str:
    if not arg:
        return f"usage: {usage} word"
    return f"{'removed' if lst.remove(arg) else 'not found'}: {arg}"
