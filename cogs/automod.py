from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta
import asyncio
import re

import nextcord
from nextcord.ext import commands

from bot import MemactAutoModBot
from config import BOT_JOIN_ROLE_ID, COMMAND_GUILD_IDS, INTRO_CHANNEL_ID, MEMBER_JOIN_ROLE_ID, WELCOME_CHANNEL_ID
from utils.checks import is_moderator_member, require_admin
from utils.blocklist import DATASET_PRESETS, LIGHT_WORD_ALLOWLIST, compile_blocked_term_pattern, fetch_dataset_terms_sync
from utils.ui import build_embed, send_interaction


INVITE_RE = re.compile(r"(discord\.gg|discord\.com/invite)/[A-Za-z0-9-]+", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
SUSPICIOUS_LINK_TOKENS = ("discord-gifts", "nitro-free", "steamcomrnunity", "free-nitro", "claim-prize")


class AutomodCog(commands.Cog):
    def __init__(self, bot: MemactAutoModBot) -> None:
        self.bot = bot
        self.spam_history: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self.repeat_history: dict[tuple[int, int], deque[tuple[float, str]]] = defaultdict(deque)
        self.blocked_word_cache: dict[int, list[tuple[str, re.Pattern[str]]]] = {}
        self._last_history_cleanup_ts = 0.0

    def _invalidate_blocked_word_cache(self, guild_id: int) -> None:
        self.blocked_word_cache.pop(guild_id, None)

    def _get_blocked_word_patterns(self, guild_id: int) -> list[tuple[str, re.Pattern[str]]]:
        cached = self.blocked_word_cache.get(guild_id)
        if cached is not None:
            return cached
        patterns = [
            (term, compile_blocked_term_pattern(term))
            for term in self.bot.db.list_blocked_words(guild_id)
            if term not in LIGHT_WORD_ALLOWLIST
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

    async def _handle_violation(
        self,
        message: nextcord.Message,
        *,
        rule_name: str,
        reason: str,
        points: int,
    ) -> None:
        if not isinstance(message.author, nextcord.Member):
            return
        try:
            await message.delete()
        except (nextcord.Forbidden, nextcord.HTTPException):
            pass
        moderator = self.bot.user or message.author
        await self.bot.apply_warning(
            message.guild,
            message.author,
            moderator=moderator,
            reason=reason,
            points=points,
            source="automod",
            rule_name=rule_name,
        )

    def _caps_ratio(self, text: str) -> tuple[int, float]:
        letters = [char for char in text if char.isalpha()]
        if not letters:
            return 0, 0.0
        uppercase = sum(1 for char in letters if char.isupper())
        return len(letters), uppercase / len(letters)

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
                return True, "No spam"

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
            normalized_content = content.casefold()
            for word, pattern in self._get_blocked_word_patterns(message.guild.id):
                if pattern.search(normalized_content):
                    await self._handle_violation(
                        message,
                        rule_name="Be respectful",
                        reason=f"Blocked word detected: `{word}`.",
                        points=2,
                    )
                    return

            if any(token in normalized_content for token in SUSPICIOUS_LINK_TOKENS) and URL_RE.search(content):
                await self._handle_violation(
                    message,
                    rule_name="No scams or malicious links",
                    reason="Suspicious link pattern detected.",
                    points=3,
                )
                return

            if config["invite_filter_enabled"] and INVITE_RE.search(content):
                await self._handle_violation(
                    message,
                    rule_name="No unsolicited advertising",
                    reason="Invite link detected without approval.",
                    points=1,
                )
                return

            if config["mention_filter_enabled"] and len(message.mentions) >= config["mention_threshold"]:
                await self._handle_violation(
                    message,
                    rule_name="No spam",
                    reason=f"Mass mention detected with {len(message.mentions)} mentions.",
                    points=1,
                )
                return

            if config["caps_filter_enabled"]:
                letter_count, caps_ratio = self._caps_ratio(content)
                if letter_count >= config["caps_min_length"] and caps_ratio >= config["caps_ratio"]:
                    await self._handle_violation(
                        message,
                        rule_name="No spam",
                        reason=f"Excessive caps detected at {caps_ratio:.0%}.",
                        points=1,
                    )
                    return

            if config["spam_filter_enabled"] or config["repeat_filter_enabled"]:
                triggered, rule_name = self._check_spam(
                    message.guild.id,
                    message.author.id,
                    content,
                    config=config,
                    now_ts=message.created_at.timestamp(),
                )
                if triggered:
                    await self._handle_violation(
                        message,
                        rule_name=rule_name or "No spam",
                        reason="Spam or repeated message threshold reached.",
                        points=1,
                    )
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
                    ("Blocked Words", f"{len(blocked_words)} configured" if blocked_words else "None", False),
                    ("Caps Threshold", f"{config['caps_ratio']:.0%} with minimum {config['caps_min_length']} letters", False),
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

    @automod.subcommand(description="Adjust automod thresholds")
    async def settings(
        self,
        interaction: nextcord.Interaction,
        caps_ratio_percent: int = nextcord.SlashOption(required=False, default=75, min_value=1, max_value=100),
        caps_min_length: int = nextcord.SlashOption(required=False, default=12, min_value=1, max_value=200),
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
