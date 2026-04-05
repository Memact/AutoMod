from __future__ import annotations

import asyncio
from typing import Any

import nextcord
from nextcord.ext import commands, tasks

from bot import MemactAutoModBot
from config import BLUESKY_POLL_SECONDS, BLUESKY_RELAY_CHANNEL_ID, COMMAND_GUILD_IDS
from utils.bluesky import (
    BlueskyAPIError,
    BlueskyFeedPage,
    BlueskyPost,
    fetch_author_feed_page,
    latest_post_uri,
    normalize_handle,
    truncate_post_text,
)
from utils.checks import require_admin
from utils.time import from_iso
from utils.ui import build_embed, send_interaction


BLUESKY_HISTORY_PAGE_SIZE = 10
BLUESKY_SYNC_PAGE_SIZE = 100


class BlueskyHistorySelect(nextcord.ui.Select):
    def __init__(self, view: "BlueskyHistoryView") -> None:
        self.history_view = view
        options = [
            nextcord.SelectOption(
                label=view.format_option_label(index, post),
                description=view.format_option_description(post),
                value=str(index),
            )
            for index, post in enumerate(view.posts)
        ]
        super().__init__(
            placeholder="Choose a Bluesky post to send",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: nextcord.Interaction) -> None:
        if interaction.user.id != self.history_view.requester_id:
            await send_interaction(
                interaction,
                content="Only the moderator who opened this picker can use it.",
                ephemeral=True,
            )
            return

        post = self.history_view.posts[int(self.values[0])]
        await self.history_view.cog.post_manual_selection(
            interaction,
            self.history_view.feed_config,
            post,
        )


