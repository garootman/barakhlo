# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dev loop uses `uv` (project is Python 3.12, package layout is `src/barakhlo`):

```bash
uv sync                                    # install deps into .venv (includes dev group)
uv run ruff check .                        # lint
uv run ruff format .                       # format (line-length=100, target py312)

# CLI entrypoint is `python -m barakhlo` (see src/barakhlo/__main__.py).
# Local runs need BARAKHLO_DATA pointing at a writable dir and a populated .env.
BARAKHLO_DATA=$PWD/data uv run python -m barakhlo auth      # interactive Telethon login, writes data/barakhlo.session
BARAKHLO_DATA=$PWD/data uv run python -m barakhlo chats     # list dialog ids to populate SOURCE_CHATS
BARAKHLO_DATA=$PWD/data uv run python -m barakhlo run       # start the daemon
BARAKHLO_DATA=$PWD/data uv run python -m barakhlo scan 7    # one-shot history scan (days)
```

No test suite exists. Do not invent one unless asked.

## Architecture

Single-process async app. The Telethon userbot (one user session) is the event source; the Telegram Bot API (separate bot token) is the *output* channel. Matches are forwarded from the bot, not the user account — this split is intentional and load-bearing: it keeps the user account silent while still letting it read chats.

Entry points (`app.py`):
- `run()` — long-running daemon. Registers one `events.NewMessage` handler that first tries `commands.handle_command` (Saved-Messages-only control channel), then `_process_message` (source-chat match + forward). On startup, kicks off `_scan_history` for `STARTUP_SCAN_HOURS` to cover restarts.
- `auth()` / `list_chats()` / `scan_cli()` — one-shot CLI helpers sharing the same config/client setup.

Module responsibilities:
- `config.py` — env-var loading. All runtime state lives under `BARAKHLO_DATA` (default `/data` in the container, overridable locally). That dir holds `barakhlo.session`, `keywords.json`, `seen.db`.
- `keywords.py` — JSON-backed, lowercase, thread-locked keyword list. Seeded with Russian defaults on first run. Mutated by `.kw add/rm` commands and by editing `data/keywords.json` on disk (no hot-reload — `Keywords.reload()` exists but isn't wired to a command).
- `matcher.py` — substring-on-normalized-text first; for keywords ≥ `MIN_FUZZY_LEN` (5 chars), falls back to `rapidfuzz.partial_ratio` against the threshold. Short keywords deliberately skip fuzzy to avoid false positives.
- `dedup.py` — SQLite (aiosqlite) with two keys per message: `m:{chat_id}:{msg_id}` and `t:{sha256(chat+normalized_text)[:16]}`. TTL is 7 days with opportunistic GC on each insert. The text key is why startup rescans don't duplicate forwards across restarts.
- `forwarder.py` — thin `httpx` wrapper around Bot API `sendMessage`; HTML parse mode, 4096-char cap, no retries. Failures are logged and swallowed.
- `commands.py` — parses `.kw|.scan|.ping|.help` *only* from Saved Messages (`event.chat_id == me_id`). `.scan` passes through a `trigger_scan` callable provided by `run()` that guards against concurrent scans with an `asyncio.Lock`.

### Deploy model
Two GitHub Actions workflows: `build.yml` builds/pushes to `ghcr.io/garootman/barakhlo` on every push to `main`; `deploy.yml` runs on a **self-hosted runner on the VPS** after a successful build, syncs `compose.yaml` to `$BARAKHLO_HOME` (default `~/barakhlo`), and does `docker compose pull && up -d`. `.env` and `./data` live on the VPS — never in the image. Changes to those require SSH, not a push.

## Gotchas

- Telethon session file is `data/barakhlo.session`; deleting it forces re-auth. The session is tied to the phone number used in `auth`.
- `SOURCE_CHATS` accepts `@username`, invite links, or numeric ids (including `-100…` channel ids). Use `chats` subcommand to discover them — ids from `getUpdates` are for the *bot's* chats, not the userbot's.
- `TARGET_CHAT_ID` is where forwards go; it's a *bot* chat id, obtained by sending `/start` to the bot and reading `getUpdates`.
- History scan is `reverse=False` and breaks on `msg.date < cutoff` — i.e. it walks newest→oldest and stops, so very chatty sources with gaps can still be handled cheaply.
