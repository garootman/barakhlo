from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Iterable

from telethon import TelegramClient, events

from . import config as config_mod
from .commands import handle_command
from .dedup import Dedup
from .forwarder import Forwarder
from .keywords import Keywords
from .matcher import match


log = logging.getLogger("barakhlo")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _client(cfg: config_mod.Config) -> TelegramClient:
    cfg.session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(cfg.session_path), cfg.api_id, cfg.api_hash)


def _chat_link(chat_id: int, msg_id: int, username: str | None) -> str | None:
    if username:
        return f"https://t.me/{username}/{msg_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{msg_id}"
    return None


def _format(
    chat_title: str,
    chat_username: str | None,
    sender_name: str,
    hits: list[str],
    text: str,
    link: str | None,
) -> str:
    header = f"<b>{escape(chat_title)}</b>"
    if chat_username:
        header += f" (@{escape(chat_username)})"
    header += f"\nfrom: {escape(sender_name)}\nhits: {escape(', '.join(hits))}"
    body = escape(text[:3500])
    footer = f'\n<a href="{link}">open in telegram</a>' if link else ""
    return f"{header}\n\n{body}{footer}"


async def _sender_name(event) -> str:
    sender = await event.get_sender()
    if sender is None:
        return "?"
    parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
    name = " ".join(p for p in parts if p).strip()
    if not name:
        name = getattr(sender, "username", None) or str(getattr(sender, "id", "?"))
    return name


async def _resolve_sources(client: TelegramClient, raw: Iterable[str]) -> dict[int, object]:
    resolved: dict[int, object] = {}
    for s in raw:
        try:
            # numeric ids
            key: str | int = s
            try:
                key = int(s)
            except ValueError:
                pass
            entity = await client.get_entity(key)
            resolved[entity.id] = entity
            log.info("watching %s -> id=%s title=%s", s, entity.id, getattr(entity, "title", ""))
        except Exception as e:
            log.error("cannot resolve source %r: %s", s, e)
    return resolved


async def _process_message(
    event,
    *,
    source_entities: dict[int, object],
    keywords: Keywords,
    matcher_threshold: int,
    dedup: Dedup,
    forwarder: Forwarder,
) -> None:
    if event.chat_id not in source_entities:
        return
    text = event.raw_text or ""
    if not text:
        return
    hits = match(text, keywords.all(), matcher_threshold)
    if not hits:
        return
    if await dedup.seen_or_mark(event.chat_id, event.id, text):
        return

    chat = source_entities[event.chat_id]
    chat_title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)
    chat_username = getattr(chat, "username", None)
    sender_name = await _sender_name(event)
    link = _chat_link(event.chat_id, event.id, chat_username)

    msg = _format(chat_title, chat_username, sender_name, hits, text, link)
    ok = await forwarder.send(msg)
    if ok:
        log.info("forwarded from %s msg_id=%s hits=%s", chat_title, event.id, hits)


async def _scan_history(
    client: TelegramClient,
    source_entities: dict[int, object],
    days: int,
    *,
    keywords: Keywords,
    matcher_threshold: int,
    dedup: Dedup,
    forwarder: Forwarder,
) -> int:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    total_forwarded = 0
    for chat_id, entity in source_entities.items():
        title = getattr(entity, "title", str(chat_id))
        log.info("scanning %s for last %s days...", title, days)
        count = 0
        forwarded = 0
        async for msg in client.iter_messages(entity, offset_date=None, reverse=False):
            if msg.date is None or msg.date < cutoff:
                break
            count += 1
            text = msg.raw_text or msg.message or ""
            if not text:
                continue
            hits = match(text, keywords.all(), matcher_threshold)
            if not hits:
                continue
            if await dedup.seen_or_mark(chat_id, msg.id, text):
                continue

            chat_username = getattr(entity, "username", None)
            sender_name = "?"
            try:
                sender = await msg.get_sender()
                if sender is not None:
                    parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
                    n = " ".join(p for p in parts if p).strip()
                    sender_name = n or getattr(sender, "username", None) or str(getattr(sender, "id", "?"))
            except Exception:
                pass
            link = _chat_link(chat_id, msg.id, chat_username)
            payload = _format(title, chat_username, sender_name, hits, text, link)
            if await forwarder.send(payload):
                forwarded += 1
                total_forwarded += 1
            # small delay to avoid bot-api flood
            await asyncio.sleep(0.2)
        log.info("scanned %s: %s msgs seen, %s forwarded", title, count, forwarded)
    return total_forwarded


