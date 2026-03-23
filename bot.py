from __future__ import annotations

import asyncio
from datetime import timedelta
import traceback
from typing import Any

import nextcord
from nextcord.ext import commands

from config import EMBED_COLOR, Settings
from db import Database
from utils.time import format_timedelta, to_iso, utcnow
from utils.ui import build_embed, safe_dm


class MemactAutoModBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = nextcord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True
        intents.guild_messages = True
        intents.bans = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            application_id=settings.application_id,
            default_guild_ids=[settings.dev_guild_id] if settings.dev_guild_id is not None else None,
        )
        self.settings = settings
        self.db = Database(settings.database_path)
        self.theme_color = EMBED_COLOR
        self.keepalive_state: Any | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._commands_synced = False
        self.add_application_command_check(self._allowed_guild_check)
        for extension in (
            "cogs.moderation",
            "cogs.configuration",
            "cogs.automod",
            "cogs.rules",
            "cogs.embed_tools",
            "cogs.community",
        ):
            self.load_extension(extension)
        self.add_all_application_commands()

    async def close(self) -> None:
        if self.keepalive_state is not None:
            self.keepalive_state.set_status("stopped", "Bot process is shutting down.")
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        local_command_count = len(self.get_all_application_commands())
        if self.user is not None:
            print(f"Logged in as {self.user} ({self.user.id})")
            if self.keepalive_state is not None:
                self.keepalive_state.set_status("ready", f"Connected as {self.user}.")
        print(f"Loaded {local_command_count} local application command groups.")
        if not self._commands_synced:
            try:
                if self.settings.dev_guild_id is not None:
                    print(f"Syncing application commands to guild {self.settings.dev_guild_id}...")
                    await self.sync_application_commands(guild_id=self.settings.dev_guild_id)
                else:
                    print("Syncing application commands globally...")
                    await self.sync_all_application_commands()
                self._commands_synced = True
                print("Synced application commands with Discord.")
            except Exception as error:
                print(f"Application command sync failed: {type(error).__name__}: {error}")
                traceback.print_exc()
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(
                self._scheduled_action_loop(),
                name="memact-automod-scheduler",
            )
        await self._enforce_allowed_guilds()

    async def on_guild_join(self, guild: nextcord.Guild) -> None:
        if self.is_allowed_guild_id(guild.id):
            return
        try:
            await guild.leave()
        except (nextcord.Forbidden, nextcord.HTTPException):
            return

    async def on_application_command_error(self, interaction: nextcord.Interaction, error: Exception) -> None:
        if isinstance(error, nextcord.ApplicationCheckFailure):
            if interaction.guild is None:
                await self._reply_error(interaction, "Memact AutoMod only works inside its configured server.")
                return
            if not self.is_allowed_guild_id(interaction.guild.id):
                await self._reply_error(interaction, f"Memact AutoMod is locked to server `{self.settings.dev_guild_id}`.")
                return
        if isinstance(error, nextcord.Forbidden):
            await self._reply_error(interaction, "I don't have permission to do that. Check my role position and permissions.")
            return
        if isinstance(error, nextcord.HTTPException):
            await self._reply_error(interaction, "Discord rejected that action. Please try again or check the provided values.")
            return
        original_error = error.original if isinstance(error, nextcord.ApplicationInvokeError) else error
        print(f"Application command error: {type(original_error).__name__}: {original_error}")
        traceback.print_exception(type(original_error), original_error, original_error.__traceback__)
        await self._reply_error(interaction, f"Something went wrong: `{type(original_error).__name__}`")

    async def _reply_error(self, interaction: nextcord.Interaction, message: str) -> None:
        embed = build_embed("Memact AutoMod", message, color=0x8B0000)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    def is_allowed_guild_id(self, guild_id: int | None) -> bool:
        return guild_id is not None and (
            self.settings.dev_guild_id is None or guild_id == self.settings.dev_guild_id
        )

    async def _allowed_guild_check(self, interaction: nextcord.Interaction) -> bool:
        return self.is_allowed_guild_id(interaction.guild.id if interaction.guild else None)

    async def _enforce_allowed_guilds(self) -> None:
        if self.settings.dev_guild_id is None:
            return
        for guild in list(self.guilds):
            if guild.id == self.settings.dev_guild_id:
                continue
            try:
                await guild.leave()
            except (nextcord.Forbidden, nextcord.HTTPException):
                continue

    async def send_log(
        self,
        guild: nextcord.Guild,
        *,
        title: str,
        description: str,
        fields: list[tuple[str, str, bool]] | None = None,
    ) -> None:
        if not self.is_allowed_guild_id(guild.id):
            return
        config = self.db.get_guild_config(guild.id)
        channel_id = config["log_channel_id"]
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        embed = build_embed(title, description, fields=fields)
        try:
            await channel.send(embed=embed)
        except (nextcord.Forbidden, nextcord.HTTPException):
            return

    async def add_case(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        action: str,
        reason: str,
        *,
        points: int = 0,
        active: bool = True,
        expires_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        return self.db.add_case(
            guild_id,
            user_id,
            moderator_id,
            action,
            reason,
            points=points,
            active=active,
            expires_at=expires_at,
            metadata=metadata,
        )

    async def dm_case_notice(
        self,
        user: nextcord.abc.User,
        *,
        action: str,
        guild_name: str,
        reason: str,
        case_id: int,
        duration: timedelta | None = None,
    ) -> bool:
        fields = [("Server", guild_name, False), ("Case", str(case_id), True), ("Action", action, True)]
        if duration is not None:
            fields.append(("Duration", format_timedelta(duration), True))
        embed = build_embed("Moderation Notice", reason, fields=fields)
        return await safe_dm(user, embed=embed)

    async def apply_warning(
        self,
        guild: nextcord.Guild,
        member: nextcord.Member,
        *,
        moderator: nextcord.abc.User,
        reason: str,
        points: int,
        source: str,
        rule_name: str | None = None,
    ) -> tuple[int, int, str | None]:
        if not self.is_allowed_guild_id(guild.id):
            return 0, 0, None
        metadata = {"source": source}
        if rule_name:
            metadata["rule"] = rule_name
        case_id = await self.add_case(
            guild.id,
            member.id,
            moderator.id,
            "warn",
            reason,
            points=points,
            metadata=metadata,
        )
        total_points = self.db.get_active_warning_points(guild.id, member.id)
        await self.dm_case_notice(
            member,
            action="Warning",
            guild_name=guild.name,
            reason=f"{reason}\n\nActive warning points: {total_points}",
            case_id=case_id,
        )
        await self.send_log(
            guild,
            title="Warning Issued",
            description=f"{member.mention} received a warning.",
            fields=[
                ("Case", str(case_id), True),
                ("Points", str(points), True),
                ("Active Total", str(total_points), True),
                ("Reason", reason, False),
                ("Source", source, True),
            ],
        )

        config = self.db.get_guild_config(guild.id)
        bot_user = self.user or moderator
        escalation: str | None = None

        if total_points >= config["warn_ban_threshold"] and member.bannable:
            await member.ban(reason=f"Auto-ban after warnings: {reason}", delete_message_seconds=0)
            escalation = "ban"
            case_id = await self.add_case(
                guild.id,
                member.id,
                bot_user.id,
                "ban",
                f"Automatic ban after reaching {total_points} warning points.",
                metadata={"source": "warn_threshold"},
            )
            await self.dm_case_notice(
                member,
                action="Ban",
                guild_name=guild.name,
                reason=f"Automatic ban after reaching {total_points} warning points.",
                case_id=case_id,
            )
            await self.send_log(
                guild,
                title="Automatic Ban",
                description=f"{member.mention} was banned automatically after warning escalation.",
                fields=[("Case", str(case_id), True), ("Warning Points", str(total_points), True)],
            )
        elif total_points >= config["warn_kick_threshold"] and member.kickable:
            await member.kick(reason=f"Auto-kick after warnings: {reason}")
            escalation = "kick"
            case_id = await self.add_case(
                guild.id,
                member.id,
                bot_user.id,
                "kick",
                f"Automatic kick after reaching {total_points} warning points.",
                metadata={"source": "warn_threshold"},
            )
            await self.dm_case_notice(
                member,
                action="Kick",
                guild_name=guild.name,
                reason=f"Automatic kick after reaching {total_points} warning points.",
                case_id=case_id,
            )
            await self.send_log(
                guild,
                title="Automatic Kick",
                description=f"{member.mention} was kicked automatically after warning escalation.",
                fields=[("Case", str(case_id), True), ("Warning Points", str(total_points), True)],
            )
        elif total_points >= config["warn_timeout_threshold"]:
            timeout_minutes = max(1, int(config["warn_timeout_minutes"]))
            duration = timedelta(minutes=timeout_minutes)
            await member.edit(timeout=duration, reason=f"Auto-timeout after warnings: {reason}")
            escalation = "timeout"
            case_id = await self.add_case(
                guild.id,
                member.id,
                bot_user.id,
                "timeout",
                f"Automatic timeout after reaching {total_points} warning points.",
                expires_at=to_iso(utcnow() + duration),
                metadata={"source": "warn_threshold", "duration_minutes": timeout_minutes},
            )
            await self.dm_case_notice(
                member,
                action="Timeout",
                guild_name=guild.name,
                reason=f"Automatic timeout after reaching {total_points} warning points.",
                case_id=case_id,
                duration=duration,
            )
            await self.send_log(
                guild,
                title="Automatic Timeout",
                description=f"{member.mention} was timed out automatically after warning escalation.",
                fields=[
                    ("Case", str(case_id), True),
                    ("Warning Points", str(total_points), True),
                    ("Duration", format_timedelta(duration), True),
                ],
            )

        return case_id, total_points, escalation

    async def _scheduled_action_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.process_due_actions()
            except Exception as error:
                print(f"Scheduled action failure: {error!r}")
            await asyncio.sleep(30)

    async def process_due_actions(self) -> None:
        for action in self.db.list_due_actions(to_iso(utcnow()) or ""):
            if not self.is_allowed_guild_id(action["guild_id"]):
                self.db.delete_scheduled_action(action["id"])
                continue
            guild = self.get_guild(action["guild_id"])
            if guild is None:
                self.db.delete_scheduled_action(action["id"])
                continue

            if action["action"] == "unban":
                user = nextcord.Object(id=action["user_id"])
                reason = action["payload"].get("reason", "Temporary ban expired.")
                try:
                    await guild.unban(user, reason=reason)
                except nextcord.NotFound:
                    pass
                except (nextcord.Forbidden, nextcord.HTTPException):
                    continue
                await self.send_log(
                    guild,
                    title="Temporary Ban Expired",
                    description=f"User ID `{action['user_id']}` has been unbanned automatically.",
                )
            elif action["action"] == "untimeout":
                reason = action["payload"].get("reason", "Temporary timeout expired.")
                member = guild.get_member(action["user_id"])
                if member is None:
                    try:
                        member = await guild.fetch_member(action["user_id"])
                    except nextcord.NotFound:
                        member = None
                    except (nextcord.Forbidden, nextcord.HTTPException):
                        continue
                if member is not None:
                    try:
                        await member.edit(timeout=None, reason=reason)
                    except (nextcord.Forbidden, nextcord.HTTPException):
                        continue
                    await self.send_log(
                        guild,
                        title="Temporary Timeout Expired",
                        description=f"{member.mention} had their timeout removed automatically.",
                    )
            else:
                print(
                    f"Unsupported scheduled action {action['action']!r} "
                    f"for guild {action['guild_id']} and user {action['user_id']}; deleting it."
                )
            self.db.delete_scheduled_action(action["id"])
