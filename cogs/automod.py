from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
import asyncio
import re

import nextcord
from nextcord.ext import commands

from bot import MemactAutoModBot
from config import BOT_JOIN_ROLE_ID, COMMAND_GUILD_IDS, INTRO_CHANNEL_ID, MEMBER_JOIN_ROLE_ID, WELCOME_CHANNEL_ID
from utils.checks import is_moderator_member, require_admin
from utils.blocklist import (
    DATASET_PRESETS,
    LENIENT_DATASET_PRESETS,
    LIGHT_WORD_ALLOWLIST,
    compile_blocked_term_pattern,
    fetch_dataset_terms_sync,
    fetch_lenient_terms_sync,
)
from utils.moderation_engine import AutomodDecision, evaluate_automod
from utils.ui import build_embed, send_interaction


PROMO_KEYWORDS = (
    "promo",
    "promotion",
    "discount",
    "coupon",
    "sale",
    "deal",
    "offer",
    "limited time",
    "giveaway",
    "free",
    "subscribe",
    "follow",
    "check out",
    "visit",
    "join",
    "invite",
    "server",
    "guild",
    "discord",
    "youtube",
    "twitch",
    "instagram",
    "tiktok",
    "twitter",
    "x.com",
    "shop",
    "store",
    "merch",
    "commission",
)

PROMO_DATASET_PATH = Path(__file__).resolve().parent.parent / "data" / "promo_keywords.txt"


