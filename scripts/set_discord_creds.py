"""Write the Discord bot token and your user ID into .env.

Prompted rather than passed as arguments, so the token never lands in shell
history, and hidden while typing via getpass. A shell one-liner was the first
attempt and it broke: `read -p` is bash, and macOS defaults to zsh where -p
means "read from coprocess". Python has no such difference.

Nothing is written unless both values look right, so a mis-paste fails loudly
instead of half-filling the file.

Usage:
    .venv/bin/python scripts/set_discord_creds.py
    .venv/bin/python scripts/set_discord_creds.py --verify   # also ask Discord
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Three dot-separated base64url segments. Checked because the overwhelmingly
# common failure is a partial copy, and a truncated token fails at connect with
# an opaque 401 rather than anything that points back to here.
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}$")


def ask_token() -> str:
    print("Paste the bot token, then press Enter.")
    print("Nothing will appear as you paste -- that is deliberate.\n")
    token = getpass.getpass("Bot token: ").strip()
    if not token:
        sys.exit("No token entered; nothing written.")
    if token.lower().startswith("bot "):
        # The Authorization header wants "Bot <token>"; the token itself does not.
        token = token[4:].strip()
        print("  (stripped the leading 'Bot ' -- the token itself does not include it)")
    if not TOKEN_SHAPE.match(token):
        print(f"\nThat does not look like a bot token ({len(token)} chars).", file=sys.stderr)
        print("Expected three parts separated by dots, around 70 characters.", file=sys.stderr)
        print("The usual cause is a partial copy, or copying the Application ID or", file=sys.stderr)
        print("Public Key from the General Information page instead of the token", file=sys.stderr)
        print("from the Bot page. Nothing written.", file=sys.stderr)
        sys.exit(1)
    return token


def ask_user_id() -> str:
    raw = input("\nYour Discord user ID: ").strip()
    if not raw.isdigit():
        sys.exit(f"A user ID is all digits; got {raw!r}. Nothing written.")
    if not 17 <= len(raw) <= 20:
        sys.exit(f"A user ID is 17-20 digits; got {len(raw)}. Nothing written.")
    return raw


def write(token: str, user_id: str) -> None:
    if not ENV_PATH.exists():
        sys.exit(f"No .env at {ENV_PATH}. Copy .env.example to .env first.")
    text = ENV_PATH.read_text()
    for key, value in (("DISCORD_BOT_TOKEN", token), ("OWNER_USER_ID", user_id)):
        pattern = rf"(?m)^{key}=.*$"
        if not re.search(pattern, text):
            sys.exit(f".env has no {key}= line to fill in. Nothing written.")
        # A lambda replacement, so nothing in the token is read as a backreference.
        text = re.sub(pattern, lambda _m, v=value, k=key: f"{k}={v}", text)
    ENV_PATH.write_text(text)
    print(f"\nWritten to {ENV_PATH}")
    print(f"  DISCORD_BOT_TOKEN = {token[:6]}…{token[-4:]}  ({len(token)} chars)")
    print(f"  OWNER_USER_ID     = {user_id}")


def verify(token: str) -> int:
    """Ask Discord who this token belongs to. Catches a truncated paste, which
    the config check cannot: that only sees a non-empty string."""
    try:
        import aiohttp, asyncio
    except ImportError:
        print("\n(skipping verification: aiohttp not installed)")
        return 0

    async def go() -> int:
        url = "https://discord.com/api/v10/users/@me"
        headers = {"Authorization": f"Bot {token}"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 401:
                    print("\nDiscord rejected the token (401).", file=sys.stderr)
                    print("Most likely a partial paste, or it was reset after copying.", file=sys.stderr)
                    return 1
                if response.status != 200:
                    print(f"\nDiscord returned HTTP {response.status}.", file=sys.stderr)
                    return 1
                me = await response.json()
                print(f"\nDiscord accepted it: bot is {me.get('username')} (id {me.get('id')})")
                return 0

    return asyncio.run(go())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="ask Discord whether the token works")
    args = parser.parse_args()

    token = ask_token()
    user_id = ask_user_id()
    write(token, user_id)
    return verify(token) if args.verify else 0


if __name__ == "__main__":
    sys.exit(main())
