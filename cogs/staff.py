from __future__ import annotations

from datetime import timedelta
from typing import Optional

import nextcord
from nextcord.ext import commands

from bot import MemactAutoModBot
from config import COMMAND_GUILD_IDS
from utils.checks import require_moderator
from utils.time import format_timedelta, parse_duration, to_iso, utcnow
from utils.ui import build_embed, send_interaction


class StaffCog(commands.Cog):
    def __init__(self, bot: MemactAutoModBot) -> None:
        self.bot = bot

    async def _can_touch(self, actor: nextcord.Member, target: nextcord.Member) -> bool:
        if actor == target or target == target.guild.owner:
            return False
        if actor.guild_permissions.administrator:
            return True
        return actor.top_role > target.top_role

    async def _require_target(self, interaction: nextcord.Interaction, target: nextcord.Member) -> nextcord.Member | None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return None
        if not await self._can_touch(moderator, target):
            await send_interaction(interaction, content="You can't moderate that member because of role hierarchy or ownership.", ephemeral=True)
            return None
        return moderator

    @nextcord.slash_command(
        description="Compact staff moderation console",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_messages=True),
    )
    async def staff(self, interaction: nextcord.Interaction) -> None:
        pass

    @staff.subcommand(description="Kick a member")
    async def kick(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        reason: str = nextcord.SlashOption(required=False, default="No reason provided."),
    ) -> None:
        moderator = await self._require_target(interaction, member)
        if moderator is None:
            return
        if not member.kickable:
            await send_interaction(interaction, content="I can't kick that member. Check my role position.", ephemeral=True)
            return
        await member.kick(reason=reason)
        case_id = await self.bot.add_case(interaction.guild.id, member.id, moderator.id, "kick", reason)
        await self.bot.dm_case_notice(member, action="Kick", guild_name=interaction.guild.name, reason=reason, case_id=case_id)
        await self.bot.send_log(
            interaction.guild,
            title="Member Kicked",
            description=f"{member.mention} was kicked.",
            fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True), ("Reason", reason, False)],
        )
        await send_interaction(interaction, embed=build_embed("Kick Complete", f"Kicked {member.mention}.", fields=[("Case", str(case_id), True)]))

    @staff.subcommand(description="Ban a member")
    async def ban(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        reason: str = nextcord.SlashOption(required=False, default="No reason provided."),
        delete_message_hours: int = nextcord.SlashOption(required=False, default=0, min_value=0, max_value=168),
        duration: Optional[str] = nextcord.SlashOption(required=False, description="Optional tempban like 7d or 12h"),
    ) -> None:
        moderator = await self._require_target(interaction, member)
        if moderator is None:
            return
        parsed_duration = parse_duration(duration) if duration else None
        if duration and parsed_duration is None:
            await send_interaction(interaction, content="Duration must look like `30m`, `12h`, `7d`, or `1w`.", ephemeral=True)
            return
        expires_at = to_iso(utcnow() + parsed_duration) if parsed_duration else None
        await member.ban(reason=reason, delete_message_seconds=delete_message_hours * 3600)
        case_id = await self.bot.add_case(
            interaction.guild.id,
            member.id,
            moderator.id,
            "ban",
            reason,
            expires_at=expires_at,
            metadata={"temporary": bool(parsed_duration)},
        )
        if expires_at is not None:
            self.bot.db.schedule_action(interaction.guild.id, member.id, "unban", expires_at, {"reason": f"Temporary ban expired for case #{case_id}."})
        await self.bot.dm_case_notice(member, action="Ban", guild_name=interaction.guild.name, reason=reason, case_id=case_id, duration=parsed_duration)
        fields = [("Case", str(case_id), True), ("Moderator", moderator.mention, True), ("Reason", reason, False)]
        if parsed_duration is not None:
            fields.append(("Duration", format_timedelta(parsed_duration), True))
        await self.bot.send_log(interaction.guild, title="Member Banned", description=f"{member.mention} was banned.", fields=fields)
        await send_interaction(interaction, embed=build_embed("Ban Complete", f"Banned {member.mention}.", fields=fields))

    @staff.subcommand(description="Timeout a member")
    async def timeout(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        duration: str = nextcord.SlashOption(description="Example: 30m, 6h, 2d"),
        reason: str = nextcord.SlashOption(required=False, default="No reason provided."),
    ) -> None:
        moderator = await self._require_target(interaction, member)
        if moderator is None:
            return
        parsed_duration = parse_duration(duration)
        if parsed_duration is None:
            await send_interaction(interaction, content="Duration must look like `30m`, `6h`, or `2d`.", ephemeral=True)
            return
        if parsed_duration.total_seconds() > 28 * 24 * 3600:
            await send_interaction(interaction, content="Discord timeouts cannot exceed 28 days.", ephemeral=True)
            return
        await member.edit(timeout=parsed_duration, reason=reason)
        case_id = await self.bot.add_case(
            interaction.guild.id,
            member.id,
            moderator.id,
            "timeout",
            reason,
            expires_at=to_iso(utcnow() + parsed_duration),
            metadata={"duration": duration},
        )
        await self.bot.dm_case_notice(member, action="Timeout", guild_name=interaction.guild.name, reason=reason, case_id=case_id, duration=parsed_duration)
        await self.bot.send_log(
            interaction.guild,
            title="Member Timed Out",
            description=f"{member.mention} was timed out.",
            fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True), ("Duration", format_timedelta(parsed_duration), True), ("Reason", reason, False)],
        )
        await send_interaction(interaction, embed=build_embed("Timeout Complete", f"Timed out {member.mention}.", fields=[("Case", str(case_id), True)]))

    @staff.subcommand(description="Remove a member timeout")
    async def untimeout(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        reason: str = nextcord.SlashOption(required=False, default="Timeout removed by moderator."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        await member.edit(timeout=None, reason=reason)
        case_id = await self.bot.add_case(interaction.guild.id, member.id, moderator.id, "untimeout", reason, active=False)
        await self.bot.send_log(interaction.guild, title="Timeout Removed", description=f"{member.mention} had their timeout removed.", fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True)])
        await send_interaction(interaction, embed=build_embed("Timeout Removed", f"Removed timeout from {member.mention}.", fields=[("Case", str(case_id), True)]))

    @staff.subcommand(description="Warn a member manually")
    async def warn(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        points: int = nextcord.SlashOption(required=False, default=1, min_value=1, max_value=10),
        reason: str = nextcord.SlashOption(required=False, default="No reason provided."),
    ) -> None:
        moderator = await self._require_target(interaction, member)
        if moderator is None:
            return
        case_id, total_points, escalation = await self.bot.apply_warning(interaction.guild, member, moderator=moderator, reason=reason, points=points, source="manual")
        lines = [f"Warned {member.mention}.", f"Case #{case_id}.", f"Active warning points: {total_points}."]
        if escalation:
            lines.append(f"Automatic escalation: `{escalation}`.")
        await send_interaction(interaction, embed=build_embed("Warning Logged", "\n".join(lines)))

    @staff.subcommand(description="Show active warning points")
    async def warnings(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        limit: int = nextcord.SlashOption(required=False, default=10, min_value=1, max_value=20),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        warnings = self.bot.db.list_active_warning_cases(interaction.guild.id, member.id, limit=limit)
        total_points = self.bot.db.get_active_warning_points(interaction.guild.id, member.id)
        if not warnings:
            await send_interaction(interaction, embed=build_embed("Active Warnings", f"{member.mention} has no active warning points."))
            return
        lines = [f"#{item['id']} | {item['points']} point(s) | {item['reason']}" for item in warnings]
        await send_interaction(interaction, embed=build_embed(f"Warnings for {member}", "\n".join(lines), fields=[("Active Points", str(total_points), True)]))

    @staff.subcommand(description="Revoke the latest active warning")
    async def unwarn_latest(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        reason: str = nextcord.SlashOption(required=False, default="Latest warning revoked."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        warning = self.bot.db.deactivate_latest_warning_for_member(interaction.guild.id, member.id)
        if warning is None:
            await send_interaction(interaction, content=f"{member.mention} has no active warnings to revoke.", ephemeral=True)
            return
        audit_case_id = await self.bot.add_case(interaction.guild.id, member.id, moderator.id, "unwarn", reason, active=False, metadata={"target_case": warning["id"], "mode": "latest"})
        total_points = self.bot.db.get_active_warning_points(interaction.guild.id, member.id)
        await self.bot.send_log(
            interaction.guild,
            title="Latest Warning Revoked",
            description=f"The latest active warning for {member.mention} was revoked.",
            fields=[("Revoked Warning", str(warning["id"]), True), ("Audit Case", str(audit_case_id), True), ("Active Points Now", str(total_points), True), ("Reason", reason, False)],
        )
        await send_interaction(interaction, embed=build_embed("Latest Warning Revoked", f"Revoked warning #{warning['id']} for {member.mention}.", fields=[("Active Points Now", str(total_points), True)]))

    @staff.subcommand(description="Clear all active warnings")
    async def clearwarns(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        reason: str = nextcord.SlashOption(required=False, default="Warnings cleared by moderator."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        cleared = self.bot.db.clear_active_warnings_for_member(interaction.guild.id, member.id)
        audit_case_id = await self.bot.add_case(interaction.guild.id, member.id, moderator.id, "clearwarns", reason, active=False, metadata={"cleared": cleared})
        await self.bot.send_log(interaction.guild, title="Warnings Cleared", description=f"Cleared {cleared} active warnings for {member.mention}.", fields=[("Audit Case", str(audit_case_id), True), ("Moderator", moderator.mention, True), ("Reason", reason, False)])
        await send_interaction(interaction, embed=build_embed("Warnings Cleared", f"Cleared `{cleared}` active warnings for {member.mention}."))

    @staff.subcommand(description="Bulk delete recent messages")
    async def purge(
        self,
        interaction: nextcord.Interaction,
        amount: int = nextcord.SlashOption(min_value=1, max_value=100),
        member: Optional[nextcord.Member] = nextcord.SlashOption(required=False, description="Only delete messages from this member"),
        reason: str = nextcord.SlashOption(required=False, default="Message cleanup"),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, nextcord.TextChannel):
            await interaction.followup.send("This command only works in text channels.", ephemeral=True)
            return
        deleted = await channel.purge(limit=amount, check=lambda message: member is None or message.author.id == member.id, bulk=True)
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "purge", reason, active=False, metadata={"deleted": len(deleted), "channel_id": channel.id})
        await self.bot.send_log(interaction.guild, title="Messages Purged", description=f"{moderator.mention} purged messages in {channel.mention}.", fields=[("Case", str(case_id), True), ("Deleted", str(len(deleted)), True), ("Reason", reason, False)])
        await interaction.followup.send(embed=build_embed("Purge Complete", f"Deleted `{len(deleted)}` messages.", fields=[("Case", str(case_id), True)]), ephemeral=True)

    @staff.subcommand(description="Set slowmode on a channel")
    async def slowmode(
        self,
        interaction: nextcord.Interaction,
        seconds: int = nextcord.SlashOption(min_value=0, max_value=21600),
        channel: Optional[nextcord.TextChannel] = nextcord.SlashOption(required=False),
        reason: str = nextcord.SlashOption(required=False, default="Slowmode updated."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, nextcord.TextChannel):
            await send_interaction(interaction, content="Please run this in or target a text channel.", ephemeral=True)
            return
        await target_channel.edit(slowmode_delay=seconds, reason=reason)
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "slowmode", reason, active=False, metadata={"channel_id": target_channel.id, "seconds": seconds})
        await self.bot.send_log(interaction.guild, title="Slowmode Updated", description=f"Slowmode changed in {target_channel.mention}.", fields=[("Case", str(case_id), True), ("Seconds", str(seconds), True)])
        await send_interaction(interaction, embed=build_embed("Slowmode Updated", f"Set slowmode in {target_channel.mention} to `{seconds}` seconds."))

    @staff.subcommand(description="Lock a text channel")
    async def lock(
        self,
        interaction: nextcord.Interaction,
        channel: Optional[nextcord.TextChannel] = nextcord.SlashOption(required=False),
        reason: str = nextcord.SlashOption(required=False, default="Channel locked."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, nextcord.TextChannel):
            await send_interaction(interaction, content="Please run this in or target a text channel.", ephemeral=True)
            return
        overwrite = target_channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await target_channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=reason)
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "lock", reason, active=False, metadata={"channel_id": target_channel.id})
        await self.bot.send_log(interaction.guild, title="Channel Locked", description=f"{target_channel.mention} was locked.", fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True)])
        await send_interaction(interaction, embed=build_embed("Channel Locked", f"Locked {target_channel.mention}."))

    @staff.subcommand(description="Unlock a text channel")
    async def unlock(
        self,
        interaction: nextcord.Interaction,
        channel: Optional[nextcord.TextChannel] = nextcord.SlashOption(required=False),
        reason: str = nextcord.SlashOption(required=False, default="Channel unlocked."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, nextcord.TextChannel):
            await send_interaction(interaction, content="Please run this in or target a text channel.", ephemeral=True)
            return
        overwrite = target_channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await target_channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=reason)
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "unlock", reason, active=False, metadata={"channel_id": target_channel.id})
        await self.bot.send_log(interaction.guild, title="Channel Unlocked", description=f"{target_channel.mention} was unlocked.", fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True)])
        await send_interaction(interaction, embed=build_embed("Channel Unlocked", f"Unlocked {target_channel.mention}."))

    @staff.subcommand(description="Show recent case history for a member")
    async def history(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        limit: int = nextcord.SlashOption(required=False, default=10, min_value=1, max_value=20),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        cases = self.bot.db.list_member_cases(interaction.guild.id, member.id, limit=limit)
        if not cases:
            await send_interaction(interaction, embed=build_embed("Case History", f"No case history found for {member.mention}."))
            return
        lines = [f"#{item['id']} | {item['action']} | {item['reason']} | {'active' if item['active'] else 'inactive'}" for item in cases]
        await send_interaction(interaction, embed=build_embed(f"Case History for {member}", "\n".join(lines)))

    @staff.subcommand(description="Preview or kick recent joins during a raid")
    async def raid_cleanup(
        self,
        interaction: nextcord.Interaction,
        joined_within_minutes: int = nextcord.SlashOption(required=False, default=60, min_value=1, max_value=1440),
        dry_run: bool = nextcord.SlashOption(required=False, default=True),
        reason: str = nextcord.SlashOption(required=False, default="Raid cleanup."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        cutoff = utcnow() - timedelta(minutes=joined_within_minutes)
        candidates = [
            member
            for member in interaction.guild.members
            if not member.bot and member != interaction.guild.owner and member.joined_at is not None and member.joined_at >= cutoff and member.kickable
        ]
        if dry_run:
            preview = "\n".join(f"{member.mention} | joined {member.joined_at.isoformat()}" for member in candidates[:10]) or "No eligible members."
            await send_interaction(interaction, embed=build_embed("Raid Cleanup Preview", preview, fields=[("Eligible", str(len(candidates)), True)]))
            return
        await interaction.response.defer(ephemeral=True)
        kicked = 0
        for member in candidates:
            try:
                await member.kick(reason=reason)
                kicked += 1
            except (nextcord.Forbidden, nextcord.HTTPException):
                continue
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "raid_cleanup", reason, active=False, metadata={"kicked": kicked, "window_minutes": joined_within_minutes})
        await self.bot.send_log(interaction.guild, title="Raid Cleanup Executed", description=f"{moderator.mention} ran raid cleanup.", fields=[("Case", str(case_id), True), ("Kicked", str(kicked), True)])
        await interaction.followup.send(embed=build_embed("Raid Cleanup Complete", f"Kicked `{kicked}` members.", fields=[("Case", str(case_id), True)]), ephemeral=True)


def setup(bot: MemactAutoModBot) -> None:
    bot.add_cog(StaffCog(bot))
