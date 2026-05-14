from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import time
from datetime import datetime, timedelta

import nextcord
from nextcord.ext import commands

from bot import MemactAutoModBot
from config import ACTION_LOG_CHANNEL_ID, BOT_JOIN_ROLE_ID, COMMAND_GUILD_IDS, INTRO_CHANNEL_ID, MEMBER_JOIN_ROLE_ID, WELCOME_CHANNEL_ID
from utils.checks import is_moderator_member, require_admin
from utils.content_guard import GuardDecision, evaluate_guard_message
from utils.sentinel import SentinelDecision, content_hash, evaluate_message
from utils.ui import build_embed, send_interaction


RULE_PREFIX = "Memact Guard"
SCAM_LINK_PATTERNS = [
    "*discord-gifts*",
    "*discordgift*",
    "*free-nitro*",
    "*nitro-free*",
    "*steamcomrnunity*",
    "*steancommunity*",
    "*claim-prize*",
    "*walletconnect*",
]


class AutomodCog(commands.Cog):
    """Discord-native protection layer plus Memact onboarding behavior."""

    def __init__(self, bot: MemactAutoModBot) -> None:
        self.bot = bot
        self._native_sync_started = False
        self._sentinel_alert_cooldowns: dict[tuple[int, int, str], float] = {}
        self._message_windows: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self._content_windows: dict[tuple[int, int], deque[tuple[float, str]]] = defaultdict(deque)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._native_sync_started:
            return
        self._native_sync_started = True
        asyncio.create_task(self._sync_native_rules_for_ready_guilds(), name="memact-native-automod-sync")

    async def _sync_native_rules_for_ready_guilds(self) -> None:
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            if not self.bot.is_allowed_guild_id(guild.id):
                continue
            config = self.bot.db.get_guild_config(guild.id)
            if not config["automod_enabled"]:
                continue
            try:
                await self._ensure_native_rules(guild, enabled=True)
            except (nextcord.Forbidden, nextcord.HTTPException) as error:
                print(f"Native AutoMod setup failed for guild {guild.id}: {type(error).__name__}: {error}")

    def _log_channel_object(self, guild: nextcord.Guild) -> nextcord.Object | None:
        config = self.bot.db.get_guild_config(guild.id)
        channel_id = config["log_channel_id"] or ACTION_LOG_CHANNEL_ID
        channel = guild.get_channel(channel_id)
        if channel is None:
            return None
        return nextcord.Object(id=channel_id)

    def _block_action(self, message: str) -> nextcord.AutoModerationAction:
        return nextcord.AutoModerationAction(
            type=nextcord.AutoModerationActionType.block_message,
            metadata=nextcord.AutoModerationActionMetadata(custom_message=message),
        )

    def _alert_action(self, guild: nextcord.Guild) -> nextcord.AutoModerationAction | None:
        channel = self._log_channel_object(guild)
        if channel is None:
            return None
        return nextcord.AutoModerationAction(
            type=nextcord.AutoModerationActionType.send_alert_message,
            metadata=nextcord.AutoModerationActionMetadata(channel=channel),
        )

    def _actions(self, guild: nextcord.Guild, message: str) -> list[nextcord.AutoModerationAction]:
        actions = [self._block_action(message)]
        alert = self._alert_action(guild)
        if alert is not None:
            actions.append(alert)
        return actions

    async def _existing_memact_rules(self, guild: nextcord.Guild) -> dict[str, nextcord.AutoModerationRule]:
        rules = await guild.auto_moderation_rules()
        return {rule.name: rule for rule in rules if rule.name.startswith(f"{RULE_PREFIX}:")}

    async def _upsert_rule(
        self,
        guild: nextcord.Guild,
        existing: dict[str, nextcord.AutoModerationRule],
        *,
        name: str,
        trigger_type: nextcord.AutoModerationTriggerType,
        actions: list[nextcord.AutoModerationAction],
        trigger_metadata: nextcord.AutoModerationTriggerMetadata | None = None,
        exempt_roles: list[nextcord.Object] | None = None,
        enabled: bool,
    ) -> nextcord.AutoModerationRule:
        rule = existing.get(name)
        if rule is None:
            create_kwargs = {
                "name": name,
                "event_type": nextcord.AutoModerationEventType.message_send,
                "trigger_type": trigger_type,
                "actions": actions,
                "enabled": enabled,
                "reason": "Memact Guard native AutoMod setup.",
            }
            if trigger_metadata is not None:
                create_kwargs["trigger_metadata"] = trigger_metadata
            if exempt_roles is not None:
                create_kwargs["exempt_roles"] = exempt_roles
            return await guild.create_auto_moderation_rule(**create_kwargs)
        edit_kwargs = {
            "name": name,
            "event_type": nextcord.AutoModerationEventType.message_send,
            "actions": actions,
            "enabled": enabled,
            "reason": "Memact Guard native AutoMod refresh.",
        }
        if trigger_metadata is not None:
            edit_kwargs["trigger_metadata"] = trigger_metadata
        if exempt_roles is not None:
            edit_kwargs["exempt_roles"] = exempt_roles
        return await rule.edit(**edit_kwargs)

    async def _ensure_native_rules(self, guild: nextcord.Guild, *, enabled: bool) -> list[nextcord.AutoModerationRule]:
        existing = await self._existing_memact_rules(guild)
        config = self.bot.db.get_guild_config(guild.id)
        mention_limit = max(5, int(config["mention_threshold"]))
        exempt_roles = [
            nextcord.Object(id=role_id)
            for role_id in [*config["admin_role_ids"], *config["mod_role_ids"]]
        ][:20]
        rules = [
            await self._upsert_rule(
                guild,
                existing,
                name=f"{RULE_PREFIX}: Spam",
                trigger_type=nextcord.AutoModerationTriggerType.spam,
                actions=self._actions(guild, "Discord blocked this as spam."),
                exempt_roles=exempt_roles,
                enabled=enabled,
            ),
            await self._upsert_rule(
                guild,
                existing,
                name=f"{RULE_PREFIX}: Mention Raid",
                trigger_type=nextcord.AutoModerationTriggerType.mention_spam,
                trigger_metadata=nextcord.AutoModerationTriggerMetadata(
                    mention_total_limit=mention_limit,
                    mention_raid_protection_enabled=True,
                ),
                actions=self._actions(guild, "Discord blocked this because it mentioned too many people."),
                exempt_roles=exempt_roles,
                enabled=enabled,
            ),
            await self._upsert_rule(
                guild,
                existing,
                name=f"{RULE_PREFIX}: Hate Speech",
                trigger_type=nextcord.AutoModerationTriggerType.keyword_preset,
                trigger_metadata=nextcord.AutoModerationTriggerMetadata(
                    presets=[nextcord.KeywordPresetType.slurs],
                ),
                actions=self._actions(guild, "Discord blocked this because it looks like hate speech."),
                exempt_roles=exempt_roles,
                enabled=enabled,
            ),
            await self._upsert_rule(
                guild,
                existing,
                name=f"{RULE_PREFIX}: Scam Links",
                trigger_type=nextcord.AutoModerationTriggerType.keyword,
                trigger_metadata=nextcord.AutoModerationTriggerMetadata(keyword_filter=SCAM_LINK_PATTERNS),
                actions=self._actions(guild, "Discord blocked this because it looks like a scam link."),
                exempt_roles=exempt_roles,
                enabled=enabled,
            ),
        ]
        return rules

    async def _set_native_rules_enabled(self, guild: nextcord.Guild, enabled: bool) -> int:
        rules = await self._existing_memact_rules(guild)
        changed = 0
        for rule in rules.values():
            await rule.edit(enabled=enabled, reason="Memact Guard toggle.")
            changed += 1
        return changed

    def _build_welcome_embed(self, member: nextcord.Member) -> nextcord.Embed:
        member_count = member.guild.member_count or len(member.guild.members)
        display_name = member.display_name.strip() or member.name
        age_days = max(0, int((nextcord.utils.utcnow() - member.created_at).total_seconds() // 86400))
        templates = (
            (
                f"Welcome in, {display_name}",
                f"You are member #{member_count}. Start with a quick intro in <#{INTRO_CHANNEL_ID}> so the server can place the name with the person.",
            ),
            (
                f"{display_name} joined Memact",
                f"Fresh arrival logged. Drop a short intro in <#{INTRO_CHANNEL_ID}> and make yourself easy to welcome.",
            ),
            (
                f"Good to have you, {display_name}",
                f"Your Discord account is about {age_days} day(s) old. When you are ready, say hi in <#{INTRO_CHANNEL_ID}>.",
            ),
            (
                f"New member: {display_name}",
                f"Settle in, read the rules, and post a small intro in <#{INTRO_CHANNEL_ID}> when you get a moment.",
            ),
        )
        title, description = templates[member.id % len(templates)]
        return build_embed(
            title,
            description,
            fields=[
                ("Server", member.guild.name, True),
                ("Member Count", str(member_count), True),
                ("Intro Channel", f"<#{INTRO_CHANNEL_ID}>", True),
            ],
        )

    async def _assign_join_role(self, member: nextcord.Member, role_id: int) -> bool:
        role = member.guild.get_role(role_id)
        if role is None:
            print(f"Join role {role_id} was not found in guild {member.guild.id}.")
            return False
        if role in member.roles:
            return True
        try:
            await member.add_roles(role, reason="Automatic join role assignment.")
        except (nextcord.Forbidden, nextcord.HTTPException) as error:
            print(f"Failed to assign join role {role_id} to user {member.id}: {type(error).__name__}: {error}")
            return False
        return True

    async def _send_welcome_message(self, member: nextcord.Member) -> None:
        embed = self._build_welcome_embed(member)
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if channel is not None:
            try:
                await channel.send(
                    content=member.mention,
                    embed=embed,
                    allowed_mentions=nextcord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            except (nextcord.Forbidden, nextcord.HTTPException):
                pass
        try:
            await member.send(embed=embed)
        except (nextcord.Forbidden, nextcord.HTTPException):
            pass

    async def _acknowledge_intro_message(self, message: nextcord.Message) -> None:
        if message.guild is None:
            return
        if self.bot.db.has_intro_acknowledgement(message.guild.id, message.author.id):
            return
        if not self.bot.db.mark_intro_acknowledgement(message.guild.id, message.author.id, message_id=message.id):
            return
        try:
            await message.add_reaction("\U0001f44b")
        except (nextcord.Forbidden, nextcord.HTTPException):
            pass

    def _age_hours(self, then: datetime | None) -> float:
        if then is None:
            return 0.0
        return max(0.0, (nextcord.utils.utcnow() - then).total_seconds() / 3600)

    def _is_staff_actor(self, message: nextcord.Message, config: dict) -> bool:
        if not isinstance(message.author, nextcord.Member) or message.author.bot:
            return False
        return is_moderator_member(message.author, config)

    def _is_staff_only_channel(self, message: nextcord.Message, config: dict) -> bool:
        guild = message.guild
        if guild is None:
            return False
        channel = getattr(message.channel, "parent", None) or message.channel
        permissions_for = getattr(channel, "permissions_for", None)
        if not callable(permissions_for):
            return False
        if permissions_for(guild.default_role).view_channel:
            return False
        staff_role_ids = set(config["admin_role_ids"]) | set(config["mod_role_ids"])
        if not staff_role_ids:
            return True
        for role_id in staff_role_ids:
            role = guild.get_role(role_id)
            if role is not None and permissions_for(role).view_channel:
                return True
        return True

    def _actor_kind(self, message: nextcord.Message) -> str:
        if message.webhook_id is not None:
            return "webhook"
        if message.author.bot:
            return "bot_app"
        return "human"

    def _message_stats(self, message: nextcord.Message) -> tuple[int, int]:
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        message_window = self._message_windows[key]
        content_window = self._content_windows[key]
        while message_window and now - message_window[0] > 8:
            message_window.popleft()
        while content_window and now - content_window[0][0] > 90:
            content_window.popleft()
        digest = content_hash(message.content)
        message_window.append(now)
        content_window.append((now, digest))
        duplicate_count = sum(1 for _, item_hash in content_window if item_hash == digest)
        return len(message_window), duplicate_count

    def _guard_signal_payload(self, decision: GuardDecision) -> list[dict[str, object]]:
        return [
            {
                "category": signal.category,
                "label": signal.label,
                "severity": signal.severity,
                "confidence": round(signal.confidence, 3),
            }
            for signal in decision.signals
        ]

    async def _log_guard_action(
        self,
        message: nextcord.Message,
        decision: GuardDecision,
        *,
        event_id: int,
        deleted: bool,
        deletion_error: str | None,
        staff_only_channel: bool,
    ) -> None:
        signal_lines = [
            f"{signal.label} ({signal.category}, s{signal.severity}, {signal.confidence:.0%})"
            for signal in decision.signals
            if signal.category != "context"
        ]
        fields = [
            ("Event", f"`#{event_id}`", True),
            ("Action", "Deleted" if deleted else "Delete failed", True),
            ("Actor Type", self._actor_kind(message), True),
            ("Member/App", f"{message.author.mention} (`{message.author.id}`)", False),
            ("Channel", message.channel.mention, True),
            ("Scope", "Staff-only" if staff_only_channel else "Public", True),
            ("Category", decision.category, True),
            ("Severity", f"{decision.severity}/5", True),
            ("Confidence", f"{decision.confidence:.0%}", True),
            ("Signals", "\n".join(signal_lines) or decision.summary, False),
            ("Excerpt", decision.excerpt or "-", False),
        ]
        if deletion_error is not None:
            fields.append(("Error", deletion_error, False))
        await self.bot.send_log(
            message.guild,
            title="Memact Guard Action",
            description="A message matched Memact's local moderation guard. Every local action is logged here for staff review.",
            fields=fields,
        )

    def _sentinel_category(self, decision: SentinelDecision) -> str:
        for signal in decision.signals:
            if signal.category != "context":
                return signal.category
        return "context"

    def _sentinel_signal_payload(self, decision: SentinelDecision) -> list[dict[str, object]]:
        return [
            {
                "category": signal.category,
                "label": signal.label,
                "severity": signal.severity,
                "confidence": round(signal.confidence, 3),
            }
            for signal in decision.signals
        ]

    def _should_send_sentinel_alert(
        self,
        guild_id: int,
        user_id: int,
        category: str,
        *,
        decision: SentinelDecision,
        risk_score: float,
    ) -> bool:
        if not decision.should_alert and risk_score < 70:
            return False
        key = (guild_id, user_id, category)
        now = time.monotonic()
        if now - self._sentinel_alert_cooldowns.get(key, 0.0) < 180:
            return False
        self._sentinel_alert_cooldowns[key] = now
        return True

    async def _run_guard(self, message: nextcord.Message, config: dict) -> bool:
        if message.guild is None or not message.content:
            return False
        recent_message_count, duplicate_message_count = self._message_stats(message)
        mention_count = len(message.mentions) + len(message.role_mentions)
        if message.mention_everyone:
            mention_count += 5
        joined_at = getattr(message.author, "joined_at", None)
        staff_only_channel = self._is_staff_only_channel(message, config)
        decision = evaluate_guard_message(
            content=message.content,
            mention_count=mention_count,
            account_age_hours=self._age_hours(message.author.created_at),
            joined_age_hours=self._age_hours(joined_at),
            raid_mode=bool(config["raid_mode"]),
            is_bot_actor=message.author.bot or message.webhook_id is not None,
            is_staff_actor=self._is_staff_actor(message, config),
            staff_only_channel=staff_only_channel,
            recent_message_count=recent_message_count,
            duplicate_message_count=duplicate_message_count,
        )
        if decision is None:
            return False

        deleted = False
        deletion_error: str | None = None
        if decision.should_delete:
            try:
                await message.delete()
                deleted = True
            except (nextcord.Forbidden, nextcord.HTTPException) as error:
                deletion_error = f"{type(error).__name__}: {error}"

        event_id = self.bot.db.add_sentinel_event(
            message.guild.id,
            message.author.id,
            channel_id=message.channel.id,
            message_id=message.id,
            category=decision.category,
            severity=decision.severity,
            confidence=decision.confidence,
            summary=decision.summary,
            content_hash=decision.content_hash,
            excerpt=decision.excerpt,
            signals=self._guard_signal_payload(decision),
            action=decision.action,
            actor_kind=self._actor_kind(message),
            channel_scope="staff_only" if staff_only_channel else "public",
            deleted=deleted,
        )
        if decision.should_delete:
            await self._log_guard_action(
                message,
                decision,
                event_id=event_id,
                deleted=deleted,
                deletion_error=deletion_error,
                staff_only_channel=staff_only_channel,
            )
        return deleted

    async def _run_sentinel(self, message: nextcord.Message) -> None:
        if message.guild is None or not message.content:
            return
        config = self.bot.db.get_guild_config(message.guild.id)
        mention_count = len(message.mentions) + len(message.role_mentions)
        if message.mention_everyone:
            mention_count += 5
        joined_at = getattr(message.author, "joined_at", None)
        decision = evaluate_message(
            content=message.content,
            mention_count=mention_count,
            account_age_hours=self._age_hours(message.author.created_at),
            joined_age_hours=self._age_hours(joined_at),
            raid_mode=bool(config["raid_mode"]),
        )
        if decision is None or decision.severity < 3:
            return

        category = self._sentinel_category(decision)
        event_id = self.bot.db.add_sentinel_event(
            message.guild.id,
            message.author.id,
            channel_id=message.channel.id,
            message_id=message.id,
            category=category,
            severity=decision.severity,
            confidence=decision.confidence,
            summary=decision.summary,
            content_hash=decision.content_hash,
            excerpt=decision.excerpt,
            signals=self._sentinel_signal_payload(decision),
        )
        profile = self.bot.db.get_sentinel_profile(message.guild.id, message.author.id) or {}
        risk_score = float(profile.get("risk_score", 0.0))
        if not self._should_send_sentinel_alert(
            message.guild.id,
            message.author.id,
            category,
            decision=decision,
            risk_score=risk_score,
        ):
            return

        signal_lines = [
            f"{signal.label} ({signal.category}, s{signal.severity}, {signal.confidence:.0%})"
            for signal in decision.signals
            if signal.category != "context"
        ]
        fields = [
            ("Event", f"`#{event_id}`", True),
            ("Member", f"{message.author.mention} (`{message.author.id}`)", False),
            ("Channel", message.channel.mention, True),
            ("Severity", f"{decision.severity}/5", True),
            ("Confidence", f"{decision.confidence:.0%}", True),
            ("Risk Score", f"{risk_score:.1f}/100", True),
            ("Signals", "\n".join(signal_lines) or decision.summary, False),
            ("Excerpt", decision.excerpt or "-", False),
            ("Jump", message.jump_url, False),
        ]
        await self.bot.send_log(
            message.guild,
            title="Sentinel Alert",
            description="Silent moderation intelligence flagged this message for staff review. No automatic punishment was applied.",
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_message(self, message: nextcord.Message) -> None:
        if message.guild is None:
            return
        if not self.bot.is_allowed_guild_id(message.guild.id):
            return
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return
        config = self.bot.db.get_guild_config(message.guild.id)
        deleted = False
        if config["automod_enabled"]:
            deleted = await self._run_guard(message, config)
        if not deleted:
            await self._run_sentinel(message)
        if not message.author.bot and not deleted and message.channel.id == INTRO_CHANNEL_ID:
            await self._acknowledge_intro_message(message)

    @commands.Cog.listener()
    async def on_member_join(self, member: nextcord.Member) -> None:
        if not self.bot.is_allowed_guild_id(member.guild.id):
            return
        if member.bot:
            await self._assign_join_role(member, BOT_JOIN_ROLE_ID)
            return

        config = self.bot.db.get_guild_config(member.guild.id)
        required_hours = max(config["min_account_age_hours"], 72 if config["raid_mode"] else 0)
        if required_hours <= 0:
            await self._assign_join_role(member, MEMBER_JOIN_ROLE_ID)
            await self._send_welcome_message(member)
            return

        age = nextcord.utils.utcnow() - member.created_at
        age_hours = age.total_seconds() / 3600
        if age_hours >= required_hours:
            await self._assign_join_role(member, MEMBER_JOIN_ROLE_ID)
            await self._send_welcome_message(member)
            return

        reason = f"Account younger than required minimum of {required_hours} hours."
        try:
            await member.kick(reason=reason)
        except (nextcord.Forbidden, nextcord.HTTPException):
            return
        moderator_id = self.bot.user.id if self.bot.user is not None else self.bot.settings.application_id
        if moderator_id is None:
            return
        case_id = await self.bot.add_case(member.guild.id, member.id, moderator_id, "kick", reason, metadata={"source": "join_screen"})
        await self.bot.send_log(
            member.guild,
            title="Join Screen Kick",
            description=f"{member.mention} was removed automatically on join.",
            fields=[("Case", str(case_id), True), ("Age Hours", f"{age_hours:.2f}", True), ("Reason", reason, False)],
        )

    @commands.Cog.listener()
    async def on_auto_moderation_action_execution(
        self,
        execution: nextcord.AutoModerationActionExecution,
    ) -> None:
        guild = execution.guild
        if guild is None or not self.bot.is_allowed_guild_id(guild.id):
            return
        channel = execution.channel.mention if execution.channel is not None else f"`{execution.channel_id}`"
        member = execution.member.mention if execution.member is not None else f"`{execution.member_id}`"
        fields = [
            ("Member", member, True),
            ("Channel", channel, True),
            ("Trigger", str(execution.rule_trigger_type).replace("AutoModerationTriggerType.", ""), True),
        ]
        if execution.matched_keyword:
            fields.append(("Matched", execution.matched_keyword, True))
        if execution.matched_content:
            fields.append(("Content", execution.matched_content[:900], False))
        await self.bot.send_log(
            guild,
            title="Discord AutoMod Action",
            description=f"Native rule `{execution.rule_id}` handled a message.",
            fields=fields,
        )

    @nextcord.slash_command(
        description="Native Discord AutoMod controls",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_guild=True),
    )
    async def automod(self, interaction: nextcord.Interaction) -> None:
        pass

    @automod.subcommand(description="Show native AutoMod protection status")
    async def view(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        config = self.bot.db.get_guild_config(interaction.guild.id)
        try:
            rules = await self._existing_memact_rules(interaction.guild)
        except (nextcord.Forbidden, nextcord.HTTPException):
            rules = {}
        lines = []
        for name in (
            f"{RULE_PREFIX}: Spam",
            f"{RULE_PREFIX}: Mention Raid",
            f"{RULE_PREFIX}: Hate Speech",
            f"{RULE_PREFIX}: Scam Links",
        ):
            rule = rules.get(name)
            if rule is None:
                lines.append(f"`{name}`: missing")
            else:
                lines.append(f"`{name}`: {'enabled' if rule.enabled else 'disabled'}")
        await send_interaction(
            interaction,
            embed=build_embed(
                "Memact Guard",
                "Native Discord AutoMod handles platform hard-blocks, while Memact Guard deletes logged local violations such as strong profanity, scam/promo links, bot/app spam, and extremist references.",
                fields=[
                    ("Master Switch", "On" if config["automod_enabled"] else "Off", True),
                    ("Backend", "Discord AutoMod + local deletion guard + silent Sentinel intelligence + Memact staff workflow", False),
                    ("Rules", "\n".join(lines) if lines else "No Memact Guard rules found.", False),
                ],
            ),
        )

    @automod.subcommand(description="Create or refresh the Memact Guard native AutoMod rules")
    async def install(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        await interaction.response.defer(ephemeral=True)
        self.bot.db.set_config_value(interaction.guild.id, "automod_enabled", 1)
        try:
            rules = await self._ensure_native_rules(interaction.guild, enabled=True)
        except nextcord.Forbidden:
            await interaction.followup.send("I need Manage Guild permissions to configure Discord AutoMod.", ephemeral=True)
            return
        except nextcord.HTTPException as error:
            await interaction.followup.send(f"Discord rejected the AutoMod setup: `{type(error).__name__}`.", ephemeral=True)
            return
        await interaction.followup.send(
            embed=build_embed(
                "Memact Guard Installed",
                "Native Discord AutoMod rules were created or refreshed.",
                fields=[("Rules", "\n".join(f"`{rule.name}`" for rule in rules), False)],
            ),
            ephemeral=True,
        )

    @automod.subcommand(description="Enable or disable Memact Guard")
    async def toggle(self, interaction: nextcord.Interaction, enabled: bool) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        await interaction.response.defer(ephemeral=True)
        self.bot.db.set_config_value(interaction.guild.id, "automod_enabled", int(enabled))
        try:
            changed = await self._set_native_rules_enabled(interaction.guild, enabled)
            if changed == 0 and enabled:
                await self._ensure_native_rules(interaction.guild, enabled=True)
        except (nextcord.Forbidden, nextcord.HTTPException) as error:
            await interaction.followup.send(f"Could not update native AutoMod rules: `{type(error).__name__}`.", ephemeral=True)
            return
        await interaction.followup.send(
            embed=build_embed("Memact Guard Updated", f"Native AutoMod is now {'enabled' if enabled else 'disabled'}."),
            ephemeral=True,
        )

    @automod.subcommand(description="Set the mention raid limit used by native AutoMod")
    async def mention_limit(
        self,
        interaction: nextcord.Interaction,
        limit: int = nextcord.SlashOption(min_value=5, max_value=50),
    ) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        await interaction.response.defer(ephemeral=True)
        self.bot.db.set_config_value(interaction.guild.id, "mention_threshold", limit)
        try:
            await self._ensure_native_rules(interaction.guild, enabled=True)
        except (nextcord.Forbidden, nextcord.HTTPException) as error:
            await interaction.followup.send(f"Saved the setting, but native rule refresh failed: `{type(error).__name__}`.", ephemeral=True)
            return
        await interaction.followup.send(
            embed=build_embed("Mention Raid Limit Updated", f"Native AutoMod now blocks messages with `{limit}` or more mentions."),
            ephemeral=True,
        )


def setup(bot: MemactAutoModBot) -> None:
    bot.add_cog(AutomodCog(bot))
