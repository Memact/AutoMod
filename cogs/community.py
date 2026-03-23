from __future__ import annotations

import nextcord
from nextcord.ext import commands

from bot import MemactAutoModBot
from config import COMMAND_GUILD_IDS
from utils.checks import require_moderator
from utils.ui import build_embed, send_interaction


class CommunityCog(commands.Cog):
    def __init__(self, bot: MemactAutoModBot) -> None:
        self.bot = bot

    @nextcord.slash_command(
        description="Staff queue commands for reports and appeals",
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
        channel_id = config["report_channel_id"]
        if not channel_id:
            await send_interaction(interaction, content="Reports are not configured yet. Ask an admin to set `/config report_channel`.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(channel_id)
        if not isinstance(channel, nextcord.TextChannel):
            await send_interaction(interaction, content="The configured report channel could not be found.", ephemeral=True)
            return
        report_id = self.bot.db.add_report(
            interaction.guild.id,
            "report",
            interaction.user.id,
            member.id,
            reason,
            evidence_url=evidence_url or None,
        )
        embed = build_embed(
            "New Member Report",
            reason,
            fields=[
                ("Report ID", str(report_id), True),
                ("Reporter", interaction.user.mention, True),
                ("Target", member.mention, True),
                ("Evidence", evidence_url or "None", False),
            ],
        )
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
        channel_id = config["appeal_channel_id"]
        if not channel_id:
            await send_interaction(interaction, content="Appeals are not configured yet. Ask an admin to set `/config appeal_channel`.", ephemeral=True)
            return
        case = self.bot.db.get_case(interaction.guild.id, case_id)
        if case is None:
            await send_interaction(interaction, content="That case ID was not found.", ephemeral=True)
            return
        if case["user_id"] != interaction.user.id:
            await send_interaction(interaction, content="You can only appeal your own moderation cases.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(channel_id)
        if not isinstance(channel, nextcord.TextChannel):
            await send_interaction(interaction, content="The configured appeal channel could not be found.", ephemeral=True)
            return
        appeal_id = self.bot.db.add_report(
            interaction.guild.id,
            "appeal",
            interaction.user.id,
            case["user_id"],
            reason,
            case_id=case_id,
        )
        embed = build_embed(
            "New Case Appeal",
            reason,
            fields=[
                ("Appeal ID", str(appeal_id), True),
                ("Appealing User", interaction.user.mention, True),
                ("Case ID", str(case_id), True),
                ("Original Action", case["action"], True),
                ("Original Reason", case["reason"], False),
            ],
        )
        await channel.send(embed=embed)
        await send_interaction(interaction, embed=build_embed("Appeal Submitted", f"Your appeal for case #{case_id} has been sent to the moderators."))

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
