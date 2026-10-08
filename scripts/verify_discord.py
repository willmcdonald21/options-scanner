"""Prove the Discord side actually works, before trusting it with alerts.

Four things fail silently and are each checked here:

* a truncated token -- looks fine to the config check, fails at connect with a
  bare 401
* the MESSAGE CONTENT INTENT left off -- every pasted alert then arrives with an
  empty body, which is indistinguishable from pasting nothing
* the bot not being able to read the alerts channel -- it would simply never
  react to anything
* the wrong OWNER_USER_ID -- a server or channel id pasted by mistake means the
  owner filter rejects every alert you send

Read-only: posts nothing, changes nothing.

Usage:
    .venv/bin/python scripts/verify_discord.py
"""

from __future__ import annotations

import asyncio
import sys

import aiohttp
from dotenv import load_dotenv

from options_scanner.config import PROJECT_ROOT

API = "https://discord.com/api/v10"

# Application flags carrying the message-content intent. Unverified apps that
# toggle it in the portal get the LIMITED flag; approved ones get the other.
GATEWAY_MESSAGE_CONTENT = 1 << 18
GATEWAY_MESSAGE_CONTENT_LIMITED = 1 << 19

# Exactly what the bot needs, and nothing more. Output goes out through the
# updates webhook, so Send Messages is deliberately absent.
PERMS = {
    "Add Reactions": 1 << 6,          # the eyes/check/cross on each alert
    "View Channels": 1 << 10,         # see both channels
    "Read Message History": 1 << 16,  # read pasted alerts and typed commands
}
INVITE_PERMISSIONS = sum(PERMS.values())

FAILS: list[str] = []


def ok(msg: str) -> None:
    print(f"  [ ok ] {msg}")


def bad(msg: str, fix: str = "") -> None:
    print(f"  [FAIL] {msg}")
    if fix:
        print(f"         fix: {fix}")
    FAILS.append(msg)


async def get(session, path: str):
    async with session.get(f"{API}{path}") as response:
        body = None
        try:
            body = await response.json()
        except Exception:
            pass
        return response.status, body


async def run() -> int:
    import os

    load_dotenv(PROJECT_ROOT / ".env")
    token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    owner = (os.environ.get("OWNER_USER_ID") or "").strip()
    alerts = (os.environ.get("ALERTS_CHANNEL_ID") or "").strip()
    updates = (os.environ.get("UPDATES_CHANNEL_ID") or "").strip()
    if not token:
        print("DISCORD_BOT_TOKEN is empty; run scripts/set_discord_creds.py")
        return 1

    headers = {"Authorization": f"Bot {token}"}
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        print("\nToken")
        status, me = await get(session, "/users/@me")
        if status == 401:
            bad("Discord rejected the token (401)",
                "usually a partial paste; re-run scripts/set_discord_creds.py")
            return 1
        if status != 200:
            bad(f"/users/@me returned HTTP {status}")
            return 1
        ok(f"accepted -- bot is {me.get('username')} (id {me.get('id')})")

        print("\nMessage Content intent")
        status, app = await get(session, "/applications/@me")
        if status != 200:
            bad(f"could not read the application ({status}); check the intent by eye")
        else:
            flags = int(app.get("flags") or 0)
            if flags & (GATEWAY_MESSAGE_CONTENT | GATEWAY_MESSAGE_CONTENT_LIMITED):
                ok("enabled -- pasted alerts will arrive with their text")
            else:
                bad("MESSAGE CONTENT INTENT is OFF -- every paste would arrive empty",
                    "Developer Portal -> your app -> Bot -> Privileged Gateway Intents "
                    "-> MESSAGE CONTENT INTENT on -> Save Changes")

        print("\nServer membership")
        status, guilds = await get(session, "/users/@me/guilds")
        if status != 200:
            bad(f"could not list the bot's servers (HTTP {status})")
        elif not guilds:
            # Everything else about the bot can be perfect and it still cannot
            # see a single message until it has been invited to the server.
            app_id = (app or {}).get("id") or me.get("id")
            invite = (
                f"https://discord.com/oauth2/authorize?client_id={app_id}"
                f"&scope=bot&permissions={INVITE_PERMISSIONS}"
            )
            bad("the bot is not in ANY server -- it cannot see a single message",
                "open this link and pick your server:\n         " + invite)
            print("         (grants only: " + ", ".join(PERMS) + ")")
        else:
            ok("in " + ", ".join(f"{g['name']}" for g in guilds))

        print("\nChannels")
        for label, channel_id, need_history in (
            ("alerts", alerts, True),
            ("updates", updates, True),
        ):
            if not channel_id:
                bad(f"{label} channel id is not set")
                continue
            status, channel = await get(session, f"/channels/{channel_id}")
            if status == 403:
                bad(f"cannot see the {label} channel (403)",
                    f"invite the bot to that channel: Edit Channel -> Permissions -> add the bot "
                    f"with View Channel")
                continue
            if status == 404:
                bad(f"{label} channel {channel_id} does not exist (404)", "check the id")
                continue
            if status != 200:
                bad(f"{label} channel returned HTTP {status}")
                continue
            ok(f"{label}: #{channel.get('name')} visible")

            if need_history:
                status, _ = await get(session, f"/channels/{channel_id}/messages?limit=1")
                if status == 200:
                    ok(f"{label}: can read history")
                elif status == 403:
                    bad(f"{label}: cannot read message history (403)",
                        "Edit Channel -> Permissions -> give the bot Read Message History")
                else:
                    bad(f"{label}: reading history returned HTTP {status}")

        print("\nOwner")
        if not owner.isdigit():
            bad(f"OWNER_USER_ID {owner!r} is not numeric")
        else:
            status, user = await get(session, f"/users/{owner}")
            if status == 200:
                ok(f"OWNER_USER_ID is {user.get('username')} -- alerts from this user are accepted")
                if user.get("bot"):
                    bad("that id belongs to a bot, not to you")
            elif status == 404:
                bad(f"no Discord user with id {owner}",
                    "this is usually a server or channel id by mistake; right-click your own "
                    "name -> Copy User ID")
            else:
                bad(f"/users/{owner} returned HTTP {status}")

    print()
    if FAILS:
        print(f"{len(FAILS)} problem(s) -- the bot will not work correctly until they are fixed\n")
        return 1
    print("Discord side is ready.\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
