from __future__ import annotations

import asyncio
import io
import logging
import signal
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Iterable

from telethon import TelegramClient, events, utils as tg_utils

from . import config as config_mod
from .commands import handle_command
from .dedup import Dedup
from .forwarder import Forwarder, MediaItem
from .keywords import Keywords
from .matcher import match


log = logging.getLogger("barakhlo")

ALBUM_FLUSH_SECONDS = 1.5
MAX_MEDIA_SIZE = 45_000_000  # bot API cap is 50MB; leave headroom


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
    date: datetime | None,
    *,
    max_body: int = 3500,
) -> str:
    header = f"<b>{escape(chat_title)}</b>"
    if chat_username:
        header += f" (@{escape(chat_username)})"
    header += f"\nfrom: {escape(sender_name)}"
    if date is not None:
        header += f"\ndate: {date.strftime('%Y-%m-%d %H:%M UTC')}"
    header += f"\nhits: {escape(', '.join(hits))}"
    if link:
        header += f'\n<a href="{link}">open in telegram</a>'
    body = escape(text[:max_body])
    return f"{header}\n\n{body}"


def _sender_name_of(sender) -> str:
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
            key: str | int = s
            try:
                key = int(s)
            except ValueError:
                pass
            entity = await client.get_entity(key)
            marked = tg_utils.get_peer_id(entity)
            resolved[marked] = entity
            log.info("watching %s -> id=%s title=%s", s, marked, getattr(entity, "title", ""))
        except Exception as e:
            log.error("cannot resolve source %r: %s", s, e)
    return resolved


async def _download_media(msg) -> MediaItem | None:
    if not msg.media:
        return None
    size = getattr(getattr(msg, "file", None), "size", None) or 0
    if size and size > MAX_MEDIA_SIZE:
        log.info("skip media: size %s > %s (msg_id=%s)", size, MAX_MEDIA_SIZE, msg.id)
        return None
    buf = io.BytesIO()
    try:
        await msg.download_media(file=buf)
    except Exception:
        log.exception("media download failed (msg_id=%s)", msg.id)
        return None
    data = buf.getvalue()
    if not data:
        return None
    if msg.photo:
        return data, "photo", "photo.jpg"
    if msg.video:
        return data, "video", "video.mp4"
    name = getattr(getattr(msg, "file", None), "name", None) or "file.bin"
    return data, "document", name


async def _collect_media(msgs) -> list[MediaItem]:
    candidates = [m for m in msgs if m.media]
    if not candidates:
        return []
    results = await asyncio.gather(*(_download_media(m) for m in candidates[:10]))
    return [r for r in results if r is not None]


async def _forward_group(
    msgs: list,
    *,
    chat_entity,
    hits: list[str],
    text: str,
    forwarder: Forwarder,
) -> bool:
    first = msgs[0]
    chat_title = (
        getattr(chat_entity, "title", None)
        or getattr(chat_entity, "username", None)
        or str(first.chat_id)
    )
    chat_username = getattr(chat_entity, "username", None)
    try:
        sender = await first.get_sender()
    except Exception:
        sender = None
    sender_name = _sender_name_of(sender)
    link = _chat_link(first.chat_id, first.id, chat_username)

    media = await _collect_media(msgs)
    if media:
        caption = _format(
            chat_title, chat_username, sender_name, hits, text, link, first.date, max_body=800
        )
        if len(media) > 1:
            ok = await forwarder.send_media_group(media, caption)
        else:
            data, kind, name = media[0]
            ok = await forwarder.send_media(data, kind, name, caption)
    else:
        payload = _format(
            chat_title, chat_username, sender_name, hits, text, link, first.date
        )
        ok = await forwarder.send(payload)

    if ok:
        log.info(
            "forwarded from %s ids=%s hits=%s media=%s",
            chat_title,
            [m.id for m in msgs],
            hits,
            len(media),
        )
    return ok