class BlueskyHistoryView(nextcord.ui.View):
    def __init__(
        self,
        cog: "BlueskyCog",
        *,
        requester_id: int,
        guild_id: int,
        feed_config: dict[str, Any],
        page: BlueskyFeedPage,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.feed_config = dict(feed_config)
        self.posts = page.posts
        self.current_cursor: str | None = None
        self.next_cursor = page.cursor
        self.previous_cursors: list[str | None] = []
        self._refresh_components()

    def _refresh_components(self) -> None:
        self.clear_items()
        if self.posts:
            self.add_item(BlueskyHistorySelect(self))
        self.add_item(self.PreviousButton(self))
        self.add_item(self.NextButton(self))

    def format_option_label(self, index: int, post: BlueskyPost) -> str:
        date_label = (post.created_at or "")[:10] or "Unknown date"
        text = truncate_post_text(post.text, limit=70).replace("\n", " ")
        text = text or "Bluesky post"
        return f"{index + 1}. {date_label} | {text}"

    def format_option_description(self, post: BlueskyPost) -> str:
        return f"@{post.handle} -> {post.post_url}"

    def build_embed(self, guild: nextcord.Guild) -> nextcord.Embed:
        channel = guild.get_channel(BLUESKY_RELAY_CHANNEL_ID)
        channel_label = channel.mention if isinstance(channel, nextcord.TextChannel) else f"`{BLUESKY_RELAY_CHANNEL_ID}`"
        if not self.posts:
            description = "No Bluesky posts were available on this page."
        else:
            lines = [
                f"`{index + 1}` {truncate_post_text(post.text, limit=120).replace(chr(10), ' ')}"
                for index, post in enumerate(self.posts)
            ]
            description = "\n".join(lines)
        embed = build_embed(
            "Bluesky History Picker",
            description,
            fields=[
                ("Account", f"`@{self.feed_config['handle']}`", True),
                ("Target Channel", channel_label, True),
                ("Page Size", str(len(self.posts)), True),
            ],
        )
        embed.set_footer(text="Pick a post from the dropdown to send it into the relay channel.")
        return embed

    async def _show_page(self, interaction: nextcord.Interaction, cursor: str | None, *, moving_back: bool) -> None:
        try:
            page = await fetch_author_feed_page(
                str(self.feed_config["handle"]),
                limit=BLUESKY_HISTORY_PAGE_SIZE,
                cursor=cursor,
            )
        except BlueskyAPIError as error:
            await send_interaction(interaction, content=f"Bluesky request failed: {error}", ephemeral=True)
            return
        except Exception:
            await send_interaction(
                interaction,
                content="I couldn't reach Bluesky right now. Please try again in a moment.",
                ephemeral=True,
            )
            return

        if moving_back:
            if self.previous_cursors:
                self.current_cursor = self.previous_cursors.pop()
            else:
                self.current_cursor = None
        else:
            self.previous_cursors.append(self.current_cursor)
            self.current_cursor = cursor

        self.posts = page.posts
        self.next_cursor = page.cursor
        self._refresh_components()
        await interaction.response.edit_message(embed=self.build_embed(interaction.guild), view=self)

    async def interaction_check(self, interaction: nextcord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await send_interaction(
            interaction,
            content="Only the moderator who opened this picker can use it.",
            ephemeral=True,
        )
        return False

    class PreviousButton(nextcord.ui.Button):
        def __init__(self, view: "BlueskyHistoryView") -> None:
            self.history_view = view
            super().__init__(
                label="Previous",
                style=nextcord.ButtonStyle.secondary,
                disabled=not view.previous_cursors,
            )

        async def callback(self, interaction: nextcord.Interaction) -> None:
            previous_cursor = self.history_view.previous_cursors[-1] if self.history_view.previous_cursors else None
            await self.history_view._show_page(interaction, previous_cursor, moving_back=True)

    class NextButton(nextcord.ui.Button):
        def __init__(self, view: "BlueskyHistoryView") -> None:
            self.history_view = view
            super().__init__(
                label="Next",
                style=nextcord.ButtonStyle.primary,
                disabled=not view.next_cursor,
            )

        async def callback(self, interaction: nextcord.Interaction) -> None:
            if not self.history_view.next_cursor:
                await send_interaction(interaction, content="There are no older posts on the next page.", ephemeral=True)
                return
            await self.history_view._show_page(interaction, self.history_view.next_cursor, moving_back=False)


class BlueskyCog(commands.Cog):
    def __init__(self, bot: MemactAutoModBot) -> None:
        self.bot = bot
        self._feed_locks: dict[int, asyncio.Lock] = {}

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.poll_feeds.is_running():
            self.poll_feeds.start()

    def cog_unload(self) -> None:
        if self.poll_feeds.is_running():
            self.poll_feeds.cancel()

    def _get_feed_lock(self, guild_id: int) -> asyncio.Lock:
        return self._feed_locks.setdefault(guild_id, asyncio.Lock())

    def _resolve_relay_channel(self, guild: nextcord.Guild) -> nextcord.TextChannel | None:
        channel = guild.get_channel(BLUESKY_RELAY_CHANNEL_ID)
        if isinstance(channel, nextcord.TextChannel):
            return channel
        return None

    def _format_relay_channel(self, guild: nextcord.Guild) -> str:
        channel = self._resolve_relay_channel(guild)
        if channel is not None:
            return channel.mention
        return f"`{BLUESKY_RELAY_CHANNEL_ID}`"

    def _build_post_embed(self, post: BlueskyPost, *, title: str) -> nextcord.Embed:
        embed = build_embed(
            title,
            truncate_post_text(post.text),
            fields=[("Source", post.post_url, False)],
        )
        timestamp = from_iso(post.created_at)
        if timestamp is not None:
            embed.timestamp = timestamp
        author_label = f"{post.display_name} (@{post.handle})"
        if post.avatar_url:
            embed.set_author(name=author_label, icon_url=post.avatar_url)
        else:
            embed.set_author(name=author_label)
        if post.image_url:
            embed.set_image(url=post.image_url)
        return embed

    async def _deliver_post(
        self,
        channel: nextcord.TextChannel,
        post: BlueskyPost,
        *,
        manual: bool,
    ) -> None:
        label = "Selected Bluesky post" if manual else "New Bluesky post"
        content = f"{label} from `@{post.handle}`"
        title = "Bluesky Post" if manual else "New Bluesky Post"
        await channel.send(content=content, embed=self._build_post_embed(post, title=title))

    def _has_reached_sync_point(self, post: BlueskyPost, feed_config: dict[str, Any]) -> bool:
        last_post_uri = str(feed_config.get("last_post_uri") or "").strip()
        if last_post_uri and post.uri == last_post_uri:
            return True

        last_post_created_at = str(feed_config.get("last_post_created_at") or "").strip()
        if last_post_created_at and post.created_at and post.created_at <= last_post_created_at:
            return True

        return False

    def _should_advance_cursor(self, post: BlueskyPost, feed_config: dict[str, Any]) -> bool:
        current_created_at = str(feed_config.get("last_post_created_at") or "").strip()
        if not current_created_at:
            return True
        if not post.created_at:
            return False
        return post.created_at > current_created_at

    def _save_cursor(self, guild_id: int, post: BlueskyPost) -> None:
        self.bot.db.update_bluesky_feed_cursor(
            guild_id,
            last_post_uri=post.uri,
            last_post_created_at=post.created_at,
        )

    def _refresh_cursor_from_visible_latest(self, guild_id: int, feed_config: dict[str, Any], latest_visible: BlueskyPost) -> None:
        preserved_created_at = str(feed_config.get("last_post_created_at") or "").strip() or latest_visible.created_at
        self.bot.db.update_bluesky_feed_cursor(
            guild_id,
            last_post_uri=latest_visible.uri,
            last_post_created_at=preserved_created_at,
        )

    async def _sync_feed(self, feed_config: dict[str, Any]) -> int | None:
        guild_id = int(feed_config["guild_id"])
        async with self._get_feed_lock(guild_id):
            guild = self.bot.get_guild(guild_id)
            if guild is None or not self.bot.is_allowed_guild_id(guild.id):
                return 0

            channel = self._resolve_relay_channel(guild)
            if channel is None:
                print(f"Bluesky relay channel {BLUESKY_RELAY_CHANNEL_ID} was not found in guild {guild_id}.")
                return 0

            cursor: str | None = None
            pending_posts: list[BlueskyPost] = []
            latest_visible: BlueskyPost | None = None

            while True:
                try:
                    page = await fetch_author_feed_page(
                        str(feed_config["handle"]),
                        limit=BLUESKY_SYNC_PAGE_SIZE,
                        cursor=cursor,
                    )
                except BlueskyAPIError as error:
                    print(f"Bluesky sync failed for guild {guild_id}: {error}")
                    return None
                except Exception as error:
                    print(f"Unexpected Bluesky sync failure for guild {guild_id}: {type(error).__name__}: {error}")
                    return None

                if not page.posts:
                    break

                if latest_visible is None:
                    latest_visible = page.posts[0]

                reached_sync_point = False
                for post in page.posts:
                    if self._has_reached_sync_point(post, feed_config):
                        reached_sync_point = True
                        break
                    pending_posts.append(post)

                if reached_sync_point or not page.cursor:
                    break
                cursor = page.cursor

            if not pending_posts:
                if latest_visible is not None:
                    self._refresh_cursor_from_visible_latest(guild_id, feed_config, latest_visible)
                return 0

            posted_count = 0
            for post in reversed(pending_posts):
                try:
                    await self._deliver_post(channel, post, manual=False)
                except (nextcord.Forbidden, nextcord.HTTPException) as error:
                    print(f"Failed to post Bluesky update in guild {guild_id}: {type(error).__name__}: {error}")
                    break
                self._save_cursor(guild_id, post)
                feed_config["last_post_uri"] = post.uri
                feed_config["last_post_created_at"] = post.created_at
                posted_count += 1

            if posted_count == 0 and latest_visible is not None:
                self._refresh_cursor_from_visible_latest(guild_id, feed_config, latest_visible)

            return posted_count

    async def post_manual_selection(
        self,
        interaction: nextcord.Interaction,
        feed_config: dict[str, Any],
        post: BlueskyPost,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await send_interaction(interaction, content="This command only works inside a server.", ephemeral=True)
            return

        channel = self._resolve_relay_channel(guild)
        if channel is None:
            await send_interaction(
                interaction,
                content=f"The fixed Bluesky relay channel `{BLUESKY_RELAY_CHANNEL_ID}` was not found.",
                ephemeral=True,
            )
            return

        try:
            await self._deliver_post(channel, post, manual=True)
        except (nextcord.Forbidden, nextcord.HTTPException) as error:
            await send_interaction(
                interaction,
                content=f"I couldn't post to the relay channel: {type(error).__name__}.",
                ephemeral=True,
            )
            return

        if self._should_advance_cursor(post, feed_config):
            self._save_cursor(guild.id, post)
            feed_config["last_post_uri"] = post.uri
            feed_config["last_post_created_at"] = post.created_at

        await send_interaction(
            interaction,
            embed=build_embed(
                "Bluesky Post Sent",
                f"Posted the selected Bluesky post into {channel.mention}.",
                fields=[("Source", post.post_url, False)],
            ),
        )

    @tasks.loop(seconds=BLUESKY_POLL_SECONDS)
    async def poll_feeds(self) -> None:
        for feed_config in self.bot.db.list_enabled_bluesky_feeds():
            await self._sync_feed(feed_config)

    @poll_feeds.before_loop
    async def before_poll_feeds(self) -> None:
        await self.bot.wait_until_ready()

    @nextcord.slash_command(
        description="Configure Bluesky post relays",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_guild=True),
    )
    async def bluesky(self, interaction: nextcord.Interaction) -> None:
        pass

    @bluesky.subcommand(description="Show the current Bluesky relay settings")
    async def view(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return

        feed_config = self.bot.db.get_bluesky_feed(interaction.guild.id)
        if feed_config is None:
            await send_interaction(
                interaction,
                embed=build_embed(
                    "Bluesky Relay",
                    "No Bluesky account has been configured for automatic relay yet.",
                    fields=[("Relay Channel", self._format_relay_channel(interaction.guild), False)],
                ),
            )
            return

        status = "Enabled" if feed_config["enabled"] else "Disabled"
        embed = build_embed(
            "Bluesky Relay",
            "Current Bluesky relay settings.",
            fields=[
                ("Account", f"`@{feed_config['handle']}`", True),
                ("Status", status, True),
                ("Relay Channel", self._format_relay_channel(interaction.guild), False),
                ("Last Synced Post", feed_config["last_post_uri"] or "Waiting for the first sync.", False),
                ("Last Synced Time", feed_config["last_post_created_at"] or "Not synced yet.", False),
            ],
        )
        await send_interaction(interaction, embed=embed)

    @bluesky.subcommand(description="Set which Bluesky account should auto-post into the fixed relay channel")
    async def setup(
        self,
        interaction: nextcord.Interaction,
        handle: str,
    ) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return

        relay_channel = self._resolve_relay_channel(interaction.guild)
        if relay_channel is None:
            await send_interaction(
                interaction,
                content=f"The fixed relay channel `{BLUESKY_RELAY_CHANNEL_ID}` was not found in this server.",
                ephemeral=True,
            )
            return

        normalized_handle = normalize_handle(handle)
        if not normalized_handle:
            await send_interaction(interaction, content="Please provide a valid Bluesky handle.", ephemeral=True)
            return

        try:
            page = await fetch_author_feed_page(normalized_handle, limit=1)
        except BlueskyAPIError as error:
            await send_interaction(
                interaction,
                content=f"Bluesky could not find that account: {error}",
                ephemeral=True,
            )
            return
        except Exception:
            await send_interaction(
                interaction,
                content="I couldn't reach Bluesky right now. Please try again in a moment.",
                ephemeral=True,
            )
            return

        latest_post = page.posts[0] if page.posts else None
        self.bot.db.save_bluesky_feed(
            interaction.guild.id,
            handle=normalized_handle,
            channel_id=BLUESKY_RELAY_CHANNEL_ID,
            enabled=True,
            last_post_uri=latest_post.uri if latest_post is not None else None,
            last_post_created_at=latest_post.created_at if latest_post is not None else None,
        )
        await send_interaction(
            interaction,
            embed=build_embed(
                "Bluesky Relay Updated",
                (
                    f"I'll watch `@{normalized_handle}` and auto-post future Bluesky posts in {relay_channel.mention}.\n\n"
                    "The current latest post was saved as the sync point, so older posts will not auto-backfill."
                ),
            ),
        )

    @bluesky.subcommand(description="Enable the saved Bluesky relay")
    async def enable(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return

        if not self.bot.db.set_bluesky_feed_enabled(interaction.guild.id, True):
            await send_interaction(interaction, content="Set up a Bluesky relay first with `/bluesky setup`.", ephemeral=True)
            return

        await send_interaction(
            interaction,
            embed=build_embed(
                "Bluesky Relay Enabled",
                f"The automatic relay is now active for {self._format_relay_channel(interaction.guild)}.",
            ),
        )

    @bluesky.subcommand(description="Disable the saved Bluesky relay")
    async def disable(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return

        if not self.bot.db.set_bluesky_feed_enabled(interaction.guild.id, False):
            await send_interaction(interaction, content="There is no Bluesky relay to disable yet.", ephemeral=True)
            return

        await send_interaction(
            interaction,
            embed=build_embed("Bluesky Relay Disabled", "The automatic Bluesky relay has been paused."),
        )

    @bluesky.subcommand(description="Delete the saved Bluesky relay configuration")
    async def remove(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return

        if not self.bot.db.delete_bluesky_feed(interaction.guild.id):
            await send_interaction(interaction, content="There is no Bluesky relay to remove yet.", ephemeral=True)
            return

        await send_interaction(
            interaction,
            embed=build_embed("Bluesky Relay Removed", "The saved Bluesky relay configuration has been deleted."),
        )

    @bluesky.subcommand(description="Check immediately for any Bluesky posts missed while the bot was offline")
    async def sync_now(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return

        feed_config = self.bot.db.get_bluesky_feed(interaction.guild.id)
        if feed_config is None:
            await send_interaction(interaction, content="Set up a Bluesky relay first with `/bluesky setup`.", ephemeral=True)
            return
        if not feed_config["enabled"]:
            await send_interaction(interaction, content="The Bluesky relay is currently disabled.", ephemeral=True)
            return

        posted_count = await self._sync_feed(feed_config)
        if posted_count is None:
            await send_interaction(
                interaction,
                content="I couldn't reach Bluesky right now. Please try again in a moment.",
                ephemeral=True,
            )
            return
        if posted_count == 0:
            await send_interaction(
                interaction,
                embed=build_embed("Bluesky Sync", "No new Bluesky posts were waiting to be posted."),
            )
            return

        await send_interaction(
            interaction,
            embed=build_embed(
                "Bluesky Sync",
                f"Posted `{posted_count}` missed Bluesky post(s) into {self._format_relay_channel(interaction.guild)}.",
            ),
        )

    @bluesky.subcommand(description="Open a picker so moderators can manually send older Bluesky posts")
    async def history(self, interaction: nextcord.Interaction) -> None:
        admin = await require_admin(interaction)
        if admin is None:
            return

        feed_config = self.bot.db.get_bluesky_feed(interaction.guild.id)
        if feed_config is None:
            await send_interaction(interaction, content="Set up a Bluesky relay first with `/bluesky setup`.", ephemeral=True)
            return

        relay_channel = self._resolve_relay_channel(interaction.guild)
        if relay_channel is None:
            await send_interaction(
                interaction,
                content=f"The fixed relay channel `{BLUESKY_RELAY_CHANNEL_ID}` was not found in this server.",
                ephemeral=True,
            )
            return

        try:
            page = await fetch_author_feed_page(str(feed_config["handle"]), limit=BLUESKY_HISTORY_PAGE_SIZE)
        except BlueskyAPIError as error:
            await send_interaction(interaction, content=f"Bluesky request failed: {error}", ephemeral=True)
            return
        except Exception:
            await send_interaction(
                interaction,
                content="I couldn't reach Bluesky right now. Please try again in a moment.",
                ephemeral=True,
            )
            return

        view = BlueskyHistoryView(
            self,
            requester_id=interaction.user.id,
            guild_id=interaction.guild.id,
            feed_config=feed_config,
            page=page,
        )
        await send_interaction(
            interaction,
            embed=view.build_embed(interaction.guild),
            view=view,
            ephemeral=True,
        )


def setup(bot: MemactAutoModBot) -> None:
    bot.add_cog(BlueskyCog(bot))
