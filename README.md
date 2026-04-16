# barakhlo

Telegram userbot that watches flea-market chats, matches messages against a keyword
list (exact + fuzzy), and forwards hits to a DM via a Telegram bot.

- Userbot via [Telethon](https://docs.telethon.dev/)
- Fuzzy matching via [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz)
- Dedup via SQLite (same `msg_id` or same normalized text within 7 days)
- Dockerized, deployed by GitHub Actions self-hosted runner on the VPS

## Layout

```
src/barakhlo/      python package
data/              runtime volume on VPS: keywords.json, session, seen.db
Dockerfile         multi-stage, uv-based
compose.yaml       single service, reads ./.env, mounts ./data
.github/workflows/ build (on GitHub) + deploy (on self-hosted runner)
```

## First-time VPS setup

All paths below assume the runner user `garuda` and app home `~/barakhlo`
(override with `BARAKHLO_HOME`).

### 1. Pre-reqs on VPS
```bash
docker --version && docker compose version
# add runner user to docker group if not already
sudo usermod -aG docker $USER
# re-login
```

### 2. Prepare app home
```bash
mkdir -p ~/barakhlo/data
cd ~/barakhlo
curl -O https://raw.githubusercontent.com/garootman/barakhlo/main/compose.yaml
```

### 3. Create `.env` at `~/barakhlo/.env`
```env
TG_API_ID=...
TG_API_HASH=...
TG_BOT_TOKEN=...
TARGET_CHAT_ID=...
SOURCE_CHATS=...
FUZZY_THRESHOLD=85
STARTUP_SCAN_HOURS=24
```

`TARGET_CHAT_ID`: send `/start` to your bot, then
```bash
curl "https://api.telegram.org/bot$TG_BOT_TOKEN/getUpdates" | jq '.result[0].message.chat.id'
```

### 4. Log in to the userbot (one time)
```bash
cd ~/barakhlo
docker compose run --rm barakhlo auth
```
Enter phone, code from Telegram, and 2FA password. Session lands in `./data/barakhlo.session`.

### 5. Find source chat ids
```bash
docker compose run --rm barakhlo chats
```
Copy the numeric ids of the flea-market chats into `SOURCE_CHATS=` (comma-separated).

### 6. Start
```bash
docker compose up -d
docker compose logs -f
```

### 7. Set up the GitHub Actions self-hosted runner
On the repo page: **Settings → Actions → Runners → New self-hosted runner**.
Follow the shown steps on VPS. Register it as a service so it survives reboots:
```bash
cd ~/actions-runner
./svc.sh install
./svc.sh start
```
Runner user must be in the `docker` group.

From now on, every push to `main` builds the image on GitHub, pushes to
`ghcr.io/garootman/barakhlo`, and the self-hosted runner pulls + restarts.

## Managing keywords from your phone

Send these to **Saved Messages** (the userbot reads its own account):

| command              | effect                                             |
|----------------------|----------------------------------------------------|
| `.help`              | show available commands                            |
| `.kw list`           | list keywords                                      |
| `.kw add <word>`     | add a keyword                                      |
| `.kw rm <word>`      | remove a keyword                                   |
| `.scan [days]`       | rescan source chats for last N days (default 7)    |
| `.ping`              | liveness check                                     |

Keywords are stored in `data/keywords.json` and can also be edited on disk.

## Local dev

```bash
uv sync
cp .env.example .env  # fill in
mkdir -p data
BARAKHLO_DATA=$PWD/data uv run python -m barakhlo auth
BARAKHLO_DATA=$PWD/data uv run python -m barakhlo chats
BARAKHLO_DATA=$PWD/data uv run python -m barakhlo run
```

## Notes

- Forwarding goes through the **Bot API**, so the match preview is sent *from the
  bot*, not from your user account. Original links are included where possible.
- The fuzzy matcher skips fuzzy mode for keywords shorter than 5 chars (would
  false-positive too much). Short keywords still match as exact substrings.
- On restart, the daemon rescans the last `STARTUP_SCAN_HOURS` of history so no
  posts are missed across container restarts. Dedup ensures nothing is duplicated.