class AutomodCog(commands.Cog):
    def __init__(self, bot: MemactAutoModBot) -> None:
        self.bot = bot
        self.spam_history: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self.repeat_history: dict[tuple[int, int], deque[tuple[float, str]]] = defaultdict(deque)
        self.soft_violation_history: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self.blocked_word_cache: dict[int, list[tuple[str, re.Pattern[str]]]] = {}
        self._last_history_cleanup_ts = 0.0
        self._dataset_seed_task: asyncio.Task | None = None
        self._dataset_seed_started = False

    def cog_unload(self) -> None:
        if self._dataset_seed_task is not None:
            self._dataset_seed_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._dataset_seed_started:
            return
        self._dataset_seed_started = True
        self._dataset_seed_task = asyncio.create_task(
            self._seed_startup_datasets(),
            name="memact-automod-dataset-seed",
        )

    def _load_promo_dataset_terms(self) -> list[str]:
        with PROMO_DATASET_PATH.open("r", encoding="utf-8") as handle:
            return [line.strip() for line in handle.readlines() if line.strip()]

    async def _seed_startup_datasets(self) -> None:
        await self.bot.wait_until_ready()
        guild_ids = [guild.id for guild in self.bot.guilds if self.bot.is_allowed_guild_id(guild.id)]
        if not guild_ids:
            return

        blocked_terms: list[str] = []
        lenient_terms: list[str] = []
        promo_terms: list[str] = []

        try:
            blocked_terms = await asyncio.to_thread(fetch_dataset_terms_sync, "strong_en")
        except Exception as error:
            print(f"Startup blocked-word dataset seed failed: {type(error).__name__}: {error}")

        try:
            lenient_terms = await asyncio.to_thread(fetch_lenient_terms_sync, "mild_en")
        except Exception as error:
            print(f"Startup lenient-word dataset seed failed: {type(error).__name__}: {error}")

        try:
            promo_terms = await asyncio.to_thread(self._load_promo_dataset_terms)
        except OSError as error:
            print(f"Startup promo keyword dataset seed failed: {type(error).__name__}: {error}")

        for guild_id in guild_ids:
            added_blocked = self.bot.db.bulk_add_blocked_words(guild_id, blocked_terms) if blocked_terms else 0
            added_lenient = self.bot.db.bulk_add_lenient_words(guild_id, lenient_terms) if lenient_terms else 0
            removed_lenient_from_blocklist = 0
            for term in LIGHT_WORD_ALLOWLIST:
                if self.bot.db.remove_blocked_word(guild_id, term):
                    removed_lenient_from_blocklist += 1
            for term in self.bot.db.list_lenient_words(guild_id):
                if self.bot.db.remove_blocked_word(guild_id, term):
                    removed_lenient_from_blocklist += 1
            added_promo = self.bot.db.bulk_add_promo_keywords(guild_id, promo_terms) if promo_terms else 0
            self._invalidate_blocked_word_cache(guild_id)
            print(
                "Startup datasets seeded for guild "
                f"{guild_id}: blocked +{added_blocked}, lenient +{added_lenient}, "
                f"blocked lenient removed {removed_lenient_from_blocklist}, promo +{added_promo}."
            )

    def _invalidate_blocked_word_cache(self, guild_id: int) -> None:
        self.blocked_word_cache.pop(guild_id, None)

    def _get_blocked_word_patterns(self, guild_id: int) -> list[tuple[str, re.Pattern[str]]]:
        cached = self.blocked_word_cache.get(guild_id)
        if cached is not None:
            return cached
        lenient_words = set(self.bot.db.list_lenient_words(guild_id))
        lenient_words.update(LIGHT_WORD_ALLOWLIST)
        patterns = [
            (term, compile_blocked_term_pattern(term))
            for term in self.bot.db.list_blocked_words(guild_id)
            if term not in lenient_words
        ]
        self.blocked_word_cache[guild_id] = patterns
        return patterns

    def _cleanup_history_cache(self, now_ts: float, *, spam_window_seconds: int, repeat_window_seconds: int) -> None:
        if now_ts - self._last_history_cleanup_ts < 300:
            return

        for key, history in list(self.spam_history.items()):
            while history and now_ts - history[0] > spam_window_seconds:
                history.popleft()
            if not history:
                self.spam_history.pop(key, None)

        for key, history in list(self.repeat_history.items()):
            while history and now_ts - history[0][0] > repeat_window_seconds:
                history.popleft()
            if not history:
                self.repeat_history.pop(key, None)

        self._last_history_cleanup_ts = now_ts

    def _clear_member_history(self, guild_id: int, user_id: int) -> None:
        key = (guild_id, user_id)
        self.spam_history.pop(key, None)
        self.repeat_history.pop(key, None)
        self.soft_violation_history.pop(key, None)

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
            print(
                f"Failed to assign join role {role_id} to user {member.id} "
                f"in guild {member.guild.id}: {type(error).__name__}: {error}"
            )
            return False
        return True

    def _build_welcome_embed(self, member: nextcord.Member) -> nextcord.Embed:
        return build_embed(
            f"Welcome to {member.guild.name}",
            f"Please post your intro in the channel <#{INTRO_CHANNEL_ID}> so everyone can get to know you.",
        )

    async def _send_welcome_dm(self, member: nextcord.Member, embed: nextcord.Embed) -> None:
        try:
            await member.send(embed=embed)
            return
        except (nextcord.Forbidden, nextcord.HTTPException) as error:
            print(
                f"Failed to send welcome DM to user {member.id} "
                f"in guild {member.guild.id}: {type(error).__name__}: {error}"
            )

    async def _send_welcome_message(self, member: nextcord.Member) -> None:
        embed = self._build_welcome_embed(member)
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if channel is None:
            print(f"Welcome channel {WELCOME_CHANNEL_ID} was not found in guild {member.guild.id}.")
        else:
            try:
                await channel.send(
                    content=member.mention,
                    embed=embed,
                    allowed_mentions=nextcord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            except (nextcord.Forbidden, nextcord.HTTPException) as error:
                print(
                    f"Failed to send welcome message for user {member.id} "
                    f"in guild {member.guild.id}: {type(error).__name__}: {error}"
                )

        await self._send_welcome_dm(member, embed)

    async def _acknowledge_intro_message(self, message: nextcord.Message) -> None:
        if self.bot.db.has_intro_acknowledgement(message.guild.id, message.author.id):
            return
        if not self.bot.db.mark_intro_acknowledgement(
            message.guild.id,
            message.author.id,
            message_id=message.id,
        ):
            return

        embed = build_embed(
            "Thanks for the intro",
            "Thanks for introducing yourself. We're glad to have you here.",
        )
        try:
            await message.reply(embed=embed, mention_author=False)
        except (nextcord.Forbidden, nextcord.HTTPException) as error:
            print(
                f"Failed to reply to intro message {message.id} "
                f"in guild {message.guild.id}: {type(error).__name__}: {error}"
            )
        try:
            await message.add_reaction("\U0001f44b")
        except (nextcord.Forbidden, nextcord.HTTPException) as error:
            print(
                f"Failed to react to intro message {message.id} "
                f"in guild {message.guild.id}: {type(error).__name__}: {error}"
            )

    def _record_soft_violation(self, guild_id: int, user_id: int, now_ts: float) -> int:
        key = (guild_id, user_id)
        history = self.soft_violation_history[key]
        history.append(now_ts)
        while history and now_ts - history[0] > 900:
            history.popleft()
        if not history:
            self.soft_violation_history.pop(key, None)
        return len(history)

    async def _handle_automod_decision(
        self,
        message: nextcord.Message,
        *,
        decision: AutomodDecision,
    ) -> None:
        if not isinstance(message.author, nextcord.Member):
            return
        if decision.action == "none":
            return

        if decision.delete_message:
            try:
                await message.delete()
            except (nextcord.Forbidden, nextcord.HTTPException):
                pass

        warning_points = decision.warn_points
        soft_count = 0
        if warning_points <= 0 and decision.soft_strike:
            soft_count = self._record_soft_violation(
                message.guild.id,
                message.author.id,
                message.created_at.timestamp(),
            )
            if soft_count >= 3:
                warning_points = 1

        if warning_points > 0:
            moderator = self.bot.user or message.author
            reason = decision.reason
            if soft_count >= 3:
                reason = f"Repeated soft automod violations ({soft_count}/3). Latest: {decision.reason}"
            await self.bot.apply_warning(
                message.guild,
                message.author,
                moderator=moderator,
                reason=reason,
                points=warning_points,
                source="automod",
                rule_name="Automod scoring",
            )
            return

        action_label = "Deleted" if decision.delete_message else "Logged"
        await self.bot.send_log(
            message.guild,
            title=f"Automod {action_label}",
            description=decision.reason,
            fields=[
                ("Member", message.author.mention, True),
                ("Channel", message.channel.mention, True),
                ("Score", f"{decision.score:.1f}", True),
                ("Signals", ", ".join(signal.kind for signal in decision.signals), False),
            ],
        )

    def _check_spam(self, guild_id: int, user_id: int, content: str, *, config: dict, now_ts: float) -> tuple[bool, str | None]:
        self._cleanup_history_cache(
            now_ts,
            spam_window_seconds=config["spam_window_seconds"],
            repeat_window_seconds=config["repeat_window_seconds"],
        )
        key = (guild_id, user_id)
        if config["spam_filter_enabled"]:
            history = self.spam_history[key]
            history.append(now_ts)
            while history and now_ts - history[0] > config["spam_window_seconds"]:
                history.popleft()
            if not history:
                self.spam_history.pop(key, None)
            if len(history) >= config["spam_threshold"]:
                return True, "No spam"

        normalized = " ".join(content.lower().split())
        if config["repeat_filter_enabled"]:
            repeat_history = self.repeat_history[key]
            repeat_history.append((now_ts, normalized))
            while repeat_history and now_ts - repeat_history[0][0] > config["repeat_window_seconds"]:
                repeat_history.popleft()
            if not repeat_history:
                self.repeat_history.pop(key, None)
            if normalized and sum(1 for _, value in repeat_history if value == normalized) >= config["repeat_threshold"]:
                return True, "Repeat spam"

        return False, None

    @commands.Cog.listener()
    async def on_message(self, message: nextcord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, nextcord.Member):
            return
        if not self.bot.is_allowed_guild_id(message.guild.id):
            return

        config = self.bot.db.get_guild_config(message.guild.id)
        content = message.content or ""
        should_run_automod = config["automod_enabled"] and not is_moderator_member(message.author, config)

        if should_run_automod and content:
            spam_triggered = False
            repeat_triggered = False
            if config["spam_filter_enabled"] or config["repeat_filter_enabled"]:
                triggered, rule_name = self._check_spam(
                    message.guild.id,
                    message.author.id,
                    content,
                    config=config,
                    now_ts=message.created_at.timestamp(),
                )
                spam_triggered = bool(triggered and rule_name == "No spam")
                repeat_triggered = bool(triggered and rule_name != "No spam")

            now = message.created_at
            account_age_hours = (now - message.author.created_at).total_seconds() / 3600
            joined_at = message.author.joined_at or now
            joined_age_hours = (now - joined_at).total_seconds() / 3600
            promo_keywords = set(PROMO_KEYWORDS)
            promo_keywords.update(self.bot.db.list_promo_keywords(message.guild.id))
            decision = evaluate_automod(
                content=content,
                config=config,
                blocked_patterns=self._get_blocked_word_patterns(message.guild.id),
                promo_keywords=promo_keywords,
                mention_count=len(message.mentions),
                account_age_hours=account_age_hours,
                joined_age_hours=joined_age_hours,
                has_attachments=bool(message.attachments or message.stickers),
                spam_triggered=spam_triggered,
                repeat_triggered=repeat_triggered,
            )
            if decision.action != "none":
                await self._handle_automod_decision(message, decision=decision)
                return


        if message.channel.id == INTRO_CHANNEL_ID:
            await self._acknowledge_intro_message(message)

    @commands.Cog.listener()
    async def on_member_join(self, member: nextcord.Member) -> None:
        if not self.bot.is_allowed_guild_id(member.guild.id):
            return
        if member.bot:
            await self._assign_join_role(member, BOT_JOIN_ROLE_ID)
            return

        config = self.bot.db.get_guild_config(member.guild.id)
        if not config["raid_mode"] and config["min_account_age_hours"] <= 0:
            await self._assign_join_role(member, MEMBER_JOIN_ROLE_ID)
            await self._send_welcome_message(member)
            return

        required_hours = max(config["min_account_age_hours"], 72 if config["raid_mode"] else 0)
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
    async def on_member_remove(self, member: nextcord.Member) -> None:
        self._clear_member_history(member.guild.id, member.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: nextcord.Guild) -> None:
        for key in [key for key in self.spam_history if key[0] == guild.id]:
            self.spam_history.pop(key, None)
        for key in [key for key in self.repeat_history if key[0] == guild.id]:
            self.repeat_history.pop(key, None)
        for key in [key for key in self.soft_violation_history if key[0] == guild.id]:
            self.soft_violation_history.pop(key, None)
        self.blocked_word_cache.pop(guild.id, None)

    @nextcord.slash_command(
        description="Automod configuration commands",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_guild=True),
    )
    async def automod(self, interaction: nextcord.Interaction) -> None:
        pass

    @automod.subcommand(description="Show current automod settings")
    async def view(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        config = self.bot.db.get_guild_config(interaction.guild.id)
        blocked_words = self.bot.db.list_blocked_words(interaction.guild.id)
        lenient_words = self.bot.db.list_lenient_words(interaction.guild.id)
        promo_keywords = self.bot.db.list_promo_keywords(interaction.guild.id)
        await send_interaction(
            interaction,
            embed=build_embed(
                "Automod Settings",
                "Current automod toggles and thresholds.",
                fields=[
                    ("Enabled", "Yes" if config["automod_enabled"] else "No", True),
                    ("Invite Filter", "On" if config["invite_filter_enabled"] else "Off", True),
                    ("Caps Filter", "On" if config["caps_filter_enabled"] else "Off", True),
                    ("Spam Filter", "On" if config["spam_filter_enabled"] else "Off", True),
                    ("Repeat Filter", "On" if config["repeat_filter_enabled"] else "Off", True),
                    ("Mention Filter", "On" if config["mention_filter_enabled"] else "Off", True),
                    ("Mode", "Scored and conservative", True),
                    ("Blocked Words", f"{len(blocked_words)} configured" if blocked_words else "None", False),
                    ("Lenient Words", f"{len(lenient_words)} allowlisted" if lenient_words else "None", False),
                    ("Promo Keywords", f"{len(promo_keywords)} configured" if promo_keywords else "None", False),
                    ("Caps Threshold", f"{config['caps_ratio']:.0%} with minimum {max(config['caps_min_length'], 24)} letters", False),
                    ("Spam Threshold", f"{config['spam_threshold']} messages / {config['spam_window_seconds']}s", False),
                    ("Repeat Threshold", f"{config['repeat_threshold']} duplicates / {config['repeat_window_seconds']}s", False),
                    ("Mention Threshold", str(config["mention_threshold"]), False),
                ],
            ),
        )

    @automod.subcommand(description="Toggle a specific automod filter")
    async def toggle(
        self,
        interaction: nextcord.Interaction,
        filter_name: str = nextcord.SlashOption(
            choices={
                "Automod": "automod_enabled",
                "Invite Filter": "invite_filter_enabled",
                "Caps Filter": "caps_filter_enabled",
                "Spam Filter": "spam_filter_enabled",
                "Repeat Filter": "repeat_filter_enabled",
                "Mention Filter": "mention_filter_enabled",
            }
        ),
        enabled: bool = True,
    ) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        self.bot.db.set_config_value(interaction.guild.id, filter_name, int(enabled))
        await send_interaction(interaction, embed=build_embed("Automod Updated", f"`{filter_name}` is now {'enabled' if enabled else 'disabled'}."))

    @automod.subcommand(description="Add a blocked word")
    async def add_blocked_word(self, interaction: nextcord.Interaction, term: str) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        added = self.bot.db.add_blocked_word(interaction.guild.id, term)
        if not added:
            await send_interaction(interaction, content="That blocked word is already present or invalid.", ephemeral=True)
            return
        self._invalidate_blocked_word_cache(interaction.guild.id)
        await send_interaction(interaction, embed=build_embed("Blocked Word Added", f"Added `{term.lower()}` to the blocked words list."))

    @automod.subcommand(description="Remove a blocked word")
    async def remove_blocked_word(self, interaction: nextcord.Interaction, term: str) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        removed = self.bot.db.remove_blocked_word(interaction.guild.id, term)
        if not removed:
            await send_interaction(interaction, content="That blocked word was not found.", ephemeral=True)
            return
        self._invalidate_blocked_word_cache(interaction.guild.id)
        await send_interaction(interaction, embed=build_embed("Blocked Word Removed", f"Removed `{term.lower()}` from the blocked words list."))

    @automod.subcommand(description="List blocked words")
    async def blocked_words(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        words = self.bot.db.list_blocked_words(interaction.guild.id)
        if not words:
            await send_interaction(interaction, embed=build_embed("Blocked Words", "No blocked words configured."))
            return
        await send_interaction(
            interaction,
            embed=build_embed(
                "Blocked Words",
                f"{len(words)} blocked terms are configured.\n\nUse `/automod import_dataset` to refresh the curated online list, or `/automod add_blocked_word` and `/automod remove_blocked_word` for manual changes.",
            ),
        )

    @automod.subcommand(description="Import blocked words from a curated online dataset")
    async def import_dataset(
        self,
        interaction: nextcord.Interaction,
        dataset: str = nextcord.SlashOption(
            required=False,
            default="strong_en",
            choices={
                "Strong English (Recommended)": "strong_en",
                "Strong English + Hindi": "strong_en_hi",
                "LDNOOBW English": "ldnoobw_en",
                "LDNOOBW Hindi": "ldnoobw_hi",
                "LDNOOBW English + Hindi": "ldnoobw_en_hi",
            },
        ),
        mode: str = nextcord.SlashOption(
            required=False,
            default="merge",
            choices={
                "Merge (Recommended)": "merge",
                "Replace Existing": "replace",
            },
        ),
    ) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            imported_terms = await asyncio.to_thread(fetch_dataset_terms_sync, dataset)
        except Exception as error:
            print(f"Dataset import failed for {dataset}: {type(error).__name__}: {error}")
            await interaction.followup.send(
                embed=build_embed(
                    "Dataset Import Failed",
                    "Could not fetch or parse the selected online dataset. Please try again later.",
                ),
                ephemeral=True,
            )
            return

        removed = 0
        if mode == "replace":
            removed = self.bot.db.clear_blocked_words(interaction.guild.id)
        added = self.bot.db.bulk_add_blocked_words(interaction.guild.id, imported_terms)
        lenient_removed = 0
        for term in LIGHT_WORD_ALLOWLIST:
            if self.bot.db.remove_blocked_word(interaction.guild.id, term):
                lenient_removed += 1
        for term in self.bot.db.list_lenient_words(interaction.guild.id):
            if self.bot.db.remove_blocked_word(interaction.guild.id, term):
                lenient_removed += 1
        self._invalidate_blocked_word_cache(interaction.guild.id)
        total = self.bot.db.count_blocked_words(interaction.guild.id)
        label = DATASET_PRESETS[dataset]["label"]
        await interaction.followup.send(
            embed=build_embed(
                "Dataset Imported",
                f"Imported blocked words from **{label}**.",
                fields=[
                    ("Mode", mode.title(), True),
                    ("Removed", str(removed), True),
                    ("Added", str(added), True),
                    ("Lenient Removed", str(lenient_removed), True),
                    ("Total Configured", str(total), True),
                ],
            ),
            ephemeral=True,
        )

    @automod.subcommand(description="Import a lenient-word allowlist dataset")
    async def import_lenient_dataset(
        self,
        interaction: nextcord.Interaction,
        dataset: str = nextcord.SlashOption(
            required=False,
            default="mild_en",
            choices={
                "Mild English (Recommended)": "mild_en",
            },
        ),
        mode: str = nextcord.SlashOption(
            required=False,
            default="merge",
            choices={
                "Merge (Recommended)": "merge",
                "Replace Existing": "replace",
            },
        ),
    ) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            imported_terms = await asyncio.to_thread(fetch_lenient_terms_sync, dataset)
        except Exception as error:
            print(f"Lenient dataset import failed for {dataset}: {type(error).__name__}: {error}")
            await interaction.followup.send(
                embed=build_embed(
                    "Lenient Dataset Import Failed",
                    "Could not fetch or parse the selected lenient dataset. Please try again later.",
                ),
                ephemeral=True,
            )
            return

        removed = 0
        if mode == "replace":
            removed = self.bot.db.clear_lenient_words(interaction.guild.id)
        added = self.bot.db.bulk_add_lenient_words(interaction.guild.id, imported_terms)
        lenient_removed = 0
        for term in imported_terms:
            if self.bot.db.remove_blocked_word(interaction.guild.id, term):
                lenient_removed += 1
        self._invalidate_blocked_word_cache(interaction.guild.id)
        total = len(self.bot.db.list_lenient_words(interaction.guild.id))
        label = LENIENT_DATASET_PRESETS[dataset]["label"]
        await interaction.followup.send(
            embed=build_embed(
                "Lenient Dataset Imported",
                f"Imported allowlisted words from **{label}**.",
                fields=[
                    ("Mode", mode.title(), True),
                    ("Removed", str(removed), True),
                    ("Added", str(added), True),
                    ("Removed From Blocklist", str(lenient_removed), True),
                    ("Total Lenient", str(total), True),
                ],
            ),
            ephemeral=True,
        )

    @automod.subcommand(description="Import a promotion keyword dataset")
    async def import_promo_dataset(
        self,
        interaction: nextcord.Interaction,
        mode: str = nextcord.SlashOption(
            required=False,
            default="merge",
            choices={
                "Merge (Recommended)": "merge",
                "Replace Existing": "replace",
            },
        ),
    ) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        await interaction.response.defer(ephemeral=True)
        dataset_path = "data/promo_keywords.txt"
        try:
            with open(dataset_path, "r", encoding="utf-8") as handle:
                terms = [line.strip() for line in handle.readlines() if line.strip()]
        except OSError:
            await interaction.followup.send(
                embed=build_embed(
                    "Promo Dataset Import Failed",
                    "Could not read the bundled promotion keyword dataset.",
                ),
                ephemeral=True,
            )
            return

        removed = 0
        if mode == "replace":
            removed = self.bot.db.clear_promo_keywords(interaction.guild.id)
        added = self.bot.db.bulk_add_promo_keywords(interaction.guild.id, terms)
        total = len(self.bot.db.list_promo_keywords(interaction.guild.id))
        await interaction.followup.send(
            embed=build_embed(
                "Promo Dataset Imported",
                "Imported bundled promotion keywords.",
                fields=[
                    ("Mode", mode.title(), True),
                    ("Removed", str(removed), True),
                    ("Added", str(added), True),
                    ("Total Promo Keywords", str(total), True),
                ],
            ),
            ephemeral=True,
        )

    @automod.subcommand(description="Adjust automod thresholds")
    async def settings(
        self,
        interaction: nextcord.Interaction,
        caps_ratio_percent: int = nextcord.SlashOption(required=False, default=75, min_value=1, max_value=100),
        caps_min_length: int = nextcord.SlashOption(required=False, default=24, min_value=24, max_value=200),
        mention_threshold: int = nextcord.SlashOption(required=False, default=5, min_value=1, max_value=50),
        spam_threshold: int = nextcord.SlashOption(required=False, default=6, min_value=2, max_value=50),
        spam_window_seconds: int = nextcord.SlashOption(required=False, default=12, min_value=2, max_value=300),
        repeat_threshold: int = nextcord.SlashOption(required=False, default=3, min_value=2, max_value=20),
        repeat_window_seconds: int = nextcord.SlashOption(required=False, default=90, min_value=5, max_value=1800),
    ) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return
        self.bot.db.set_config_value(interaction.guild.id, "caps_ratio", caps_ratio_percent / 100)
        self.bot.db.set_config_value(interaction.guild.id, "caps_min_length", caps_min_length)
        self.bot.db.set_config_value(interaction.guild.id, "mention_threshold", mention_threshold)
        self.bot.db.set_config_value(interaction.guild.id, "spam_threshold", spam_threshold)
        self.bot.db.set_config_value(interaction.guild.id, "spam_window_seconds", spam_window_seconds)
        self.bot.db.set_config_value(interaction.guild.id, "repeat_threshold", repeat_threshold)
        self.bot.db.set_config_value(interaction.guild.id, "repeat_window_seconds", repeat_window_seconds)
        await send_interaction(
            interaction,
            embed=build_embed(
                "Automod Thresholds Updated",
                f"Caps {caps_ratio_percent}% / {caps_min_length} letters, mentions {mention_threshold}, spam {spam_threshold}/{spam_window_seconds}s, repeats {repeat_threshold}/{repeat_window_seconds}s.",
            ),
        )


def setup(bot: MemactAutoModBot) -> None:
    bot.add_cog(AutomodCog(bot))