async def run() -> None:
    _setup_logging()
    cfg = config_mod.load()
    client = _client(cfg)
    await client.start()
    me = await client.get_me()
    log.info("logged in as @%s (id=%s)", me.username, me.id)

    keywords = Keywords(cfg.keywords_path)
    dedup = Dedup(cfg.seen_db_path)
    await dedup.open()
    forwarder = Forwarder(cfg.bot_token, cfg.target_chat_id)

    source_entities = await _resolve_sources(client, cfg.source_chats)

    # in-process scan trigger (used by .scan command)
    scan_lock = asyncio.Lock()

    async def trigger_scan(days: int) -> None:
        if scan_lock.locked():
            await forwarder.send(f"scan already running, ignoring new request for {days}d")
            return
        async with scan_lock:
            try:
                n = await _scan_history(
                    client,
                    source_entities,
                    days,
                    keywords=keywords,
                    matcher_threshold=cfg.fuzzy_threshold,
                    dedup=dedup,
                    forwarder=forwarder,
                )
                await forwarder.send(f"scan done: {n} new hits in last {days}d")
            except Exception as e:
                log.exception("scan failed")
                await forwarder.send(f"scan failed: {e}")

    @client.on(events.NewMessage())
    async def _on_new(event):
        try:
            if await handle_command(event, me.id, keywords, trigger_scan):
                return
            await _process_message(
                event,
                source_entities=source_entities,
                keywords=keywords,
                matcher_threshold=cfg.fuzzy_threshold,
                dedup=dedup,
                forwarder=forwarder,
            )
        except Exception:
            log.exception("handler error")

    # startup catchup
    if cfg.startup_scan_hours > 0 and source_entities:
        hours = cfg.startup_scan_hours
        days = max(1, (hours + 23) // 24)
        log.info("startup catchup: last ~%s hours (%s days bucket)", hours, days)
        asyncio.create_task(trigger_scan(days))

    log.info("running; send .help to Saved Messages for commands")
    try:
        await client.run_until_disconnected()
    finally:
        await forwarder.close()
        await dedup.close()


async def auth() -> None:
    _setup_logging()
    cfg = config_mod.load()
    client = _client(cfg)
    await client.start()
    me = await client.get_me()
    print(f"OK logged in as @{me.username} id={me.id} name={me.first_name}")
    await client.disconnect()


async def list_chats() -> None:
    _setup_logging()
    cfg = config_mod.load()
    client = _client(cfg)
    await client.start()
    print(f"{'id':>16}  {'type':<8}  title / username")
    print("-" * 70)
    async for d in client.iter_dialogs():
        ent = d.entity
        kind = type(ent).__name__
        uname = getattr(ent, "username", None)
        label = d.name + (f" (@{uname})" if uname else "")
        print(f"{d.id:>16}  {kind:<8}  {label}")
    await client.disconnect()


async def scan_cli(days: int) -> None:
    _setup_logging()
    cfg = config_mod.load()
    client = _client(cfg)
    await client.start()
    keywords = Keywords(cfg.keywords_path)
    dedup = Dedup(cfg.seen_db_path)
    await dedup.open()
    forwarder = Forwarder(cfg.bot_token, cfg.target_chat_id)
    source_entities = await _resolve_sources(client, cfg.source_chats)
    try:
        n = await _scan_history(
            client,
            source_entities,
            days,
            keywords=keywords,
            matcher_threshold=cfg.fuzzy_threshold,
            dedup=dedup,
            forwarder=forwarder,
        )
        print(f"scan done: {n} new hits forwarded")
    finally:
        await forwarder.close()
        await dedup.close()
        await client.disconnect()
