from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telethon import events

    from .keywords import Keywords


log = logging.getLogger(__name__)

_CMD_RE = re.compile(
    r"^\.(?P<cmd>kw|bl|scan|ping|help)(?:\s+(?P<action>\S+))?(?:\s+(?P<arg>.+))?\s*$",
    re.IGNORECASE,
)


HELP_TEXT = (
    "commands (send here in Saved Messages)\n"
    "\n"
    "KEYWORDS — forward messages containing any of these\n"
    "  .kw                — list keywords\n"
    "  .kw add <word>     — add keyword   (e.g. .kw add диван)\n"
    "  .kw rm <word>      — remove keyword (e.g. .kw rm диван)\n"
    "\n"
    "BLOCKLIST — ignore messages containing any of these (overrides keywords)\n"
    "  .bl                — list blocked words\n"
    "  .bl add <word>     — add blocked word (e.g. .bl add куплю)\n"
    "  .bl rm <word>      — remove blocked word\n"
    "\n"
    "OTHER\n"
    "  .scan [days]       — rescan source chats for last N days (default 7)\n"
    "  .ping              — check bot is alive\n"
    "  .help              — this help\n"
)


async def handle_command(
    event: "events.NewMessage.Event",
    me_id: int,
    keywords: "Keywords",
    blocklist: "Keywords",
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
        await _handle_list(event, keywords, "kw", "keywords", action, arg)
    elif cmd == "bl":
        await _handle_list(event, blocklist, "bl", "blocklist", action, arg)
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


async def _handle_list(
    event, items: "Keywords", cmd: str, name: str, action: str, arg: str
) -> None:
    if action in ("", "list"):
        kws = items.all()
        if not kws:
            await event.reply(f"{name}: (empty)")
        else:
            body = "\n".join(f"- {k}" for k in kws)
            await event.reply(f"{name} ({len(kws)}):\n{body}")
        return
    if action == "add":
        if not arg:
            await event.reply(f"usage: .{cmd} add <word>")
            return
        ok = items.add(arg)
        await event.reply(f"{'added' if ok else 'already exists'}: {arg}")
        return
    if action in ("rm", "remove", "del"):
        if not arg:
            await event.reply(f"usage: .{cmd} rm <word>")
            return
        ok = items.remove(arg)
        await event.reply(f"{'removed' if ok else 'not found'}: {arg}")
        return
    await event.reply(f"unknown {name} action: {action}")
