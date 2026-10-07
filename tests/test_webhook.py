"""The webhook transport, and the card shape it sends.

No network: a fake aiohttp session records what would have been POSTed.
"""

from datetime import date

import pytest

from options_scanner.models import OptionKey
from options_scanner.notifier import (
    Level,
    Notification,
    contract_label,
    contract_sentence,
    entry_filled,
    ladder_block,
    stop_moved,
    stopped_out,
    trim_executed,
)
from options_scanner.webhook import WebhookError, WebhookNotifier, looks_like_webhook, redact

OPT = OptionKey("SPX", date(2026, 10, 6), 7815.0, "P")
TODAY = date(2026, 10, 6)
URL = "https://discord.com/api/webhooks/1552390209428262974/RcnNKpSjkazV-hnoaEnhvDqmssp99d0n4"


class FakeResponse:
    def __init__(self, status=204, body=None, text=""):
        self.status = status
        self._body = body if body is not None else {}
        self._text = text

    async def json(self):
        return self._body

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, get=None, posts=None):
        self._get = get or FakeResponse(200, {"id": "1", "name": "Options Update",
                                              "channel_id": "1552390101680525395"})
        self._posts = list(posts or [FakeResponse(204)])
        self.posted: list[dict] = []
        self.closed = False

    def get(self, url):
        return self._get

    def post(self, url, json=None):
        self.posted.append(json)
        return self._posts.pop(0) if self._posts else FakeResponse(204)

    async def close(self):
        self.closed = True


def notifier(session, **kwargs) -> WebhookNotifier:
    n = WebhookNotifier(URL, **kwargs)
    n._session = session
    return n


# --- URL handling ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        URL,
        "https://discordapp.com/api/webhooks/123/abc-DEF_ghi",
        "https://discord.com/api/v10/webhooks/123/abc",
    ],
)
def test_real_webhook_urls_are_recognized(url):
    assert looks_like_webhook(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "https://discord.com/channels/1/2/3",          # a channel link, not a webhook
        "https://evil.example.com/api/webhooks/1/abc",  # wrong host
        "https://discord.com/api/webhooks/abc/def",     # non-numeric id
    ],
)
def test_anything_else_is_rejected(url):
    assert looks_like_webhook(url) is False


def test_the_token_is_never_logged():
    """A webhook URL is a bearer credential: anyone holding it can post."""
    shown = redact(URL)
    assert "1552390209428262974" in shown
    assert "RcnNKpSjkazV" not in shown
    assert shown.endswith("***")


def test_redacting_a_non_webhook_says_so():
    assert redact("nonsense") == "(not a webhook url)"


# --- validation -----------------------------------------------------------


async def test_validation_uses_a_get_and_posts_nothing():
    """Otherwise every restart leaves a "starting up" message behind."""
    session = FakeSession()
    info = await notifier(session).validate()

    assert info["channel_id"] == "1552390101680525395"
    assert session.posted == []


async def test_a_malformed_url_fails_before_any_request():
    bad = WebhookNotifier("https://discord.com/channels/1/2/3")
    with pytest.raises(WebhookError, match="not a Discord webhook URL"):
        await bad.validate()


@pytest.mark.parametrize(
    "status,needle",
    [(404, "does not exist"), (401, "rejected its token"), (500, "HTTP 500")],
)
async def test_an_unusable_webhook_names_the_problem(status, needle):
    session = FakeSession(get=FakeResponse(status))
    with pytest.raises(WebhookError, match=needle):
        await notifier(session).validate()


# --- posting --------------------------------------------------------------


async def test_a_notification_is_posted_as_an_embed():
    session = FakeSession()
    note = Notification(title="TRIM +25% — SPX 7815P · 0DTE", description="Sold 6 of 21")
    note.add("Entry", "$0.48")

    assert await notifier(session).post(note) is True

    (payload,) = session.posted
    embed = payload["embeds"][0]
    assert embed["title"] == "TRIM +25% — SPX 7815P · 0DTE"
    assert embed["description"] == "Sold 6 of 21"
    assert {"name": "Entry", "value": "$0.48", "inline": True} in embed["fields"]
    assert "options-scanner" in embed["footer"]["text"]


async def test_a_critical_notification_mentions_the_owner_and_allows_it():
    """Webhooks suppress mentions unless explicitly allowed, and a critical
    event has to generate a push notification."""
    session = FakeSession()
    note = Notification(title="Quotes are stale", level=Level.CRITICAL)

    await notifier(session, owner_user_id=3003).post(note)

    (payload,) = session.posted
    assert payload["content"] == "<@3003>"
    assert payload["allowed_mentions"] == {"users": ["3003"]}


async def test_routine_notifications_do_not_mention_anyone():
    session = FakeSession()
    await notifier(session, owner_user_id=3003).post(Notification(title="TRIM +25%", level=Level.SUCCESS))

    assert "content" not in session.posted[0]


