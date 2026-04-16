from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telethon import events

    from .keywords import Keywords


log = logging.getLogger(__name__)

_CMD_RE = re.compile(
    r"^\.(?P<cmd>kw|scan|ping|help)(?:\s+(?P<action>\S+))?(?:\s+(?P<arg>.+))?\s*$",
    re.IGNORECASE,
)


HELP_TEXT = (
    "commands (send here in Saved Messages):\n"
    ".kw list             — show keywords\n"
    ".kw add <word>       — add keyword\n"
    ".kw rm <word>        — remove keyword\n"
    ".scan [days]         — rescan source chats for last N days (default 7)\n"
    ".ping                — check bot is alive\n"
    ".help                — this help\n"
)


async def handle_command(
    event: "events.NewMessage.Event",
    me_id: int,
    keywords: "Keywords",
    trigger_scan,
) -> bool:
    """Handle a command in Saved Messages. Returns True if it was a command."""
    if event.chat_id != me_id:
        return False
    raw = (event.raw_text or "").strip()
    m = _CMD_RE.match(raw)
    if not m:
        return False

    cmd = m.group("cmd").lower()
    action = (m.group("action") or "").lower()
    arg = (m.group("arg") or "").strip()

    if cmd == "kw":
        await _handle_kw(event, keywords, action, arg)
    elif cmd == "scan":
        days = 7
        if action:
            try:
                days = max(1, min(90, int(action)))
            except ValueError:
                await event.reply(f"bad days arg: {action}")
                return True
        await event.reply(f"scan started: last {days} days")
        await trigger_scan(days)
    elif cmd == "ping":
        await event.reply("pong")
    elif cmd == "help":
        await event.reply(HELP_TEXT)

    return True


async def _handle_kw(event, keywords: "Keywords", action: str, arg: str) -> None:
    if action == "list" or not action:
        kws = keywords.all()
        if not kws:
            await event.reply("keywords: (empty)")
        else:
            body = "\n".join(f"- {k}" for k in kws)
            await event.reply(f"keywords ({len(kws)}):\n{body}")
        return
    if action == "add":
        if not arg:
            await event.reply("usage: .kw add <word>")
            return
        ok = keywords.add(arg)
        await event.reply(f"{'added' if ok else 'already exists'}: {arg}")
        return
    if action in ("rm", "remove", "del"):
        if not arg:
            await event.reply("usage: .kw rm <word>")
            return
        ok = keywords.remove(arg)
        await event.reply(f"{'removed' if ok else 'not found'}: {arg}")
        return
    await event.reply(f"unknown kw action: {action}")
