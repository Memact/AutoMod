from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from typing import Optional

import nextcord
from nextcord.ext import commands, tasks

from bot import MemactAutoModBot
from config import COMMAND_GUILD_IDS
from utils.checks import require_admin
from utils.time import to_iso, utcnow
from utils.ui import build_embed, send_interaction


DESTRUCTIVE_ACTIONS = [
    "channel_delete",
    "role_delete",
    "member_ban",
    "member_kick",
    "webhook_delete",
]


class SafetyCog(commands.Cog):
    def __init__(self, bot: MemactAutoModBot) -> None:
        self.bot = bot
        self._backup_lock = asyncio.Lock()
        self._antinuke_alerts: dict[tuple[int, int], float] = {}
        self.backup_database.change_interval(hours=max(1, self.bot.settings.backup_interval_hours))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.backup_database.is_running():
            self.backup_database.start()

    def cog_unload(self) -> None:
        if self.backup_database.is_running():
            self.backup_database.cancel()

    def _clip(self, value: str | None, limit: int = 900) -> str:
        if not value:
            return "-"
        normalized = " ".join(value.split())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 3]}..."

    def _format_user(self, user: nextcord.abc.User | None) -> str:
        if user is None:
            return "Unknown"
        return f"{user.mention} (`{user.id}`)"

    def _format_channel(self, channel: nextcord.abc.GuildChannel | nextcord.Thread | None) -> str:
        if channel is None:
            return "Unknown"
        mention = getattr(channel, "mention", None)
        name = getattr(channel, "name", "unknown")
        return f"{mention or f'#{name}'} (`{channel.id}`)"

    def _format_role(self, role: nextcord.Role | None) -> str:
        if role is None:
            return "Unknown"
        return f"{role.mention} (`{role.id}`)"

    def _format_sentinel_event(self, event: dict) -> str:
        channel_id = event.get("channel_id")
        channel = f"<#{channel_id}>" if channel_id else "unknown channel"
        created_at = str(event.get("created_at", "unknown time")).replace("+00:00", " UTC")
        summary = self._clip(str(event.get("summary", "-")), 180)
        action = str(event.get("action", "observe"))
        deleted = "deleted" if event.get("deleted") else "observed"
        return (
            f"`#{event['id']}` s{event['severity']} {event['category']} {action}/{deleted} "
            f"<@{event['user_id']}> in {channel} - {summary} ({created_at})"
        )

    def _backup_files(self) -> list[Path]:
        backup_dir = Path(self.bot.settings.backup_dir)
        return sorted(
            backup_dir.glob("memact_automod_*.db"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    def _create_backup_sync(self, reason: str) -> tuple[Path, int]:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        destination = Path(self.bot.settings.backup_dir) / f"memact_automod_{timestamp}.db"
        self.bot.db.create_backup(str(destination))
        size = destination.stat().st_size
        self._prune_backups_sync()
        return destination, size

    def _prune_backups_sync(self) -> None:
        retention = max(1, self.bot.settings.backup_retention)
        for backup_file in self._backup_files()[retention:]:
            try:
                backup_file.unlink()
            except OSError:
                continue

    async def _create_backup(self, reason: str) -> tuple[Path, int]:
        async with self._backup_lock:
            return await asyncio.to_thread(self._create_backup_sync, reason)

    async def _find_audit_entry(
        self,
        guild: nextcord.Guild,
        action: nextcord.AuditLogAction,
        *,
        target_id: int | None = None,
    ) -> nextcord.AuditLogEntry | None:
        await asyncio.sleep(1.2)
        try:
            async for entry in guild.audit_logs(limit=8, action=action):
                if target_id is not None and getattr(entry.target, "id", None) != target_id:
                    continue
                if entry.created_at is not None:
                    age = (utcnow() - entry.created_at.astimezone(timezone.utc)).total_seconds()
                    if age > 45:
                        continue
                return entry
        except (nextcord.Forbidden, nextcord.HTTPException):
            return None
        return None

    async def _send_audit_log(
        self,
        guild: nextcord.Guild,
        *,
        title: str,
        description: str,
        fields: list[tuple[str, str, bool]] | None = None,
    ) -> None:
        if not self.bot.is_allowed_guild_id(guild.id):
            return
        config = self.bot.db.get_guild_config(guild.id)
        if not config["security_enabled"] or not config["audit_server_logs_enabled"]:
            return
        await self.bot.send_log(guild, title=title, description=description, fields=fields)

    def _is_self_actor(self, actor: nextcord.abc.User | None) -> bool:
        return actor is not None and self.bot.user is not None and actor.id == self.bot.user.id

    def _should_ignore_antinuke_actor(self, guild: nextcord.Guild, actor: nextcord.abc.User | None) -> bool:
        if actor is None:
            return True
        if self._is_self_actor(actor):
            return True
        return actor.id == guild.owner_id

    async def _can_timeout_member(self, member: nextcord.Member) -> bool:
        if member.guild.owner_id == member.id:
            return False
        bot_member = member.guild.me
        if bot_member is None and self.bot.user is not None:
            bot_member = member.guild.get_member(self.bot.user.id)
        if bot_member is None:
            return False
        if not getattr(bot_member.guild_permissions, "moderate_members", False):
            return False
        return bot_member.top_role > member.top_role

    async def _trigger_antinuke(
        self,
        guild: nextcord.Guild,
        *,
        actor: nextcord.abc.User,
        action: str,
        count: int,
        config: dict,
    ) -> None:
        alert_key = (guild.id, actor.id)
        now = time.monotonic()
        alert_cooldown = max(60, int(config["antinuke_window_seconds"]))
        if now - self._antinuke_alerts.get(alert_key, 0.0) < alert_cooldown:
            return
        self._antinuke_alerts[alert_key] = now

        self.bot.db.set_config_value(guild.id, "raid_mode", 1)
        reason = (
            f"Anti-nuke threshold hit after {count} destructive actions "
            f"within {config['antinuke_window_seconds']} seconds."
        )
        bot_user_id = self.bot.user.id if self.bot.user is not None else self.bot.settings.application_id
        case_id: int | None = None
        timeout_case_id: int | None = None
        timeout_status = "Not applied"

        if bot_user_id is not None:
            case_id = await self.bot.add_case(
                guild.id,
                actor.id,
                bot_user_id,
                "security_alert",
                reason,
                active=False,
                metadata={"source": "antinuke", "trigger_action": action, "event_count": count},
            )

        member = guild.get_member(actor.id)
        if member is None:
            try:
                member = await guild.fetch_member(actor.id)
            except (nextcord.NotFound, nextcord.Forbidden, nextcord.HTTPException):
                member = None

        timeout_minutes = int(config["antinuke_timeout_minutes"])
        if member is not None and timeout_minutes > 0 and await self._can_timeout_member(member):
            duration = timedelta(minutes=timeout_minutes)
            expires_at = to_iso(utcnow() + duration)
            try:
                await member.edit(timeout=duration, reason=reason)
                timeout_status = f"Timed out for {timeout_minutes} minutes"
                if bot_user_id is not None:
                    timeout_case_id = await self.bot.add_case(
                        guild.id,
                        actor.id,
                        bot_user_id,
                        "timeout",
                        reason,
                        expires_at=expires_at,
                        metadata={"source": "antinuke"},
                    )
                    self.bot.db.schedule_action(
                        guild.id,
                        actor.id,
                        "untimeout",
                        expires_at or "",
                        {"reason": f"Anti-nuke timeout expired for case #{timeout_case_id}."},
                    )
            except (nextcord.Forbidden, nextcord.HTTPException):
                timeout_status = "Failed: missing permissions or role hierarchy"
        elif member is None:
            timeout_status = "Skipped: member not found"
        elif timeout_minutes <= 0:
            timeout_status = "Disabled by config"
        else:
            timeout_status = "Skipped: role hierarchy or permission check failed"

        fields = [
            ("Actor", self._format_user(actor), False),
            ("Trigger", action.replace("_", " ").title(), True),
            ("Recent Events", str(count), True),
            ("Raid Mode", "Enabled", True),
            ("Security Case", str(case_id) if case_id is not None else "Not recorded", True),
            ("Timeout", timeout_status, True),
        ]
        if timeout_case_id is not None:
            fields.append(("Timeout Case", str(timeout_case_id), True))
        await self.bot.send_log(
            guild,
            title="Anti-Nuke Triggered",
            description="Memact AutoMod detected a burst of destructive server actions and switched into protection mode.",
            fields=fields,
        )

    async def _handle_destructive_event(
        self,
        guild: nextcord.Guild,
        *,
        action: str,
        audit_action: nextcord.AuditLogAction,
        target_id: int | None,
        target_label: str,
    ) -> None:
        if not self.bot.is_allowed_guild_id(guild.id):
            return
        config = self.bot.db.get_guild_config(guild.id)
        if not config["security_enabled"]:
            return

        entry = await self._find_audit_entry(guild, audit_action, target_id=target_id)
        actor = entry.user if entry is not None else None
        reason = self._clip(entry.reason if entry is not None else None, 300)

        if config["audit_server_logs_enabled"] and not self._is_self_actor(actor):
            await self.bot.send_log(
                guild,
                title="Security Audit Event",
                description=action.replace("_", " ").title(),
                fields=[
                    ("Actor", self._format_user(actor), False),
                    ("Target", target_label, False),
                    ("Reason", reason, False),
                ],
            )

        if not config["antinuke_enabled"] or self._should_ignore_antinuke_actor(guild, actor):
            return

        self.bot.db.add_security_event(
            guild.id,
            actor.id,
            action,
            target_id=target_id,
            details={"target": target_label, "reason": reason},
        )
        since = to_iso(utcnow() - timedelta(seconds=int(config["antinuke_window_seconds"])))
        if since is None:
            return
        recent_count = self.bot.db.count_recent_security_events(
            guild.id,
            actor.id,
            actions=DESTRUCTIVE_ACTIONS,
            since_iso=since,
        )
        if recent_count >= int(config["antinuke_threshold"]):
            await self._trigger_antinuke(
                guild,
                actor=actor,
                action=action,
                count=recent_count,
                config=config,
            )

    @tasks.loop(hours=12)
    async def backup_database(self) -> None:
        try:
            backup_path, size = await self._create_backup("automatic")
        except Exception as error:
            print(f"Automatic database backup failed: {type(error).__name__}: {error}")
            return

        for guild in list(self.bot.guilds):
            if not self.bot.is_allowed_guild_id(guild.id):
                continue
            await self.bot.send_log(
                guild,
                title="Database Backup Created",
                description="A scheduled SQLite backup was saved successfully.",
                fields=[
                    ("File", backup_path.name, False),
                    ("Size", f"{size / 1024:.1f} KiB", True),
                    ("Retention", f"Latest {self.bot.settings.backup_retention}", True),
                ],
            )

    @backup_database.before_loop
    async def before_backup_database(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message_delete(self, message: nextcord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not self.bot.is_allowed_guild_id(message.guild.id):
            return
        config = self.bot.db.get_guild_config(message.guild.id)
        if not config["security_enabled"] or not config["audit_message_logs_enabled"]:
            return
        attachments = ", ".join(attachment.url for attachment in message.attachments[:3])
        fields = [
            ("Author", self._format_user(message.author), False),
            ("Channel", self._format_channel(message.channel), True),
            ("Content", self._clip(message.clean_content), False),
        ]
        if attachments:
            fields.append(("Attachments", self._clip(attachments), False))
        await self.bot.send_log(
            message.guild,
            title="Message Deleted",
            description="A member message was deleted.",
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[nextcord.Message]) -> None:
        if not messages or messages[0].guild is None:
            return
        guild = messages[0].guild
        if not self.bot.is_allowed_guild_id(guild.id):
            return
        config = self.bot.db.get_guild_config(guild.id)
        if not config["security_enabled"] or not config["audit_message_logs_enabled"]:
            return
        channel = messages[0].channel
        await self.bot.send_log(
            guild,
            title="Messages Bulk Deleted",
            description=f"`{len(messages)}` messages were deleted at once.",
            fields=[("Channel", self._format_channel(channel), True)],
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: nextcord.Message, after: nextcord.Message) -> None:
        if before.guild is None or before.author.bot:
            return
        if before.clean_content == after.clean_content:
            return
        if not self.bot.is_allowed_guild_id(before.guild.id):
            return
        config = self.bot.db.get_guild_config(before.guild.id)
        if not config["security_enabled"] or not config["audit_message_logs_enabled"]:
            return
        await self.bot.send_log(
            before.guild,
            title="Message Edited",
            description="A member edited a message.",
            fields=[
                ("Author", self._format_user(before.author), False),
                ("Channel", self._format_channel(before.channel), True),
                ("Before", self._clip(before.clean_content), False),
                ("After", self._clip(after.clean_content), False),
            ],
        )

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: nextcord.abc.GuildChannel) -> None:
        if not self.bot.is_allowed_guild_id(channel.guild.id):
            return
        entry = await self._find_audit_entry(channel.guild, nextcord.AuditLogAction.channel_create, target_id=channel.id)
        actor = entry.user if entry is not None else None
        if self._is_self_actor(actor):
            return
        await self._send_audit_log(
            channel.guild,
            title="Channel Created",
            description="A server channel was created.",
            fields=[
                ("Actor", self._format_user(actor), False),
                ("Channel", self._format_channel(channel), False),
                ("Reason", self._clip(entry.reason if entry is not None else None, 300), False),
            ],
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: nextcord.abc.GuildChannel) -> None:
        await self._handle_destructive_event(
            channel.guild,
            action="channel_delete",
            audit_action=nextcord.AuditLogAction.channel_delete,
            target_id=channel.id,
            target_label=self._format_channel(channel),
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: nextcord.abc.GuildChannel,
        after: nextcord.abc.GuildChannel,
    ) -> None:
        entry = await self._find_audit_entry(after.guild, nextcord.AuditLogAction.channel_update, target_id=after.id)
        actor = entry.user if entry is not None else None
        if self._is_self_actor(actor):
            return
        changes = []
        if before.name != after.name:
            changes.append(f"name `{before.name}` -> `{after.name}`")
        before_topic = getattr(before, "topic", None)
        after_topic = getattr(after, "topic", None)
        if before_topic != after_topic:
            changes.append("topic changed")
        await self._send_audit_log(
            after.guild,
            title="Channel Updated",
            description=", ".join(changes) if changes else "A server channel was updated.",
            fields=[
                ("Actor", self._format_user(actor), False),
                ("Channel", self._format_channel(after), False),
                ("Reason", self._clip(entry.reason if entry is not None else None, 300), False),
            ],
        )

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: nextcord.Role) -> None:
        entry = await self._find_audit_entry(role.guild, nextcord.AuditLogAction.role_create, target_id=role.id)
        actor = entry.user if entry is not None else None
        if self._is_self_actor(actor):
            return
        await self._send_audit_log(
            role.guild,
            title="Role Created",
            description="A server role was created.",
            fields=[
                ("Actor", self._format_user(actor), False),
                ("Role", self._format_role(role), False),
                ("Reason", self._clip(entry.reason if entry is not None else None, 300), False),
            ],
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: nextcord.Role) -> None:
        await self._handle_destructive_event(
            role.guild,
            action="role_delete",
            audit_action=nextcord.AuditLogAction.role_delete,
            target_id=role.id,
            target_label=self._format_role(role),
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: nextcord.Role, after: nextcord.Role) -> None:
        entry = await self._find_audit_entry(after.guild, nextcord.AuditLogAction.role_update, target_id=after.id)
        actor = entry.user if entry is not None else None
        if self._is_self_actor(actor):
            return
        changes = []
        if before.name != after.name:
            changes.append(f"name `{before.name}` -> `{after.name}`")
        if before.permissions.value != after.permissions.value:
            changes.append("permissions changed")
        await self._send_audit_log(
            after.guild,
            title="Role Updated",
            description=", ".join(changes) if changes else "A server role was updated.",
            fields=[
                ("Actor", self._format_user(actor), False),
                ("Role", self._format_role(after), False),
                ("Reason", self._clip(entry.reason if entry is not None else None, 300), False),
            ],
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: nextcord.Guild, user: nextcord.User) -> None:
        await self._handle_destructive_event(
            guild,
            action="member_ban",
            audit_action=nextcord.AuditLogAction.ban,
            target_id=user.id,
            target_label=self._format_user(user),
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: nextcord.Guild, user: nextcord.User) -> None:
        entry = await self._find_audit_entry(guild, nextcord.AuditLogAction.unban, target_id=user.id)
        actor = entry.user if entry is not None else None
        if self._is_self_actor(actor):
            return
        await self._send_audit_log(
            guild,
            title="Member Unbanned",
            description="A member was unbanned.",
            fields=[
                ("Actor", self._format_user(actor), False),
                ("Member", self._format_user(user), False),
                ("Reason", self._clip(entry.reason if entry is not None else None, 300), False),
            ],
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: nextcord.Member) -> None:
        await self._handle_kick_audit(member)

    async def _handle_kick_audit(self, member: nextcord.Member) -> None:
        guild = member.guild
        if not self.bot.is_allowed_guild_id(guild.id):
            return
        entry = await self._find_audit_entry(guild, nextcord.AuditLogAction.kick, target_id=member.id)
        if entry is None:
            return
        await self._handle_destructive_event(
            guild,
            action="member_kick",
            audit_action=nextcord.AuditLogAction.kick,
            target_id=member.id,
            target_label=self._format_user(member),
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: nextcord.Member, after: nextcord.Member) -> None:
        if not self.bot.is_allowed_guild_id(after.guild.id):
            return

        before_roles = {role.id: role for role in before.roles if role.id != after.guild.id}
        after_roles = {role.id: role for role in after.roles if role.id != after.guild.id}
        added_roles = [after_roles[role_id] for role_id in after_roles.keys() - before_roles.keys()]
        removed_roles = [before_roles[role_id] for role_id in before_roles.keys() - after_roles.keys()]

        if added_roles or removed_roles:
            entry = await self._find_audit_entry(after.guild, nextcord.AuditLogAction.member_role_update, target_id=after.id)
            actor = entry.user if entry is not None else None
            if not self._is_self_actor(actor):
                fields = [
                    ("Actor", self._format_user(actor), False),
                    ("Member", self._format_user(after), False),
                ]
                if added_roles:
                    fields.append(("Added", ", ".join(role.mention for role in added_roles[:10]), False))
                if removed_roles:
                    fields.append(("Removed", ", ".join(role.mention for role in removed_roles[:10]), False))
                await self._send_audit_log(
                    after.guild,
                    title="Member Roles Updated",
                    description="A member's roles changed.",
                    fields=fields,
                )

        if before.nick != after.nick:
            entry = await self._find_audit_entry(after.guild, nextcord.AuditLogAction.member_update, target_id=after.id)
            actor = entry.user if entry is not None else None
            if not self._is_self_actor(actor):
                await self._send_audit_log(
                    after.guild,
                    title="Member Nickname Updated",
                    description="A member nickname changed.",
                    fields=[
                        ("Actor", self._format_user(actor), False),
                        ("Member", self._format_user(after), False),
                        ("Before", before.nick or before.name, True),
                        ("After", after.nick or after.name, True),
                    ],
                )

    @nextcord.slash_command(
        description="Security, audit, and backup controls",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_guild=True),
    )
    async def security(self, interaction: nextcord.Interaction) -> None:
        pass

    @security.subcommand(description="Show security and backup status")
    async def view(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        config = self.bot.db.get_guild_config(interaction.guild.id)
        backup_files = self._backup_files()
        latest_backup = backup_files[0].name if backup_files else "No backups yet"
        embed = build_embed(
            "Memact Security",
            "Current safety, audit, and backup settings.",
            fields=[
                ("Security", "On" if config["security_enabled"] else "Off", True),
                ("Anti-Nuke", "On" if config["antinuke_enabled"] else "Off", True),
                (
                    "Anti-Nuke Threshold",
                    f"{config['antinuke_threshold']} destructive actions / {config['antinuke_window_seconds']}s",
                    False,
                ),
                ("Anti-Nuke Timeout", f"{config['antinuke_timeout_minutes']} minutes", True),
                ("Message Audit Logs", "On" if config["audit_message_logs_enabled"] else "Off", True),
                ("Server Audit Logs", "On" if config["audit_server_logs_enabled"] else "Off", True),
                ("Backup Interval", f"{self.bot.settings.backup_interval_hours} hours", True),
                ("Backup Retention", f"{self.bot.settings.backup_retention} files", True),
                ("Latest Backup", latest_backup, False),
            ],
        )
        await send_interaction(interaction, embed=embed)

    @security.subcommand(description="Show Sentinel and local guard intelligence for a member")
    async def sentinel(self, interaction: nextcord.Interaction, member: nextcord.Member) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        profile = self.bot.db.get_sentinel_profile(interaction.guild.id, member.id)
        events = self.bot.db.list_recent_sentinel_events(interaction.guild.id, user_id=member.id, limit=5)
        if profile is None:
            await send_interaction(
                interaction,
                embed=build_embed(
                    "Sentinel Profile",
                    f"No Sentinel or local guard events are stored for {member.mention}.",
                ),
            )
            return
        lines = [self._format_sentinel_event(event) for event in events]
        await send_interaction(
            interaction,
            embed=build_embed(
                "Sentinel Profile",
                "Review intelligence from silent Sentinel detections and local guard actions.",
                fields=[
                    ("Member", f"{member.mention} (`{member.id}`)", False),
                    ("Risk Score", f"{float(profile['risk_score']):.1f}/100", True),
                    ("Events", str(profile["event_count"]), True),
                    ("Last Event", str(profile["last_event_at"]).replace("+00:00", " UTC"), True),
                    ("Recent Signals", "\n".join(lines) if lines else "No recent events.", False),
                ],
            ),
        )

    @security.subcommand(description="Show recent Sentinel and local guard events")
    async def sentinel_recent(
        self,
        interaction: nextcord.Interaction,
        limit: int = nextcord.SlashOption(required=False, default=10, min_value=1, max_value=20),
        min_severity: Optional[int] = nextcord.SlashOption(required=False, min_value=1, max_value=5),
    ) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        events = self.bot.db.list_recent_sentinel_events(
            interaction.guild.id,
            limit=limit,
            min_severity=min_severity,
        )
        if not events:
            await send_interaction(interaction, embed=build_embed("Sentinel Events", "No Sentinel or local guard events are stored yet."))
            return
        lines = [self._format_sentinel_event(event) for event in events]
        await send_interaction(
            interaction,
            embed=build_embed(
                "Sentinel Events",
                "\n".join(lines),
                footer="Review queue covering silent detections and logged local guard actions.",
            ),
        )

    @security.subcommand(description="Adjust security and audit settings")
    async def settings(
        self,
        interaction: nextcord.Interaction,
        enabled: Optional[bool] = nextcord.SlashOption(required=False, description="Master security switch"),
        anti_nuke: Optional[bool] = nextcord.SlashOption(required=False, description="Detect destructive bursts"),
        threshold: Optional[int] = nextcord.SlashOption(required=False, min_value=2, max_value=20),
        window_seconds: Optional[int] = nextcord.SlashOption(required=False, min_value=30, max_value=900),
        timeout_minutes: Optional[int] = nextcord.SlashOption(required=False, min_value=0, max_value=10080),
        message_logs: Optional[bool] = nextcord.SlashOption(required=False, description="Log message edits/deletes"),
        server_logs: Optional[bool] = nextcord.SlashOption(required=False, description="Log server audit events"),
    ) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return

        changes: list[str] = []
        updates = {
            "security_enabled": enabled,
            "antinuke_enabled": anti_nuke,
            "antinuke_threshold": threshold,
            "antinuke_window_seconds": window_seconds,
            "antinuke_timeout_minutes": timeout_minutes,
            "audit_message_logs_enabled": message_logs,
            "audit_server_logs_enabled": server_logs,
        }
        for column, value in updates.items():
            if value is None:
                continue
            self.bot.db.set_config_value(interaction.guild.id, column, int(value) if isinstance(value, bool) else value)
            changes.append(f"`{column}` -> `{value}`")

        if not changes:
            await send_interaction(interaction, content="Pass at least one setting to update.", ephemeral=True)
            return
        await send_interaction(
            interaction,
            embed=build_embed("Security Settings Updated", "\n".join(changes)),
        )

    @security.subcommand(description="Create an immediate SQLite database backup")
    async def backup_create(
        self,
        interaction: nextcord.Interaction,
        reason: str = nextcord.SlashOption(required=False, default="Manual backup"),
    ) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            backup_path, size = await self._create_backup(reason)
        except Exception as error:
            await send_interaction(
                interaction,
                content=f"Backup failed: `{type(error).__name__}`.",
                ephemeral=True,
            )
            return
        await self.bot.send_log(
            interaction.guild,
            title="Manual Database Backup Created",
            description=f"{admin.mention} created a SQLite backup.",
            fields=[
                ("File", backup_path.name, False),
                ("Size", f"{size / 1024:.1f} KiB", True),
                ("Reason", reason, False),
            ],
        )
        await send_interaction(
            interaction,
            embed=build_embed(
                "Backup Created",
                "Saved a SQLite database backup.",
                fields=[
                    ("File", backup_path.name, False),
                    ("Size", f"{size / 1024:.1f} KiB", True),
                ],
            ),
        )

    @security.subcommand(description="List recent database backups")
    async def backup_list(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        backup_files = self._backup_files()[:10]
        if not backup_files:
            await send_interaction(interaction, embed=build_embed("Database Backups", "No backups have been created yet."))
            return
        lines = []
        for backup_file in backup_files:
            created = datetime.fromtimestamp(backup_file.stat().st_mtime, tz=timezone.utc)
            size_kib = backup_file.stat().st_size / 1024
            lines.append(f"`{backup_file.name}` - {created:%Y-%m-%d %H:%M UTC} - {size_kib:.1f} KiB")
        await send_interaction(
            interaction,
            embed=build_embed("Database Backups", "\n".join(lines)),
        )


def setup(bot: MemactAutoModBot) -> None:
    bot.add_cog(SafetyCog(bot))