async def test_a_rejected_post_is_reported_but_never_raises():
    """A failed notification must not take down the trading loop."""
    session = FakeSession(posts=[FakeResponse(400, text="bad embed")])

    assert await notifier(session).post(Notification(title="x")) is False


async def test_a_rate_limit_is_retried():
    session = FakeSession(posts=[FakeResponse(429, {"retry_after": 0.01}), FakeResponse(204)])

    assert await notifier(session).post(Notification(title="x")) is True
    assert len(session.posted) == 2


async def test_a_transport_error_is_retried_then_given_up_on():
    import aiohttp

    class Broken(FakeSession):
        def post(self, url, json=None):
            self.posted.append(json)
            raise aiohttp.ClientError("connection reset")

    session = Broken()
    assert await notifier(session).post(Notification(title="x")) is False
    assert len(session.posted) == 3  # _MAX_ATTEMPTS


async def test_closing_closes_the_session():
    session = FakeSession()
    n = notifier(session)
    await n.close()
    assert session.closed is True


# --- the card shape ------------------------------------------------------


def test_the_title_matches_the_advisors_shorthand():
    assert contract_label(OPT, TODAY) == "SPX 7815P · 0DTE"
    assert contract_label(OPT, date(2026, 10, 1)) == "SPX 7815P · Oct 6"


def test_the_label_uses_the_ticker_not_the_trading_class():
    """The advisor writes SPX even though the contract trades as SPXW."""
    assert contract_label(OPT, TODAY).startswith("SPX ")
    assert "SPXW" not in contract_label(OPT, TODAY)


def test_the_description_matches_the_advisors_contract_line():
    assert contract_sentence(OPT) == "Entered SPX Oct06 '26 7815 Put"
    assert contract_sentence(OPT, "Fully out of") == "Fully out of SPX Oct06 '26 7815 Put"


def test_the_ladder_block_matches_the_advisors_rows():
    assert ladder_block([(25, 0.594), (100, 0.95)]) == "25%   $0.594\n100%  $0.95"


def test_the_entry_card_reads_like_a_buy_card():
    note = entry_filled(
        option=OPT, qty=21, fill_price=0.48, cost=1008.0, jump_url=None,
        levels=[(25, 0.60), (50, 0.72)], today=TODAY,
    )
    values = {name: value for name, value, _ in note.fields}

    assert note.title == "BUY — SPX 7815P · 0DTE"
    assert note.description == "Entered SPX Oct06 '26 7815 Put"
    assert values["Entry"] == "$0.48"
    assert values["Contracts"] == "21"
    assert values["Cost"] == "$1,008.00"
    assert values["Trim Targets"] == "25%   $0.60\n50%   $0.72"


def test_the_trim_card_reads_like_a_trim_card():
    note = trim_executed(
        option=OPT, level_pct=25, qty=6, fill_price=0.62, entry_price=0.48,
        remaining=15, original_qty=21, realized=84.0,
        stop_note="Stop moved to break-even at 0.48 — the remaining 15 can no longer lose money.",
        today=TODAY,
    )
    values = {name: value for name, value, _ in note.fields}

    assert note.title == "TRIM +25% — SPX 7815P · 0DTE"
    assert note.description.startswith("Sold 6 of 21 @ $0.62 · 15 still running.")
    assert "break-even" in note.description
    assert values["Entry"] == "$0.48"
    assert values["Exit"] == "$0.62"
    assert values["Locked In"] == "+$84.00"


def test_the_exit_card_reads_like_a_sold_all_card():
    note = stopped_out(
        option=OPT, qty=8, stop_price=0.85, fill_price=0.80, entry_price=0.48,
        peak_price=1.40, realized=256.0, pnl_pct=66.7, today=TODAY,
    )
    values = {name: value for name, value, _ in note.fields}

    assert note.title == "SOLD ALL +66.7% — SPX 7815P · 0DTE"
    assert note.description == "Fully out of SPX Oct06 '26 7815 Put."
    assert values["Entry → avg exit"] == "$0.48 → $0.80"
    assert values["Realized"] == "+$256.00"
    assert values["Peak"] == "+191.7% ($1.40)"


def test_the_stop_card_names_the_level_in_its_title():
    note = stop_moved(option=OPT, old=0.48, new=0.62, reason="trail", remaining=8, today=TODAY)

    assert note.title == "STOP → $0.62 — SPX 7815P · 0DTE"
    assert "ratcheted" in note.description


def test_a_losing_exit_card_is_red():
    note = stopped_out(
        option=OPT, qty=8, stop_price=0.40, fill_price=0.39, entry_price=0.48,
        realized=-72.0, pnl_pct=-18.8, today=TODAY,
    )
    assert note.level is Level.ERROR
    assert note.title.startswith("SOLD ALL -18.8%")
    assert {n: v for n, v, _ in note.fields}["Realized"] == "-$72.00"
