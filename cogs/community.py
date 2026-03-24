from __future__ import annotations

from datetime import timedelta
import re

import nextcord
from nextcord.ext import commands

from bot import MemactAutoModBot
from config import (
    ABUSE_STRIKE_THRESHOLD,
    ABUSE_STRIKE_WINDOW_SECONDS,
    ABUSE_TIMEOUT_MINUTES,
    APPEAL_COOLDOWN_SECONDS,
    APPEAL_MIN_LENGTH,
    COMMAND_GUILD_IDS,
    DUPLICATE_WINDOW_SECONDS,
    REPORT_COOLDOWN_SECONDS,
    REPORT_MIN_LENGTH,
    TICKET_CHANNEL_ID,
    TICKET_COOLDOWN_SECONDS,
    TICKET_MIN_LENGTH,
)
from utils.checks import require_moderator
from utils.time import to_iso, utcnow
from utils.ui import build_embed, safe_dm, send_interaction


class CommunityCog(commands.Cog):
    def __init__(self, bot: MemactAutoModBot) -> None:
        self.bot = bot

    def _normalize_text(self, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text.lower()).strip()
        return cleaned

    def _is_low_effort(self, text: str, min_length: int) -> bool:
        stripped = text.strip()
        if len(stripped) < min_length:
            return True
        alnum = [ch.lower() for ch in stripped if ch.isalnum()]
        if len(alnum) >= max(6, min_length // 2) and len(set(alnum)) <= 2:
            return True
        return False

    def _resolve_ticket_channels(
        self,
        guild: nextcord.Guild,
        config_channel_id: int | None,
    ) -> list[nextcord.TextChannel]:
        channels: list[nextcord.TextChannel] = []
        ticket_channel = guild.get_channel(TICKET_CHANNEL_ID)
        if isinstance(ticket_channel, nextcord.TextChannel):
            channels.append(ticket_channel)
        if config_channel_id and config_channel_id != TICKET_CHANNEL_ID:
            configured = guild.get_channel(config_channel_id)
            if isinstance(configured, nextcord.TextChannel):
                channels.append(configured)
        return channels

    async def _handle_ticket_abuse(
        self,
        interaction: nextcord.Interaction,
        *,
        kind: str,
        message: str,
    ) -> None:
        if interaction.guild is None:
            return
        self.bot.db.add_ticket_abuse_event(
            interaction.guild.id,
            interaction.user.id,
            kind=kind,
            reason=message,
        )
        cutoff = to_iso(utcnow() - timedelta(seconds=ABUSE_STRIKE_WINDOW_SECONDS)) or ""
        strike_count = self.bot.db.count_recent_ticket_abuse_events(
            interaction.guild.id,
            interaction.user.id,
            since_iso=cutoff,
        )
        if strike_count >= ABUSE_STRIKE_THRESHOLD and isinstance(interaction.user, nextcord.Member):
            duration = timedelta(minutes=ABUSE_TIMEOUT_MINUTES)
            try:
                await interaction.user.edit(timeout=duration, reason="Ticket spam detected.")
            except (nextcord.Forbidden, nextcord.HTTPException):
                pass
            await self.bot.send_log(
                interaction.guild,
                title="Ticket Abuse Timeout",
                description=f"{interaction.user.mention} was timed out for ticket spam.",
                fields=[
                    ("Strikes", str(strike_count), True),
                    ("Window", f"{ABUSE_STRIKE_WINDOW_SECONDS}s", True),
                    ("Duration", f"{ABUSE_TIMEOUT_MINUTES}m", True),
                ],
            )
            await send_interaction(
                interaction,
                embed=build_embed(
                    "Ticket Blocked",
                    "You have been temporarily timed out for repeated ticket spam.",
                ),
            )
            return
        await send_interaction(
            interaction,
            embed=build_embed(
                "Ticket Blocked",
                message,
                fields=[("Strikes", f"{strike_count}/{ABUSE_STRIKE_THRESHOLD}", True)],
            ),
        )

    async def _enforce_ticket_policy(
        self,
        interaction: nextcord.Interaction,
        *,
        kind: str,
        text: str,
        target_id: int | None,
        case_id: int | None,
        evidence_url: str | None,
    ) -> bool:
        if interaction.guild is None:
            return False
        cooldown_seconds = {
            "report": REPORT_COOLDOWN_SECONDS,
            "appeal": APPEAL_COOLDOWN_SECONDS,
            "ticket": TICKET_COOLDOWN_SECONDS,
        }[kind]
        min_length = {
            "report": REPORT_MIN_LENGTH,
            "appeal": APPEAL_MIN_LENGTH,
            "ticket": TICKET_MIN_LENGTH,
        }[kind]

        if self._is_low_effort(text, min_length):
            await self._handle_ticket_abuse(
                interaction,
                kind=kind,
                message=f"Please provide more detail (minimum {min_length} characters).",
            )
            return False

        latest = self.bot.db.get_latest_report_by_author(
            interaction.guild.id,
            interaction.user.id,
            kind=kind,
        )
        if latest is not None:
            latest_time = latest.get("created_at")
            if latest_time:
                cutoff = utcnow() - timedelta(seconds=cooldown_seconds)
                if latest_time >= (to_iso(cutoff) or ""):
                    await self._handle_ticket_abuse(
                        interaction,
                        kind=kind,
                        message=f"Please wait {cooldown_seconds // 60} minutes before submitting another {kind}.",
                    )
                    return False

        cutoff = to_iso(utcnow() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)) or ""
        recent = self.bot.db.list_recent_reports_by_author(
            interaction.guild.id,
            interaction.user.id,
            kind=kind,
            since_iso=cutoff,
        )
        normalized_text = self._normalize_text(text)
        normalized_evidence = self._normalize_text(evidence_url or "")
        for entry in recent:
            if entry.get("target_id") != target_id:
                continue
            if entry.get("case_id") != case_id:
                continue
            if self._normalize_text(entry.get("reason", "")) != normalized_text:
                continue
            if self._normalize_text(entry.get("evidence_url") or "") != normalized_evidence:
                continue
            await self._handle_ticket_abuse(
                interaction,
                kind=kind,
                message=f"This {kind} looks like a duplicate of a recent submission.",
            )
            return False

        return True

    def _build_ticket_embed(
        self,
        *,
        title: str,
        description: str,
        ticket_id: int,
        kind: str,
        author: nextcord.abc.User,
        fields: list[tuple[str, str, bool]] | None = None,
    ) -> nextcord.Embed:
        base_fields = [
            ("Ticket ID", str(ticket_id), True),
            ("Type", kind, True),
            ("Author", author.mention, True),
        ]
        if fields:
            base_fields.extend(fields)
        return build_embed(title, description, fields=base_fields)

    @nextcord.slash_command(
        description="Staff queue commands for reports, appeals, and tickets",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_messages=True),
    )
    async def queue(self, interaction: nextcord.Interaction) -> None:
        pass

    @nextcord.slash_command(description="Report a member to the moderators", guild_ids=COMMAND_GUILD_IDS)
    async def report(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        reason: str,
        evidence_url: str = nextcord.SlashOption(required=False, default=""),
    ) -> None:
        if interaction.guild is None:
            await send_interaction(interaction, content="This command only works inside a server.", ephemeral=True)
            return
        config = self.bot.db.get_guild_config(interaction.guild.id)
        channels = self._resolve_ticket_channels(interaction.guild, config["report_channel_id"])
        if not channels:
            await send_interaction(interaction, content="The ticket channel could not be found.", ephemeral=True)
            return
        if not await self._enforce_ticket_policy(
            interaction,
            kind="report",
            text=reason,
            target_id=member.id,
            case_id=None,
            evidence_url=evidence_url or None,
        ):
            return
        report_id = self.bot.db.add_report(
            interaction.guild.id,
            "report",
            interaction.user.id,
            member.id,
            reason,
            evidence_url=evidence_url or None,
        )
        embed = self._build_ticket_embed(
            title="New Ticket",
            description=reason,
            ticket_id=report_id,
            kind="Report",
            author=interaction.user,
            fields=[
                ("Target", member.mention, True),
                ("Evidence", evidence_url or "None", False),
            ],
        )
        for channel in channels:
            await channel.send(embed=embed)
        await send_interaction(interaction, embed=build_embed("Report Submitted", f"Your report for {member.mention} has been sent to the moderators."))

    @nextcord.slash_command(description="Appeal a moderation case", guild_ids=COMMAND_GUILD_IDS)
    async def appeal(
        self,
        interaction: nextcord.Interaction,
        case_id: int,
        reason: str,
    ) -> None:
        if interaction.guild is None:
            await send_interaction(interaction, content="This command only works inside a server.", ephemeral=True)
            return
        config = self.bot.db.get_guild_config(interaction.guild.id)
        case = self.bot.db.get_case(interaction.guild.id, case_id)
        if case is None:
            await send_interaction(interaction, content="That case ID was not found.", ephemeral=True)
            return
        if case["user_id"] != interaction.user.id:
            await send_interaction(interaction, content="You can only appeal your own moderation cases.", ephemeral=True)
            return
        channels = self._resolve_ticket_channels(interaction.guild, config["appeal_channel_id"])
        if not channels:
            await send_interaction(interaction, content="The ticket channel could not be found.", ephemeral=True)
            return
        if not await self._enforce_ticket_policy(
            interaction,
            kind="appeal",
            text=reason,
            target_id=case["user_id"],
            case_id=case_id,
            evidence_url=None,
        ):
            return
        appeal_id = self.bot.db.add_report(
            interaction.guild.id,
            "appeal",
            interaction.user.id,
            case["user_id"],
            reason,
            case_id=case_id,
        )
        embed = self._build_ticket_embed(
            title="New Ticket",
            description=reason,
            ticket_id=appeal_id,
            kind="Appeal",
            author=interaction.user,
            fields=[
                ("Case ID", str(case_id), True),
                ("Original Action", case["action"], True),
                ("Original Reason", case["reason"], False),
            ],
        )
        for channel in channels:
            await channel.send(embed=embed)
        await send_interaction(interaction, embed=build_embed("Appeal Submitted", f"Your appeal for case #{case_id} has been sent to the moderators."))

    @nextcord.slash_command(name="raise", description="Raise a ticket for the moderators", guild_ids=COMMAND_GUILD_IDS)
    async def raise_ticket(
        self,
        interaction: nextcord.Interaction,
        subject: str,
        details: str,
        evidence_url: str = nextcord.SlashOption(required=False, default=""),
    ) -> None:
        if interaction.guild is None:
            await send_interaction(interaction, content="This command only works inside a server.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(TICKET_CHANNEL_ID)
        if not isinstance(channel, nextcord.TextChannel):
            await send_interaction(interaction, content="The ticket channel could not be found.", ephemeral=True)
            return
        if not await self._enforce_ticket_policy(
            interaction,
            kind="ticket",
            text=details,
            target_id=None,
            case_id=None,
            evidence_url=evidence_url or None,
        ):
            return
        ticket_id = self.bot.db.add_report(
            interaction.guild.id,
            "ticket",
            interaction.user.id,
            None,
            details,
            evidence_url=evidence_url or None,
        )
        embed = self._build_ticket_embed(
            title="New Ticket",
            description=details,
            ticket_id=ticket_id,
            kind="Raise",
            author=interaction.user,
            fields=[
                ("Subject", subject, True),
                ("Evidence", evidence_url or "None", False),
            ],
        )
        await channel.send(embed=embed)
        confirmation = build_embed(
            "Ticket Submitted",
            f"Your ticket `{ticket_id}` has been sent to the moderators.",
            fields=[("Subject", subject, True)],
        )
        await safe_dm(interaction.user, embed=confirmation)
        await send_interaction(interaction, embed=confirmation)

    @queue.subcommand(description="View recent report and appeal queue entries")
    async def view(
        self,
        interaction: nextcord.Interaction,
        kind: str = nextcord.SlashOption(
            required=False,
            default="all",
            choices={
                "All": "all",
                "Reports": "report",
                "Appeals": "appeal",
                "Tickets": "ticket",
            },
        ),
        status: str = nextcord.SlashOption(
            required=False,
            default="open",
            choices={
                "Open": "open",
                "Resolved": "resolved",
                "Closed": "closed",
                "All": "all",
            },
        ),
        limit: int = nextcord.SlashOption(required=False, default=10, min_value=1, max_value=25),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        entries = self.bot.db.list_reports(
            interaction.guild.id,
            kind=None if kind == "all" else kind,
            status=None if status == "all" else status,
            limit=limit,
        )
        if not entries:
            await send_interaction(interaction, embed=build_embed("Queue", "No queue entries matched that filter."))
            return
        lines = []
        for entry in entries:
            target = f"user `{entry['target_id']}`" if entry["target_id"] else "no target"
            lines.append(
                f"#{entry['id']} | {entry['kind']} | {entry['status']} | author `{entry['author_id']}` | {target}"
            )
        await send_interaction(
            interaction,
            embed=build_embed(
                "Queue Entries",
                "\n".join(lines),
                fields=[
                    ("Kind", kind, True),
                    ("Status", status, True),
                    ("Shown", str(len(entries)), True),
                ],
            ),
        )

    @queue.subcommand(description="Resolve or close a report or appeal entry")
    async def resolve(
        self,
        interaction: nextcord.Interaction,
        entry_id: int,
        status: str = nextcord.SlashOption(
            required=False,
            default="resolved",
            choices={
                "Resolved": "resolved",
                "Closed": "closed",
            },
        ),
        note: str = nextcord.SlashOption(required=False, default="Handled by staff."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        entry = self.bot.db.get_report(interaction.guild.id, entry_id)
        if entry is None:
            await send_interaction(interaction, content="That queue entry was not found.", ephemeral=True)
            return
        if not self.bot.db.update_report_status(interaction.guild.id, entry_id, status):
            await send_interaction(interaction, content="That queue entry could not be updated.", ephemeral=True)
            return
        await self.bot.send_log(
            interaction.guild,
            title="Queue Entry Updated",
            description=f"Queue entry #{entry_id} was marked as `{status}`.",
            fields=[
                ("Kind", entry["kind"], True),
                ("Moderator", moderator.mention, True),
                ("Note", note, False),
            ],
        )
        await send_interaction(
            interaction,
            embed=build_embed(
                "Queue Updated",
                f"Marked {entry['kind']} entry #{entry_id} as `{status}`.",
                fields=[("Note", note, False)],
            ),
        )


def setup(bot: MemactAutoModBot) -> None:
    bot.add_cog(CommunityCog(bot))
