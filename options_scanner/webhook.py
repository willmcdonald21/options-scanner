"""Posting to a Discord webhook.

An alternative transport for the updates channel. A webhook needs no channel
permissions at all, which means the bot's role can be misconfigured without
silencing its reporting -- and with synthetic stops, a lost warning is the
dangerous failure.

What a webhook cannot do is **read**. Alerts are read from the alerts channel
and commands from the updates channel, both of which still require the bot
token. This module only replaces the posting half.

Validation is a GET rather than a test post: a webhook URL answers with its own
metadata, so the startup check can prove the URL works without putting a
"starting up" message in the channel every restart.
"""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

logger = logging.getLogger("options_scanner.webhook")

# https://discord.com/api/webhooks/<id>/<token>
_WEBHOOK_RE = re.compile(
    r"^https://(?:\w+\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(?P<id>\d+)/(?P<token>[\w-]+)$"
)

_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Discord rate-limits webhooks per channel. A 429 carries retry_after; anything
# else is not worth retrying more than once.
_MAX_ATTEMPTS = 3


class WebhookError(RuntimeError):
    """The webhook is unusable. Raised only by validate(), so a failure to post
    can never take down the trading loop."""


def looks_like_webhook(url: str) -> bool:
    return bool(_WEBHOOK_RE.match((url or "").strip()))


def redact(url: str) -> str:
    """A webhook URL is a bearer credential: anyone holding it can post to the
    channel. Only the id is ever safe to log."""
    match = _WEBHOOK_RE.match((url or "").strip())
    if not match:
        return "(not a webhook url)"
    return f"https://discord.com/api/webhooks/{match.group('id')}/***"


class WebhookNotifier:
    """Posts notifications to one Discord webhook.

    Owns its own aiohttp session, created lazily so constructing one outside a
    running loop is harmless (which the config and tests both do).
    """

    def __init__(self, url: str, *, username: str = "options-scanner", owner_user_id: int | None = None):
        self.url = (url or "").strip()
        self.username = username
        self.owner_user_id = owner_user_id
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def validate(self) -> dict:
        """Prove the URL works, without posting. Returns the webhook metadata."""
        if not looks_like_webhook(self.url):
            raise WebhookError(
                "UPDATES_WEBHOOK_URL is not a Discord webhook URL. It should look like "
                "https://discord.com/api/webhooks/<id>/<token>"
            )
        session = await self._get_session()
        try:
            async with session.get(self.url) as response:
                if response.status == 404:
                    raise WebhookError(
                        f"webhook {redact(self.url)} does not exist (404). It may have been "
                        "deleted or regenerated."
                    )
                if response.status == 401:
                    raise WebhookError(f"webhook {redact(self.url)} rejected its token (401)")
                if response.status >= 400:
                    raise WebhookError(
                        f"webhook {redact(self.url)} returned HTTP {response.status}"
                    )
                data = await response.json()
        except aiohttp.ClientError as exc:
            raise WebhookError(f"could not reach {redact(self.url)}: {exc}") from exc

        logger.info(
            "webhook ok: %r posting to channel %s", data.get("name"), data.get("channel_id")
        )
        return data

    async def post(self, note) -> bool:
        """Send one notification. Returns whether it landed.

        Never raises. A failed notification must not take down the trading
        loop, and the log is the fallback record.
        """
        payload: dict = {
            "username": self.username,
            "embeds": [note.to_webhook_embed()],
        }
        if note.mentions_owner and self.owner_user_id:
            # Critical events have to generate a push notification, so the
            # mention goes in the message content and is explicitly allowed --
            # webhooks suppress mentions otherwise.
            payload["content"] = f"<@{self.owner_user_id}>"
            payload["allowed_mentions"] = {"users": [str(self.owner_user_id)]}

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                session = await self._get_session()
                async with session.post(self.url, json=payload) as response:
                    if response.status in (200, 204):
                        return True
                    if response.status == 429:
                        retry_after = 1.0
                        try:
                            body = await response.json()
                            retry_after = float(body.get("retry_after", 1.0))
                        except Exception:
                            pass
                        logger.warning(
                            "webhook rate-limited, retrying in %.1fs (attempt %s)",
                            retry_after, attempt,
                        )
                        await asyncio.sleep(min(retry_after, 5.0))
                        continue
                    body = (await response.text())[:200]
                    logger.error(
                        "webhook post failed: HTTP %s %s (notification: %s)",
                        response.status, body, note.to_text(),
                    )
                    return False
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("webhook post attempt %s failed: %s", attempt, exc)
                if attempt == _MAX_ATTEMPTS:
                    logger.error("giving up on notification: %s", note.to_text())
                    return False
                await asyncio.sleep(0.5 * attempt)
        return False
