from __future__ import annotations

from typing import Optional

import nextcord
from nextcord.ext import commands

from bot import MemactAutoModBot
from config import COMMAND_GUILD_IDS
from utils.checks import require_moderator
from utils.ui import build_embed, send_interaction


class EmbedToolsCog(commands.Cog):
    def __init__(self, bot: MemactAutoModBot) -> None:
        self.bot = bot

    def _make_embed(
        self,
        title: str,
        description: str,
        *,
        image_url: str | None = None,
        thumbnail_url: str | None = None,
    ) -> nextcord.Embed:
        embed = build_embed(title, description)
        if image_url:
            embed.set_image(url=image_url)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        return embed

    @nextcord.slash_command(
        description="Create and send embeds",
        guild_ids=COMMAND_GUILD_IDS,
        default_member_permissions=nextcord.Permissions(manage_messages=True),
    )
    async def embed(self, interaction: nextcord.Interaction) -> None:
        pass

    @embed.subcommand(description="Send a one-off embed")
    async def send(
        self,
        interaction: nextcord.Interaction,
        channel: nextcord.TextChannel,
        title: str,
        description: str,
        image_url: Optional[str] = nextcord.SlashOption(required=False),
        thumbnail_url: Optional[str] = nextcord.SlashOption(required=False),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        embed = self._make_embed(title, description, image_url=image_url, thumbnail_url=thumbnail_url)
        await channel.send(embed=embed)
        await send_interaction(interaction, embed=build_embed("Embed Sent", f"Posted the embed in {channel.mention}."))

    @embed.subcommand(description="Save an embed template")
    async def save(
        self,
        interaction: nextcord.Interaction,
        name: str,
        title: str,
        description: str,
        image_url: Optional[str] = nextcord.SlashOption(required=False),
        thumbnail_url: Optional[str] = nextcord.SlashOption(required=False),
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        self.bot.db.save_embed_template(
            interaction.guild.id,
            name,
            title,
            description,
            image_url=image_url,
            thumbnail_url=thumbnail_url,
        )
        await send_interaction(interaction, embed=build_embed("Template Saved", f"Saved embed template `{name.lower()}`."))

    @embed.subcommand(description="Send a saved embed template")
    async def send_saved(
        self,
        interaction: nextcord.Interaction,
        name: str,
        channel: nextcord.TextChannel,
    ) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        template = self.bot.db.get_embed_template(interaction.guild.id, name)
        if template is None:
            await send_interaction(interaction, content="That embed template was not found.", ephemeral=True)
            return
        embed = self._make_embed(
            template["title"],
            template["description"],
            image_url=template["image_url"],
            thumbnail_url=template["thumbnail_url"],
        )
        for field in template["fields_json"]:
            embed.add_field(name=field["name"], value=field["value"], inline=field.get("inline", False))
        await channel.send(embed=embed)
        await send_interaction(interaction, embed=build_embed("Template Sent", f"Posted `{template['name']}` in {channel.mention}."))

    @embed.subcommand(description="List saved embed templates")
    async def list(self, interaction: nextcord.Interaction) -> None:
        moderator = await require_moderator(interaction)
        if moderator is None:
            return
        templates = self.bot.db.list_embed_templates(interaction.guild.id)
        await send_interaction(interaction, embed=build_embed("Saved Embed Templates", "\n".join(templates) if templates else "No embed templates saved yet."))


def setup(bot: MemactAutoModBot) -> None:
    bot.add_cog(EmbedToolsCog(bot))
