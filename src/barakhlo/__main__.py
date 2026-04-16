from __future__ import annotations

import asyncio
import sys

from . import app


USAGE = (
    "usage: barakhlo {run|auth|chats|scan [days]}\n"
    "  run            start the userbot daemon\n"
    "  auth           interactive login; creates session in /data\n"
    "  chats          list all dialogs with their ids\n"
    "  scan [days]    one-shot scan of SOURCE_CHATS history (default 7)\n"
)


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write(USAGE)
        return 2
    cmd = sys.argv[1]
    if cmd == "run":
        asyncio.run(app.run())
    elif cmd == "auth":
        asyncio.run(app.auth())
    elif cmd == "chats":
        asyncio.run(app.list_chats())
    elif cmd == "scan":
        days = 7
        if len(sys.argv) >= 3:
            try:
                days = int(sys.argv[2])
            except ValueError:
                sys.stderr.write(f"bad days: {sys.argv[2]}\n")
                return 2
        asyncio.run(app.scan_cli(days))
    else:
        sys.stderr.write(USAGE)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
