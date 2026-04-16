# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dev loop uses `uv` (Python 3.12, package layout `src/barakhlo`):

```bash
uv sync                                    # install deps into .venv (includes dev group)
uv run ruff check .                        # lint
uv run ruff format .                       # format (line-length=100, target py312)

# CLI entrypoint is `python -m barakhlo` (see src/barakhlo/__main__.py).
# uv run does NOT auto-load .env — must pass --env-file.
# BARAKHLO_DATA points at the writable runtime dir (session, keywords.json, seen.db).
BARAKHLO_DATA=$PWD/data uv run --env-file .env python -m barakhlo auth      # interactive Telethon login
BARAKHLO_DATA=$PWD/data uv run --env-file .env python -m barakhlo chats     # writes data/chats.txt (dialog ids)
BARAKHLO_DATA=$PWD/data uv run --env-file .env python -m barakhlo run       # start the daemon
BARAKHLO_DATA=$PWD/data uv run --env-file .env python -m barakhlo scan 7    # one-shot history scan (days)

# Or via docker (builds from local Dockerfile, no ghcr pull):
docker compose -f compose.local.yaml run --rm barakhlo auth
docker compose -f compose.local.yaml up --build
```

No test suite exists. Do not invent one unless asked.

## Architecture

Single-process async app. The Telethon **userbot** (one user session) is the *input* — it reads source chats. The Telegram **Bot API** (separate bot token) is the *output* — it sends matches to `TARGET_CHAT_ID`. This split is load-bearing: the user account stays silent, and forwards show as "from bot" so original posters don't see who's watching them. Because the bot can't see source chats directly, any media has to be downloaded via Telethon and re-uploaded via Bot API — there is no `copyMessage` shortcut available.

### Entry points (`app.py`)
- `run()` — long-running daemon. One `events.NewMessage` handler: `commands.handle_command` first (Saved-Messages-only control channel), else buffer-or-process via `_process_group`. On startup, kicks off a `_scan_history` for `STARTUP_SCAN_HOURS` to cover restarts. Installs SIGINT/SIGTERM handlers that flip a `stop` event; on signal, disconnects the client, sleeps `ALBUM_FLUSH_SECONDS + 0.5s` to drain in-flight album flushes, then closes forwarder + dedup.
- `auth()` / `list_chats()` / `scan_cli()` — one-shot CLI helpers sharing the same config/client setup. `list_chats` writes `data/chats.txt` (stdout scrolls too fast for big accounts).

### Album-aware flow
Telegram albums are N separate messages sharing a `grouped_id`; only one typically carries the caption/text. Both live and scan flows collect them into groups and process the group as a unit:
- **Live**: when a message with `grouped_id` arrives, append to `album_buffers[gid]` and on the first hit schedule `_flush_album(gid)` after `ALBUM_FLUSH_SECONDS` (1.5s). Standalone messages skip the buffer and go straight to `_process_group([msg])`.
- **Scan**: `iter_messages` walks newest→oldest; consecutive messages with the same `grouped_id` accumulate into `buffer`, flushing when `grouped_id` changes or is `None`.

`_process_group` joins all `raw_text` from the group, matches keywords against the combined text, dedups using the smallest `msg_id` + combined-text hash (so rescans don't re-forward), then calls `_forward_group`. Forwarding picks the right Bot API method by media count: >1 → `sendMediaGroup`, 1 → `sendPhoto`/`sendVideo`/`sendDocument`, 0 → `sendMessage`. Media is downloaded into memory in parallel via `asyncio.gather`, up to 10 items, up to 45 MB each. Caption format puts the `open in telegram` link in the header (not footer) so it survives the 1024-char caption cap.

### Module responsibilities
- `config.py` — env-var loading. All runtime state lives under `BARAKHLO_DATA` (default `/data` in the container). Holds `barakhlo.session`, `keywords.json`, `seen.db`, `chats.txt`.
- `keywords.py` — JSON-backed, lowercase, thread-locked keyword list. Seeded with Russian defaults on first run. Mutated by `.kw add/rm` commands; edits to `data/keywords.json` on disk require a restart (no hot-reload — `Keywords.reload()` exists but isn't wired to a command).
- `matcher.py` — substring-on-normalized-text first; for keywords ≥ `MIN_FUZZY_LEN` (5 chars), falls back to `rapidfuzz.partial_ratio` against `FUZZY_THRESHOLD`. Short keywords skip fuzzy to avoid false positives.
- `dedup.py` — SQLite (aiosqlite) with two keys per group: `m:{chat_id}:{first_msg_id}` and `t:{sha256(chat+normalized_combined_text)[:16]}`. 7-day TTL, opportunistic GC on insert. The text key is what lets startup rescans and `.scan` not re-forward.
- `forwarder.py` — thin `httpx` wrapper: `send` (text), `send_media` (photo/video/document), `send_media_group` (album). HTML parse mode. Failures logged and swallowed.
- `commands.py` — parses `.kw|.scan|.ping|.help` *only* when `event.chat_id == me_id` (Saved Messages). `.scan` passes through a `trigger_scan` callable from `run()` guarded by `asyncio.Lock` against concurrent scans.

### Deploy model
Two GitHub Actions workflows: `build.yml` builds/pushes to `ghcr.io/garootman/barakhlo` on every push to `main`; `deploy.yml` runs on a **self-hosted runner on the VPS** after a successful build, syncs `compose.yaml` to `$BARAKHLO_HOME` (default `~/barakhlo`), `docker compose pull && up -d`. `.env` and `./data` live on the VPS — never in the image. Changes to those require SSH, not a push. `compose.local.yaml` is the dev-time counterpart that builds from the local `Dockerfile`.

## Gotchas

- **`msg.chat_id` is marked, `entity.id` is raw.** For supergroups/channels, `msg.chat_id` is `-1001234567890` while `entity.id` is `1234567890`. `source_entities` is keyed via `telethon.utils.get_peer_id(entity)` which returns the marked form — do **not** key by `entity.id` or lookups will silently miss every message.
- **Bot API caption limit is 1024 chars**; text-only messages are 4096. `_format` accepts `max_body` for this reason.
- **Media ≥ 50 MB** will be rejected by Bot API. Download is skipped above `MAX_MEDIA_SIZE` (45 MB) with a log line.
- **`uv run` does not auto-load `.env`** — pass `--env-file .env` or export vars manually. `docker compose` loads it via `env_file:`.
- **Telethon session file is `data/barakhlo.session`**; deleting it forces re-auth. The session is tied to the phone number used in `auth`.
- **`SOURCE_CHATS`** accepts `@username`, invite links, or numeric ids (including `-100…`). Use `chats` subcommand to discover them — ids from `getUpdates` are for the *bot's* chats, not the userbot's.
- **`TARGET_CHAT_ID`** is a *bot* chat id (send `/start` to the bot, read `getUpdates`).
- **History scan** walks newest→oldest and breaks on `msg.date < cutoff`, so chatty sources are cheap to rescan.
