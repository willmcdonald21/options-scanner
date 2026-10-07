"""The Discord surface. Deliberately thin.

This module knows about Discord and nothing about trading: it routes messages
into `AlertPipeline`, applies the reaction it returns, posts the notifications,
and handles the four control commands. Every trading decision lives elsewhere.

Channel discipline, from the spec:
  * the **alerts channel** is input only. The bot reads messages there from the
    owner and nobody else, and never posts text there -- only a reaction, which
    is therefore the sole in-channel signal that a paste was picked up.
  * the **updates channel** is output, and the only place commands are obeyed.

Startup refuses to proceed unless it has actually verified it can read the
alerts channel and post to the updates channel. A bot that silently lacks
permission on one of them looks identical to a bot with nothing to do.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import discord

from options_scanner.config import Mode, Settings
from options_scanner.market_hours import describe as describe_session
from options_scanner.notifier import (
    REACTION_RECEIVED,
    Level,
    Notification,
    broker_disconnected,
    end_of_day,
    flatten_requested,
    halted,
    reconciliation,
    resumed,
    startup,
    status_report,
)
from options_scanner.pipeline import AlertPipeline
from options_scanner.position_manager import PositionManager
from options_scanner.risk import RiskGate
from options_scanner.rules import next_level
from options_scanner.storage import Storage
from options_scanner.webhook import WebhookError, WebhookNotifier, redact

logger = logging.getLogger("options_scanner.discord")

COMMAND_PREFIX = "!"
FLATTEN_CONFIRM_SECONDS = 60


class StartupError(RuntimeError):
    """A required channel is missing or unusable. Fatal by design."""


class SwiftAlertBot(discord.Client):
    def __init__(
        self,
        *,
        settings: Settings,
        storage: Storage,
        risk: RiskGate,
        pipeline: AlertPipeline,
        manager: PositionManager | None = None,
    ):
        intents = discord.Intents.default()
        # Required to read the text of pasted alerts. Without it every message
        # arrives with empty content, which looks exactly like an empty paste.
        intents.message_content = True
        super().__init__(intents=intents)

        self.settings = settings
        self.storage = storage
        self.risk = risk
        self.pipeline = pipeline
        self.manager = manager
        if manager is not None:
            # The manager posts its own notifications (trims, stop moves,
            # stale quotes) straight to the updates channel.
            manager._notify = self.post
        self._alerts_channel: discord.abc.Messageable | None = None
        self._updates_channel: discord.abc.Messageable | None = None
        self._flatten_pending_until: float = 0.0
        self._manager_task: asyncio.Task | None = None
        self._ready_once = False

        # Posting transport. A webhook needs no channel permissions, so the
        # bot's role can be misconfigured without silencing its reporting --
        # and with synthetic stops a lost warning is the dangerous failure.
        self._webhook: WebhookNotifier | None = None
        if settings.discord.posts_via_webhook:
            self._webhook = WebhookNotifier(
                settings.discord.updates_webhook_url,
                owner_user_id=settings.discord.owner_user_id,
            )

    # --- startup -----------------------------------------------------------

    async def on_ready(self) -> None:
        if self._ready_once:
            # Discord reconnects fire this again; the checks and the startup
            # post should not repeat.
            logger.info("reconnected as %s", self.user)
            return
        self._ready_once = True
        try:
            await self._verify_channels()
        except StartupError as exc:
            logger.error("startup checks failed: %s", exc)
            await self.close()
            raise

        open_positions = self.storage.open_positions()
        await self.post(
            startup(
                mode=self.settings.mode.value,
                broker=self.pipeline.broker.name if self.pipeline.broker else "none (dry run)",
                summary=self.settings.summary_line(),
                session=describe_session(),
                open_positions=len(open_positions),
            )
        )
        if self.storage.is_halted():
            await self.post(halted(reason=self.storage.halt_reason() or "halted before restart"))

        # Connect before reconciling: reconciliation compares against the
        # broker, so doing it on a dead session would report every position as
        # missing and raise a false alarm on every restart.
        if self.pipeline.broker is not None and not self.pipeline.broker.is_connected:
            try:
                await self.pipeline.broker.connect()
            except Exception as exc:
                logger.error("broker connect failed: %s", exc)
                await self.post(broker_disconnected(detail=str(exc)))
                return

        # Reconcile before the loop starts, so a position whose broker state
        # disagrees with ours is reported before anything acts on it.
        if self.manager is not None:
            lines, mismatched = await self.manager.reconcile()
            if lines or mismatched:
                await self.post(reconciliation(lines=lines, mismatched=mismatched))
            self._manager_task = asyncio.create_task(self.manager.run())
            logger.info("position manager loop started")

        logger.info("ready as %s (%s)", self.user, self.settings.summary_line())

    async def close(self) -> None:
        if self.manager is not None:
            self.manager.stop()
        if self._manager_task is not None:
            self._manager_task.cancel()
        if self._webhook is not None:
            await self._webhook.close()
        await super().close()

    async def _verify_channels(self) -> None:
        """Prove both channels work before accepting any alert."""
        alerts_id = self.settings.discord.alerts_channel_id
        updates_id = self.settings.discord.updates_channel_id

        alerts = self.get_channel(alerts_id) or await self._fetch(alerts_id)
        updates = self.get_channel(updates_id) or await self._fetch(updates_id)

        if alerts is None:
            raise StartupError(
                f"cannot see the alerts channel {alerts_id}. Check the ID and that the bot was "
                "invited with View Channels and Read Message History."
            )
        if updates is None:
            raise StartupError(
                f"cannot see the updates channel {updates_id}. Check the ID and that the bot was "
                "invited with View Channels and Send Messages."
            )

        # Reading history is the actual capability needed on the alerts
        # channel, so test that rather than assuming the permission bits.
        try:
            async for _ in alerts.history(limit=1):
                break
        except discord.Forbidden as exc:
            raise StartupError(
                f"no permission to read the alerts channel {alerts_id}: {exc}"
            ) from exc

        if self._webhook is not None:
            # A GET proves the webhook works and reveals its channel, without
            # leaving a "starting up" message behind on every restart.
            try:
                info = await self._webhook.validate()
            except WebhookError as exc:
                raise StartupError(str(exc)) from exc
            target = int(info.get("channel_id") or 0)
            if target and target != updates_id:
                raise StartupError(
                    f"UPDATES_WEBHOOK_URL posts to channel {target} but UPDATES_CHANNEL_ID is "
                    f"{updates_id}. Commands are read from the latter and updates would appear in "
                    "the former, so they must be the same channel."
                )
            logger.info("posting via webhook %s", redact(self.settings.discord.updates_webhook_url))
        else:
            try:
                probe = await updates.send("Starting up…")
                await probe.delete()
            except discord.Forbidden as exc:
                raise StartupError(
                    f"no permission to post in the updates channel {updates_id}: {exc}"
                ) from exc

        self._alerts_channel = alerts
        self._updates_channel = updates
        logger.info("channel checks passed (alerts=%s updates=%s)", alerts_id, updates_id)

    async def _fetch(self, channel_id: int):
        try:
            return await self.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("could not fetch channel %s: %s", channel_id, exc)
            return None

    # --- posting -----------------------------------------------------------

    async def post(self, note: Notification) -> None:
        """Send one notification to the updates channel, never anywhere else."""
        logger.info("notify: %s", note.to_text())

        if self.pipeline.dry_run and not note.title.startswith("[DRY RUN]"):
            # Label it on the card itself rather than only in the embed, so a
            # dry-run message can never be mistaken for a real fill.
            note = replace(note, title=f"[DRY RUN] {note.title}")

        if self._webhook is not None:
            await self._webhook.post(note)
            return

        if self._updates_channel is None:
            return
        content = f"<@{self.settings.discord.owner_user_id}>" if note.mentions_owner else None
        try:
            await self._updates_channel.send(content=content, embed=note.to_embed())
        except discord.HTTPException as exc:
            # Never let a failed notification take down the trading loop; the
            # log is the fallback record.
            logger.error("could not post notification %r: %s", note.title, exc)

    # --- message routing ---------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if self.user is not None and message.author.id == self.user.id:
            return

        if message.channel.id == self.settings.discord.alerts_channel_id:
            if message.author.id != self.settings.discord.owner_user_id:
                logger.debug("ignoring alerts-channel message from %s", message.author.id)
                return
            await self._handle_alert(message)
            return

        if message.channel.id == self.settings.discord.updates_channel_id:
            if message.author.id != self.settings.discord.owner_user_id:
                return
            if message.content.strip().startswith(COMMAND_PREFIX):
                await self._handle_command(message)

    async def _handle_alert(self, message: discord.Message) -> None:
        # React first. The reaction is the only in-channel evidence the paste
        # was picked up, so it has to land even if everything after it fails.
        await self._react(message, REACTION_RECEIVED)

        result = await self.pipeline.handle_paste(
            message_id=message.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            text=message.content,
            jump_url=message.jump_url,
        )

        for note in result.notifications:
            await self.post(note)
        await self._react(message, result.final_reaction)

    async def _react(self, message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException as exc:
            logger.warning("could not add reaction %s: %s", emoji, exc)

    # --- commands ----------------------------------------------------------

    async def _handle_command(self, message: discord.Message) -> None:
        parts = message.content.strip().split()
        command = parts[0].lower().lstrip(COMMAND_PREFIX)
        args = parts[1:]

        handlers = {
            "status": self._cmd_status,
            "halt": self._cmd_halt,
            "resume": self._cmd_resume,
            "flatten": self._cmd_flatten,
            "eod": self._cmd_eod,
        }
        handler = handlers.get(command)
        if handler is None:
            await self.post(
                Notification(
                    title=f"Unknown command `!{command}`",
                    level=Level.INFO,
                    description="Available: `!status` `!halt` `!resume` `!flatten` `!eod`",
                )
            )
            return
        await handler(args)

    async def _cmd_status(self, args: list[str]) -> None:
        lines = []
        live = (
            [(m.position_id, m.state) for m in self.manager.managed.values()]
            if self.manager is not None
            else self.storage.open_positions()
        )
        for _, state in live:
            upcoming = next_level(state, self.settings.rules())
            stop = f"${state.stop_price:.2f}" if state.stop_price else "**none**"
            nxt = f"+{upcoming[0]}% at ${upcoming[1]:.3f}" if upcoming else "ladder exhausted"
            lines.append(
                f"`{state.option.occ_symbol}`\n"
                f"  {state.remaining_qty}/{state.original_qty} left · entry ${state.entry_fill:.3f} · "
                f"peak ${state.peak_bid:.3f} ({state.gain_pct:+.1f}%)\n"
                f"  stop {stop} ({state.stop_reason or 'unprotected'}) · "
                f"trims done {sorted(state.fired_levels) or 'none'} · next {nxt}"
            )
        await self.post(
            status_report(
                mode=self.settings.mode.value,
                halted_now=self.storage.is_halted(),
                session=describe_session(),
                lines=lines,
                day=self.risk.day_summary(),
            )
        )

    async def _cmd_halt(self, args: list[str]) -> None:
        reason = " ".join(args) or "halted by command"
        self.risk.halt(reason)
        await self.post(halted(reason=reason))

    async def _cmd_resume(self, args: list[str]) -> None:
        self.risk.resume()
        await self.post(resumed())

    async def _cmd_flatten(self, args: list[str]) -> None:
        """Two-step on purpose: closing everything at market is not something
        to do on a mistyped message."""
        positions = [state.option.occ_symbol for _, state in self.storage.open_positions()]
        confirming = args and args[0].lower() == "confirm"
        loop_now = asyncio.get_running_loop().time()

        if not confirming:
            self._flatten_pending_until = loop_now + FLATTEN_CONFIRM_SECONDS
            await self.post(flatten_requested(positions=positions))
            return

        if loop_now > self._flatten_pending_until:
            await self.post(
                Notification(
                    title="Flatten confirmation expired",
                    level=Level.WARNING,
                    description="Run `!flatten` again to start over.",
                )
            )
            return

        self._flatten_pending_until = 0.0
        # Halt first. Flattening and then immediately re-entering on the next
        # alert would be the worst possible reading of the command.
        self.risk.halt("flattened by command")

        if self.manager is None:
            await self.post(
                Notification(
                    title="Nothing to flatten",
                    level=Level.WARNING,
                    description="New entries are halted, but there is no position manager running.",
                )
            )
            return

        await self.post(
            Notification(
                title="Flattening everything",
                level=Level.WARNING,
                description="New entries are halted. Closing at marketable limits now.",
                fields=[("Closing", "\n".join(f"`{p}`" for p in positions) or "nothing", False)],
            )
        )
        for note in await self.manager.flatten_all():
            await self.post(note)

    async def _cmd_eod(self, args: list[str]) -> None:
        await self.post(
            end_of_day(
                day=self.risk.day_summary(),
                open_positions=[s.option.occ_symbol for _, s in self.storage.open_positions()],
            )
        )


def confirm_live_interactively(settings: Settings) -> None:
    """Second gate on live trading. `live_confirmed: true` in the config is the
    first; this is a human typing the word at the moment of starting, so a
    config file left in the wrong state cannot by itself risk money."""
    if settings.mode is not Mode.LIVE:
        return
    banner = (
        "\n"
        "  ************************************************************\n"
        "  *  LIVE TRADING. Real money. Orders will be sent.          *\n"
        f"  *  {settings.summary_line()[:56]:<56}*\n"
        "  ************************************************************\n"
    )
    print(banner)
    answer = input("Type LIVE to continue, anything else aborts: ").strip()
    if answer != "LIVE":
        raise SystemExit("live trading not confirmed; aborting")