async def _process_group(
    msgs: list,
    *,
    source_entities: dict[int, object],
    keywords: Keywords,
    matcher_threshold: int,
    dedup: Dedup,
    forwarder: Forwarder,
) -> bool:
    if not msgs:
        return False
    msgs = sorted(msgs, key=lambda m: m.id)
    first = msgs[0]
    entity = source_entities.get(first.chat_id)
    if entity is None:
        return False
    text = " ".join(m.raw_text for m in msgs if m.raw_text)
    if not text:
        return False
    hits = match(text, keywords.all(), matcher_threshold)
    if not hits:
        return False
    if await dedup.seen_or_mark(first.chat_id, first.id, text):
        return False
    return await _forward_group(
        msgs, chat_entity=entity, hits=hits, text=text, forwarder=forwarder
    )


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
        buffer: list = []
        current_gid: int | None = None

        async def flush() -> None:
            nonlocal forwarded
            if not buffer:
                return
            group = list(buffer)
            buffer.clear()
            if await _process_group(
                group,
                source_entities=source_entities,
                keywords=keywords,
                matcher_threshold=matcher_threshold,
                dedup=dedup,
                forwarder=forwarder,
            ):
                forwarded += 1
                await asyncio.sleep(0.3)  # bot-api flood guard

        async for msg in client.iter_messages(entity):
            if msg.date is None or msg.date < cutoff:
                break
            count += 1
            gid = getattr(msg, "grouped_id", None)
            if gid != current_gid or gid is None:
                await flush()
                current_gid = gid
            buffer.append(msg)
        await flush()
        total_forwarded += forwarded
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

    album_buffers: dict[int, list] = {}

    async def _flush_album(gid: int) -> None:
        await asyncio.sleep(ALBUM_FLUSH_SECONDS)
        msgs = album_buffers.pop(gid, [])
        if msgs:
            try:
                await _process_group(
                    msgs,
                    source_entities=source_entities,
                    keywords=keywords,
                    matcher_threshold=cfg.fuzzy_threshold,
                    dedup=dedup,
                    forwarder=forwarder,
                )
            except Exception:
                log.exception("album flush failed (gid=%s)", gid)

    @client.on(events.NewMessage())
    async def _on_new(event):
        try:
            if await handle_command(event, me.id, keywords, trigger_scan):
                return
            msg = event.message
            if msg.chat_id not in source_entities:
                return
            gid = getattr(msg, "grouped_id", None)
            if gid is not None:
                is_new = gid not in album_buffers
                album_buffers.setdefault(gid, []).append(msg)
                if is_new:
                    asyncio.create_task(_flush_album(gid))
                return
            await _process_group(
                [msg],
                source_entities=source_entities,
                keywords=keywords,
                matcher_threshold=cfg.fuzzy_threshold,
                dedup=dedup,
                forwarder=forwarder,
            )
        except Exception:
            log.exception("handler error")

    if cfg.startup_scan_hours > 0 and source_entities:
        hours = cfg.startup_scan_hours
        days = max(1, (hours + 23) // 24)
        log.info("startup catchup: last ~%s hours (%s days bucket)", hours, days)
        asyncio.create_task(trigger_scan(days))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(sig: int) -> None:
        if not stop.is_set():
            log.info("received signal %s, shutting down", sig)
            stop.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _on_signal, int(s))
        except NotImplementedError:
            pass  # e.g. Windows

    log.info("running; send .help to Saved Messages for commands")
    run_task = asyncio.create_task(client.run_until_disconnected())
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, pending = await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        if stop_task in done:
            log.info("disconnecting client")
            await client.disconnect()
            # let in-flight album flushes / handlers settle
            await asyncio.sleep(ALBUM_FLUSH_SECONDS + 0.5)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("closing forwarder + dedup")
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
    out_path = cfg.data_dir / "chats.txt"
    lines = [f"{'id':>16}  {'type':<8}  title / username", "-" * 70]
    count = 0
    async for d in client.iter_dialogs():
        ent = d.entity
        kind = type(ent).__name__
        uname = getattr(ent, "username", None)
        label = d.name + (f" (@{uname})" if uname else "")
        lines.append(f"{d.id:>16}  {kind:<8}  {label}")
        count += 1
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {count} dialogs to {out_path}")
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
