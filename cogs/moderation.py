from __future__ import annotations

from typing import Optional

import nextcord
from nextcord.ext import commands

from bot import MemactAutoModBot
from config import COMMAND_GUILD_IDS
from utils.checks import is_moderator_member, require_moderator
from utils.time import format_timedelta, parse_duration, to_iso, utcnow
from utils.ui import build_embed, send_interaction


class ModerationCog(commands.Cog):
    def __init__(self, bot: MemactAutoModBot) -> None:
        self.bot = bot

    async def _can_touch(self, actor: nextcord.Member, target: nextcord.Member) -> bool:
        if actor == target:
            return False
        if target == target.guild.owner:
            return False
        if actor.guild_permissions.administrator:
            return True
        return actor.top_role > target.top_role

    async def _require_target(self, interaction: nextcord.Interaction, target: nextcord.Member) -> bool:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return False
        if not await self._can_touch(moderator, target):
            await send_interaction(interaction, content="You can't moderate that member because of role hierarchy or ownership.", ephemeral=True)
            return False
        return True

    @nextcord.slash_command(
        description="Moderation commands",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_messages=True),
    )
    async def mod(self, interaction: nextcord.Interaction) -> None:
        pass

    @mod.subcommand(description="Kick a member")
    async def kick(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        reason: str = nextcord.SlashOption(required=False, default="No reason provided."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None or not await self._require_target(interaction, member):
            return
        if not member.kickable:
            await send_interaction(
                interaction,
                content="I can't kick that member. Check my role position and permissions.",
                ephemeral=True,
            )
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
        await send_interaction(interaction, embed=build_embed("Kick Complete", f"Kicked {member.mention}.", fields=[("Case", str(case_id), True), ("Reason", reason, False)]))

    @mod.subcommand(description="Ban a member")
    async def ban(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        reason: str = nextcord.SlashOption(required=False, default="No reason provided."),
        delete_message_hours: int = nextcord.SlashOption(required=False, default=0, min_value=0, max_value=168),
        duration: Optional[str] = nextcord.SlashOption(required=False, description="Optional tempban like 7d or 12h"),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None or not await self._require_target(interaction, member):
            return

        expires_at = None
        parsed_duration = None
        if duration:
            parsed_duration = parse_duration(duration)
            if parsed_duration is None:
                await send_interaction(interaction, content="Duration must look like `30m`, `12h`, `7d`, or `1w`.", ephemeral=True)
                return
            expires_at = to_iso(utcnow() + parsed_duration)

        delete_seconds = delete_message_hours * 3600
        await member.ban(reason=reason, delete_message_seconds=delete_seconds)
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
            self.bot.db.schedule_action(
                interaction.guild.id,
                member.id,
                "unban",
                expires_at,
                {"reason": f"Temporary ban expired for case #{case_id}."},
            )
        await self.bot.dm_case_notice(
            member,
            action="Ban",
            guild_name=interaction.guild.name,
            reason=reason,
            case_id=case_id,
            duration=parsed_duration,
        )
        fields = [("Case", str(case_id), True), ("Moderator", moderator.mention, True), ("Reason", reason, False)]
        if parsed_duration is not None:
            fields.append(("Duration", format_timedelta(parsed_duration), True))
        await self.bot.send_log(interaction.guild, title="Member Banned", description=f"{member.mention} was banned.", fields=fields)
        await send_interaction(interaction, embed=build_embed("Ban Complete", f"Banned {member.mention}.", fields=fields))

    @mod.subcommand(description="Unban a user by ID")
    async def unban(
        self,
        interaction: nextcord.Interaction,
        user_id: int,
        reason: str = nextcord.SlashOption(required=False, default="No reason provided."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        try:
            user = await self.bot.fetch_user(user_id)
        except nextcord.NotFound:
            await send_interaction(interaction, content="That user ID does not exist on Discord.", ephemeral=True)
            return
        except nextcord.HTTPException:
            await send_interaction(interaction, content="I couldn't look up that user ID right now.", ephemeral=True)
            return
        try:
            await interaction.guild.unban(user, reason=reason)
        except nextcord.NotFound:
            await send_interaction(interaction, content="That user is not currently banned.", ephemeral=True)
            return
        case_id = await self.bot.add_case(interaction.guild.id, user.id, moderator.id, "unban", reason)
        await self.bot.send_log(
            interaction.guild,
            title="User Unbanned",
            description=f"`{user}` was unbanned.",
            fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True), ("Reason", reason, False)],
        )
        await send_interaction(interaction, embed=build_embed("Unban Complete", f"Unbanned `{user}`.", fields=[("Case", str(case_id), True)]))

    @mod.subcommand(description="Timeout a member")
    async def timeout(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        duration: str = nextcord.SlashOption(description="Example: 30m, 6h, 2d"),
        reason: str = nextcord.SlashOption(required=False, default="No reason provided."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None or not await self._require_target(interaction, member):
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
        await self.bot.dm_case_notice(
            member,
            action="Timeout",
            guild_name=interaction.guild.name,
            reason=reason,
            case_id=case_id,
            duration=parsed_duration,
        )
        await self.bot.send_log(
            interaction.guild,
            title="Member Timed Out",
            description=f"{member.mention} was timed out.",
            fields=[
                ("Case", str(case_id), True),
                ("Moderator", moderator.mention, True),
                ("Duration", format_timedelta(parsed_duration), True),
                ("Reason", reason, False),
            ],
        )
        await send_interaction(interaction, embed=build_embed("Timeout Complete", f"Timed out {member.mention}.", fields=[("Duration", format_timedelta(parsed_duration), True), ("Case", str(case_id), True)]))

    @mod.subcommand(description="Remove a timeout from a member")
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
        await self.bot.send_log(
            interaction.guild,
            title="Timeout Removed",
            description=f"{member.mention} had their timeout removed.",
            fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True), ("Reason", reason, False)],
        )
        await send_interaction(interaction, embed=build_embed("Timeout Removed", f"Removed the timeout from {member.mention}.", fields=[("Case", str(case_id), True)]))

    @mod.subcommand(description="Warn a member")
    async def warn(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        points: int = nextcord.SlashOption(required=False, default=1, min_value=1, max_value=10),
        reason: str = nextcord.SlashOption(required=False, default="No reason provided."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None or not await self._require_target(interaction, member):
            return
        case_id, total_points, escalation = await self.bot.apply_warning(
            interaction.guild,
            member,
            moderator=moderator,
            reason=reason,
            points=points,
            source="manual",
        )
        lines = [f"Warned {member.mention}.", f"Case #{case_id}.", f"Active warning points: {total_points}."]
        if escalation:
            lines.append(f"Automatic escalation: `{escalation}`.")
        await send_interaction(interaction, embed=build_embed("Warning Logged", "\n".join(lines)))

    @mod.subcommand(description="Show active warning points and recent active warnings")
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
            await send_interaction(
                interaction,
                embed=build_embed("Active Warnings", f"{member.mention} has no active warning points."),
            )
            return
        lines = [
            f"#{item['id']} | {item['points']} point(s) | {item['reason']} | {item['created_at']}"
            for item in warnings
        ]
        await send_interaction(
            interaction,
            embed=build_embed(
                f"Active Warnings for {member}",
                "\n".join(lines),
                fields=[("Active Points", str(total_points), True)],
            ),
        )

    @mod.subcommand(description="Deactivate a warning by case ID")
    async def unwarn(
        self,
        interaction: nextcord.Interaction,
        case_id: int,
        reason: str = nextcord.SlashOption(required=False, default="Warning revoked."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        case = self.bot.db.get_case(interaction.guild.id, case_id)
        if case is None or case["action"] != "warn":
            await send_interaction(interaction, content="That warning case was not found.", ephemeral=True)
            return
        if not self.bot.db.deactivate_case(interaction.guild.id, case_id):
            await send_interaction(interaction, content="That warning is already inactive.", ephemeral=True)
            return
        audit_case_id = await self.bot.add_case(interaction.guild.id, case["user_id"], moderator.id, "unwarn", reason, active=False, metadata={"target_case": case_id})
        await self.bot.send_log(
            interaction.guild,
            title="Warning Revoked",
            description=f"Warning case #{case_id} was revoked.",
            fields=[("Audit Case", str(audit_case_id), True), ("Moderator", moderator.mention, True), ("Reason", reason, False)],
        )
        await send_interaction(interaction, embed=build_embed("Warning Revoked", f"Deactivated warning case #{case_id}.", fields=[("Audit Case", str(audit_case_id), True)]))

    @mod.subcommand(description="Deactivate the latest active warning for a member")
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
        audit_case_id = await self.bot.add_case(
            interaction.guild.id,
            member.id,
            moderator.id,
            "unwarn",
            reason,
            active=False,
            metadata={"target_case": warning["id"], "mode": "latest"},
        )
        total_points = self.bot.db.get_active_warning_points(interaction.guild.id, member.id)
        await self.bot.send_log(
            interaction.guild,
            title="Latest Warning Revoked",
            description=f"The latest active warning for {member.mention} was revoked.",
            fields=[
                ("Revoked Warning", str(warning["id"]), True),
                ("Audit Case", str(audit_case_id), True),
                ("Active Points Now", str(total_points), True),
                ("Moderator", moderator.mention, True),
                ("Reason", reason, False),
            ],
        )
        await send_interaction(
            interaction,
            embed=build_embed(
                "Latest Warning Revoked",
                f"Revoked warning #{warning['id']} for {member.mention}.",
                fields=[
                    ("Audit Case", str(audit_case_id), True),
                    ("Active Points Now", str(total_points), True),
                ],
            ),
        )

    @mod.subcommand(description="Clear all active warnings for a member")
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
        await self.bot.send_log(
            interaction.guild,
            title="Warnings Cleared",
            description=f"Cleared {cleared} active warnings for {member.mention}.",
            fields=[("Audit Case", str(audit_case_id), True), ("Moderator", moderator.mention, True), ("Reason", reason, False)],
        )
        await send_interaction(interaction, embed=build_embed("Warnings Cleared", f"Cleared `{cleared}` active warnings for {member.mention}.", fields=[("Audit Case", str(audit_case_id), True)]))

    @mod.subcommand(description="Add a staff note to a member's case history")
    async def note(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        note: str,
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        case_id = await self.bot.add_case(interaction.guild.id, member.id, moderator.id, "note", note, active=False)
        await self.bot.send_log(
            interaction.guild,
            title="Staff Note Added",
            description=f"A note was added for {member.mention}.",
            fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True), ("Note", note, False)],
        )
        await send_interaction(interaction, embed=build_embed("Note Added", f"Added a staff note for {member.mention}.", fields=[("Case", str(case_id), True)]))

    @mod.subcommand(description="Bulk delete recent messages")
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

        def check(message: nextcord.Message) -> bool:
            return member is None or message.author.id == member.id

        deleted = await channel.purge(limit=amount, check=check, bulk=True)
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "purge", reason, active=False, metadata={"deleted": len(deleted), "channel_id": channel.id})
        await self.bot.send_log(
            interaction.guild,
            title="Messages Purged",
            description=f"{moderator.mention} purged messages in {channel.mention}.",
            fields=[
                ("Case", str(case_id), True),
                ("Deleted", str(len(deleted)), True),
                ("Target", member.mention if member else "Any user", True),
                ("Reason", reason, False),
            ],
        )
        await interaction.followup.send(embed=build_embed("Purge Complete", f"Deleted `{len(deleted)}` messages.", fields=[("Case", str(case_id), True)]), ephemeral=True)

    @mod.subcommand(description="Preview or kick members who joined recently during a raid")
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
        config = self.bot.db.get_guild_config(interaction.guild.id)
        candidates: list[nextcord.Member] = []
        for member in interaction.guild.members:
            if member.bot or member == interaction.guild.owner:
                continue
            if member.joined_at is None or member.joined_at < cutoff:
                continue
            if is_moderator_member(member, config):
                continue
            if not await self._can_touch(moderator, member):
                continue
            candidates.append(member)

        if not candidates:
            await send_interaction(interaction, embed=build_embed("Raid Cleanup", "No eligible members matched that join window."))
            return

        if dry_run:
            preview = "\n".join(f"{member.mention} | joined {member.joined_at.isoformat()}" for member in candidates[:10])
            more = "" if len(candidates) <= 10 else f"\n...and {len(candidates) - 10} more."
            await send_interaction(
                interaction,
                embed=build_embed(
                    "Raid Cleanup Preview",
                    f"`{len(candidates)}` members joined within the last `{joined_within_minutes}` minutes and are eligible for cleanup.\n\n{preview}{more}",
                ),
            )
            return

        await interaction.response.defer(ephemeral=True)
        kicked = 0
        failed = 0
        for member in candidates:
            if not member.kickable:
                failed += 1
                continue
            try:
                await member.kick(reason=reason)
                kicked += 1
            except (nextcord.Forbidden, nextcord.HTTPException):
                failed += 1

        case_id = await self.bot.add_case(
            interaction.guild.id,
            moderator.id,
            moderator.id,
            "raid_cleanup",
            reason,
            active=False,
            metadata={"cutoff_minutes": joined_within_minutes, "kicked": kicked, "failed": failed},
        )
        await self.bot.send_log(
            interaction.guild,
            title="Raid Cleanup Executed",
            description=f"{moderator.mention} ran raid cleanup.",
            fields=[
                ("Case", str(case_id), True),
                ("Window", f"{joined_within_minutes} minutes", True),
                ("Kicked", str(kicked), True),
                ("Failed", str(failed), True),
                ("Reason", reason, False),
            ],
        )
        await interaction.followup.send(
            embed=build_embed(
                "Raid Cleanup Complete",
                f"Kicked `{kicked}` members from the last `{joined_within_minutes}` minutes. `{failed}` could not be kicked.",
                fields=[("Case", str(case_id), True)],
            ),
            ephemeral=True,
        )

    @mod.subcommand(description="Set slowmode on a channel")
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
        await self.bot.send_log(
            interaction.guild,
            title="Slowmode Updated",
            description=f"Slowmode changed in {target_channel.mention}.",
            fields=[("Case", str(case_id), True), ("Seconds", str(seconds), True), ("Moderator", moderator.mention, True)],
        )
        await send_interaction(interaction, embed=build_embed("Slowmode Updated", f"Set slowmode in {target_channel.mention} to `{seconds}` seconds.", fields=[("Case", str(case_id), True)]))

    @mod.subcommand(description="Set or clear a member nickname")
    async def nickname(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        nickname: Optional[str] = nextcord.SlashOption(required=False, description="Leave blank to clear it"),
        reason: str = nextcord.SlashOption(required=False, default="Nickname updated."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None or not await self._require_target(interaction, member):
            return
        await member.edit(nick=nickname, reason=reason)
        case_id = await self.bot.add_case(interaction.guild.id, member.id, moderator.id, "nickname", reason, active=False, metadata={"nickname": nickname})
        await self.bot.send_log(
            interaction.guild,
            title="Nickname Updated",
            description=f"Updated nickname for {member.mention}.",
            fields=[("Case", str(case_id), True), ("Nickname", nickname or "Cleared", True), ("Moderator", moderator.mention, True)],
        )
        await send_interaction(interaction, embed=build_embed("Nickname Updated", f"Updated nickname for {member.mention}.", fields=[("Case", str(case_id), True), ("Nickname", nickname or "Cleared", True)]))

    @mod.subcommand(description="Lock a text channel")
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
        await send_interaction(interaction, embed=build_embed("Channel Locked", f"Locked {target_channel.mention}.", fields=[("Case", str(case_id), True)]))

    @mod.subcommand(description="Unlock a text channel")
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
        await send_interaction(interaction, embed=build_embed("Channel Unlocked", f"Unlocked {target_channel.mention}.", fields=[("Case", str(case_id), True)]))

    @nextcord.slash_command(
        description="Role management commands",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_messages=True),
    )
    async def role(self, interaction: nextcord.Interaction) -> None:
        pass

    @role.subcommand(description="Create a role")
    async def create(
        self,
        interaction: nextcord.Interaction,
        name: str,
        color_hex: str = nextcord.SlashOption(required=False, default="#00011B"),
        mentionable: bool = nextcord.SlashOption(required=False, default=False),
        hoist: bool = nextcord.SlashOption(required=False, default=False),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        color_text = color_hex.replace("#", "").strip()
        try:
            color_value = int(color_text, 16)
        except ValueError:
            await send_interaction(interaction, content="Color must be a valid hex value like `#00011B`.", ephemeral=True)
            return
        role = await interaction.guild.create_role(
            name=name,
            colour=nextcord.Colour(color_value),
            mentionable=mentionable,
            hoist=hoist,
            reason=f"Created by {moderator}",
        )
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "role_create", f"Created role {name}.", active=False, metadata={"role_id": role.id})
        await self.bot.send_log(interaction.guild, title="Role Created", description=f"Created {role.mention}.", fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True)])
        await send_interaction(interaction, embed=build_embed("Role Created", f"Created {role.mention}.", fields=[("Case", str(case_id), True)]))

    @role.subcommand(description="Delete a role")
    async def delete(
        self,
        interaction: nextcord.Interaction,
        role: nextcord.Role,
        reason: str = nextcord.SlashOption(required=False, default="Role deleted."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        role_name = role.name
        await role.delete(reason=reason)
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "role_delete", reason, active=False, metadata={"role_name": role_name})
        await self.bot.send_log(interaction.guild, title="Role Deleted", description=f"Deleted `{role_name}`.", fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True)])
        await send_interaction(interaction, embed=build_embed("Role Deleted", f"Deleted `{role_name}`.", fields=[("Case", str(case_id), True)]))

    @role.subcommand(description="Give a role to a member")
    async def give(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        role: nextcord.Role,
        reason: str = nextcord.SlashOption(required=False, default="Role granted."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None or not await self._require_target(interaction, member):
            return
        await member.add_roles(role, reason=reason)
        case_id = await self.bot.add_case(interaction.guild.id, member.id, moderator.id, "role_give", reason, active=False, metadata={"role_id": role.id})
        await self.bot.send_log(interaction.guild, title="Role Granted", description=f"{role.mention} was granted to {member.mention}.", fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True)])
        await send_interaction(interaction, embed=build_embed("Role Granted", f"Gave {role.mention} to {member.mention}.", fields=[("Case", str(case_id), True)]))

    @role.subcommand(description="Remove a role from a member")
    async def remove(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
        role: nextcord.Role,
        reason: str = nextcord.SlashOption(required=False, default="Role removed."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None or not await self._require_target(interaction, member):
            return
        await member.remove_roles(role, reason=reason)
        case_id = await self.bot.add_case(interaction.guild.id, member.id, moderator.id, "role_remove", reason, active=False, metadata={"role_id": role.id})
        await self.bot.send_log(interaction.guild, title="Role Removed", description=f"{role.mention} was removed from {member.mention}.", fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True)])
        await send_interaction(interaction, embed=build_embed("Role Removed", f"Removed {role.mention} from {member.mention}.", fields=[("Case", str(case_id), True)]))

    @role.subcommand(description="List a member's roles")
    async def list(
        self,
        interaction: nextcord.Interaction,
        member: nextcord.Member,
    ) -> None:
        roles = [role.mention for role in member.roles if role != interaction.guild.default_role]
        description = "\n".join(roles) if roles else "No extra roles."
        await send_interaction(interaction, embed=build_embed(f"Roles for {member}", description))

    @nextcord.slash_command(
        description="Channel management commands",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_messages=True),
    )
    async def channel(self, interaction: nextcord.Interaction) -> None:
        pass

    @channel.subcommand(description="Set a text channel topic")
    async def topic(
        self,
        interaction: nextcord.Interaction,
        channel: nextcord.TextChannel,
        topic: str,
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        await channel.edit(topic=topic, reason=f"Topic updated by {moderator}")
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "topic", topic, active=False, metadata={"channel_id": channel.id})
        await self.bot.send_log(interaction.guild, title="Channel Topic Updated", description=f"Updated topic for {channel.mention}.", fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True)])
        await send_interaction(interaction, embed=build_embed("Topic Updated", f"Updated topic for {channel.mention}.", fields=[("Case", str(case_id), True)]))

    @channel.subcommand(name="create", description="Create a channel")
    async def create_channel(
        self,
        interaction: nextcord.Interaction,
        name: str,
        channel_type: str = nextcord.SlashOption(choices={"Text": "text", "Voice": "voice", "Category": "category"}),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        if channel_type == "text":
            created = await interaction.guild.create_text_channel(name, reason=f"Created by {moderator}")
        elif channel_type == "voice":
            created = await interaction.guild.create_voice_channel(name, reason=f"Created by {moderator}")
        else:
            created = await interaction.guild.create_category(name, reason=f"Created by {moderator}")
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "channel_create", f"Created channel {name}.", active=False, metadata={"channel_id": created.id})
        created_label = getattr(created, "mention", None) or created.name
        await self.bot.send_log(interaction.guild, title="Channel Created", description=f"Created {created_label}.", fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True)])
        await send_interaction(interaction, embed=build_embed("Channel Created", f"Created `{created.name}`.", fields=[("Case", str(case_id), True)]))

    @channel.subcommand(name="delete", description="Delete a channel")
    async def delete_channel(
        self,
        interaction: nextcord.Interaction,
        channel: nextcord.abc.GuildChannel,
        reason: str = nextcord.SlashOption(required=False, default="Channel deleted."),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        channel_name = channel.name
        await channel.delete(reason=reason)
        case_id = await self.bot.add_case(interaction.guild.id, moderator.id, moderator.id, "channel_delete", reason, active=False, metadata={"channel_name": channel_name})
        await self.bot.send_log(interaction.guild, title="Channel Deleted", description=f"Deleted `{channel_name}`.", fields=[("Case", str(case_id), True), ("Moderator", moderator.mention, True)])
        await send_interaction(interaction, embed=build_embed("Channel Deleted", f"Deleted `{channel_name}`.", fields=[("Case", str(case_id), True)]))

    @nextcord.slash_command(
        description="Case history commands",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_messages=True),
    )
    async def case(self, interaction: nextcord.Interaction) -> None:
        pass

    @case.subcommand(description="View a case by ID")
    async def view(
        self,
        interaction: nextcord.Interaction,
        case_id: int,
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        case = self.bot.db.get_case(interaction.guild.id, case_id)
        if case is None:
            await send_interaction(interaction, content="Case not found.", ephemeral=True)
            return
        embed = build_embed(
            f"Case #{case_id}",
            case["reason"],
            fields=[
                ("Action", case["action"], True),
                ("User ID", str(case["user_id"]), True),
                ("Moderator ID", str(case["moderator_id"]), True),
                ("Points", str(case["points"]), True),
                ("Active", "Yes" if case["active"] else "No", True),
                ("Created", case["created_at"], False),
            ],
        )
        if case["expires_at"]:
            embed.add_field(name="Expires", value=case["expires_at"], inline=False)
        await send_interaction(interaction, embed=embed)

    @case.subcommand(description="Show recent case history for a member")
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
        lines = []
        for item in cases:
            state = "active" if item["active"] else "inactive"
            lines.append(f"#{item['id']} | {item['action']} | {item['reason']} | {state}")
        await send_interaction(interaction, embed=build_embed(f"Case History for {member}", "\n".join(lines)))

    @case.subcommand(description="Search cases by action, member, and recent time window")
    async def search(
        self,
        interaction: nextcord.Interaction,
        action: str = nextcord.SlashOption(
            required=False,
            default="all",
            choices={
                "Any": "all",
                "Warn": "warn",
                "Kick": "kick",
                "Ban": "ban",
                "Unban": "unban",
                "Timeout": "timeout",
                "Untimeout": "untimeout",
                "Purge": "purge",
                "Note": "note",
                "Raid Cleanup": "raid_cleanup",
            },
        ),
        member: Optional[nextcord.Member] = nextcord.SlashOption(required=False),
        days_back: int = nextcord.SlashOption(required=False, default=30, min_value=1, max_value=3650),
        limit: int = nextcord.SlashOption(required=False, default=10, min_value=1, max_value=25),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        created_after = to_iso(utcnow() - timedelta(days=days_back))
        cases = self.bot.db.search_cases(
            interaction.guild.id,
            user_id=member.id if member is not None else None,
            action=None if action == "all" else action,
            created_after=created_after,
            limit=limit,
        )
        if not cases:
            await send_interaction(interaction, embed=build_embed("Case Search", "No cases matched that search."))
            return
        lines = []
        for item in cases:
            state = "active" if item["active"] else "inactive"
            lines.append(
                f"#{item['id']} | user `{item['user_id']}` | {item['action']} | {item['created_at']} | {state}"
            )
        await send_interaction(
            interaction,
            embed=build_embed(
                "Case Search Results",
                "\n".join(lines),
                fields=[
                    ("Action", action, True),
                    ("Member", member.mention if member is not None else "Any", True),
                    ("Window", f"Last {days_back} days", True),
                ],
            ),
        )


def setup(bot: MemactAutoModBot) -> None:
    bot.add_cog(ModerationCog(bot))
