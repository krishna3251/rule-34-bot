"""
Core NSFW content search cog.
Sources: Danbooru (primary), Rule34.xxx (secondary), Paheal (tertiary), Gelbooru (fallback).
"""

import asyncio
import logging
import os
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────

BLOCKED_TAGS = frozenset({
    "loli", "shota", "child", "minor", "underage",
    "toddlercon", "lolicon", "shotacon",
})

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

DEFAULT_HEADERS = {
    "User-Agent": "Rule34DiscordBot/2.0 (async; +https://github.com/krishna3251/rule-34)",
    "Accept": "application/json",
}

SOURCE_CHOICES = [
    app_commands.Choice(name="Auto (Best)", value="auto"),
    app_commands.Choice(name="Rule34", value="rule34"),
    app_commands.Choice(name="Danbooru", value="danbooru"),
    app_commands.Choice(name="Paheal", value="paheal"),
]

VIDEO_EXTENSIONS = (".mp4", ".webm")
GIF_EXTENSION = ".gif"


# ══════════════════════════════════════════════════════════════════
#  Helper — tag safety check
# ══════════════════════════════════════════════════════════════════

def _is_safe(post_tags: str) -> bool:
    """Return True if post contains no blocked tags."""
    tags_lower = post_tags.lower().split()
    return not BLOCKED_TAGS.intersection(tags_lower)


def _media_type(url: str) -> str:
    """Classify a URL as 'video', 'gif', or 'image'."""
    lower = url.lower()
    if lower.endswith(VIDEO_EXTENSIONS):
        return "video"
    if lower.endswith(GIF_EXTENSION):
        return "gif"
    return "image"


# ══════════════════════════════════════════════════════════════════
#  Fetch helpers (one per source)
# ══════════════════════════════════════════════════════════════════

async def _fetch_danbooru(session: aiohttp.ClientSession, tags: str, limit: int = 50) -> list:
    """Fetch from Danbooru JSON API."""
    url = "https://danbooru.donmai.us/posts.json"
    params = {"tags": tags, "limit": limit, "random": "true"}
    try:
        async with session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning(f"Danbooru returned {resp.status}")
                return []
            data = await resp.json()
    except aiohttp.ClientError as e:
        logger.error(f"Danbooru client error: {e}")
        return []
    except asyncio.TimeoutError:
        logger.error("API timeout: Danbooru")
        return []

    results = []
    for p in data:
        file_url = p.get("file_url") or p.get("large_file_url")
        if not file_url:
            continue
        tag_str = p.get("tag_string", "")
        if not _is_safe(tag_str):
            continue
        results.append({
            "id": str(p.get("id", "")),
            "file_url": file_url,
            "sample_url": p.get("large_file_url") or file_url,
            "tags": tag_str,
            "score": p.get("score", 0),
            "source_name": "Danbooru",
            "source_url": f"https://danbooru.donmai.us/posts/{p.get('id', '')}",
            "md5": p.get("md5", ""),
        })
    return results


async def _fetch_rule34(session: aiohttp.ClientSession, tags: str, limit: int = 50) -> list:
    """Fetch from Rule34.xxx authenticated JSON API."""
    r34_user = os.getenv("R34_USER_ID", "")
    r34_key = os.getenv("R34_API_KEY", "")

    url = "https://api.rule34.xxx/index.php"
    params = {
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "limit": limit,
        "tags": tags,
    }
    if r34_user and r34_key:
        params["user_id"] = r34_user
        params["api_key"] = r34_key

    try:
        async with session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning(f"Rule34 returned {resp.status}")
                return []
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as e:
        logger.error(f"Rule34 client error: {e}")
        return []
    except asyncio.TimeoutError:
        logger.error("API timeout: Rule34")
        return []
    except Exception as e:
        logger.error(f"Rule34 JSON parse error: {e}")
        return []

    if not isinstance(data, list):
        return []

    results = []
    for p in data:
        file_url = p.get("file_url", "")
        if not file_url:
            continue
        tag_str = p.get("tags", "")
        if not _is_safe(tag_str):
            continue
        results.append({
            "id": str(p.get("id", "")),
            "file_url": file_url,
            "sample_url": p.get("sample_url") or file_url,
            "tags": tag_str,
            "score": p.get("score", 0),
            "source_name": "Rule34",
            "source_url": f"https://rule34.xxx/index.php?page=post&s=view&id={p.get('id', '')}",
            "md5": p.get("hash", ""),
        })
    return results


async def _fetch_paheal(session: aiohttp.ClientSession, tags: str, limit: int = 50) -> list:
    """Fetch from Paheal XML API."""
    url = "https://rule34.paheal.net/api/danbooru/find_posts/index.xml"
    params = {"tags": tags, "limit": limit}
    try:
        async with session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning(f"Paheal returned {resp.status}")
                return []
            text = await resp.text()
    except aiohttp.ClientError as e:
        logger.error(f"Paheal client error: {e}")
        return []
    except asyncio.TimeoutError:
        logger.error("API timeout: Paheal")
        return []

    results = []
    try:
        root = ET.fromstring(text)
        for tag_elem in root.findall(".//tag"):
            file_url = tag_elem.get("file_url", "")
            if not file_url:
                continue
            tag_str = tag_elem.get("tags", "")
            if not _is_safe(tag_str):
                continue
            results.append({
                "id": tag_elem.get("id", ""),
                "file_url": file_url,
                "sample_url": file_url,
                "tags": tag_str,
                "score": int(tag_elem.get("score", 0) or 0),
                "source_name": "Paheal",
                "source_url": f"https://rule34.paheal.net/post/view/{tag_elem.get('id', '')}",
                "md5": tag_elem.get("md5", ""),
            })
    except ET.ParseError as e:
        logger.error(f"Paheal XML parse error: {e}")

    return results


async def _fetch_gelbooru(session: aiohttp.ClientSession, tags: str, limit: int = 50) -> list:
    """Fetch from Gelbooru JSON API."""
    gel_user = os.getenv("GELBOORU_USER_ID", "")
    gel_key = os.getenv("GELBOORU_API_KEY", "")

    url = "https://gelbooru.com/index.php"
    params = {
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "limit": limit,
        "tags": tags,
    }
    if gel_user and gel_key:
        params["user_id"] = gel_user
        params["api_key"] = gel_key

    try:
        async with session.get(url, params=params, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning(f"Gelbooru returned {resp.status}")
                return []
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as e:
        logger.error(f"Gelbooru client error: {e}")
        return []
    except asyncio.TimeoutError:
        logger.error("API timeout: Gelbooru")
        return []
    except Exception as e:
        logger.error(f"Gelbooru JSON parse error: {e}")
        return []

    # Gelbooru wraps results under "post" key
    posts = data.get("post", []) if isinstance(data, dict) else data if isinstance(data, list) else []

    results = []
    for p in posts:
        file_url = p.get("file_url", "")
        if not file_url:
            continue
        tag_str = p.get("tags", "")
        if not _is_safe(tag_str):
            continue
        results.append({
            "id": str(p.get("id", "")),
            "file_url": file_url,
            "sample_url": p.get("sample_url") or file_url,
            "tags": tag_str,
            "score": p.get("score", 0),
            "source_name": "Gelbooru",
            "source_url": f"https://gelbooru.com/index.php?page=post&s=view&id={p.get('id', '')}",
            "md5": p.get("md5", ""),
        })
    return results


async def _multi_source_fetch(session: aiohttp.ClientSession, tags: str, preferred: str = "auto") -> list:
    """Try sources in priority order. For 'auto', cascade until results found."""
    fetchers = {
        "danbooru": _fetch_danbooru,
        "rule34": _fetch_rule34,
        "paheal": _fetch_paheal,
        "gelbooru": _fetch_gelbooru,
    }

    if preferred != "auto" and preferred in fetchers:
        results = await fetchers[preferred](session, tags)
        if results:
            return results
        # Fall through to cascade

    # Cascade: danbooru → rule34 → paheal → gelbooru
    for name in ("danbooru", "rule34", "paheal", "gelbooru"):
        results = await fetchers[name](session, tags)
        if results:
            return results

    return []


# ══════════════════════════════════════════════════════════════════
#  Navigation View for browsing results
# ══════════════════════════════════════════════════════════════════

class ResultNavigator(discord.ui.View):
    """Interactive navigation for search results."""

    def __init__(
        self,
        cog: "R34Cog",
        results: list,
        interaction: discord.Interaction,
        safe_preview: bool = False,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.results = results
        self.index = 0
        self.author_id = interaction.user.id
        self.guild_id = interaction.guild.id if interaction.guild else 0
        self.safe_preview = safe_preview

    def _build_embed(self) -> discord.Embed:
        post = self.results[self.index]
        url_key = "sample_url" if self.safe_preview else "file_url"
        display_url = post.get(url_key) or post["file_url"]

        tags_list = post["tags"].split()[:10]
        tags_preview = " ".join(f"`{t}`" for t in tags_list)
        if len(post["tags"].split()) > 10:
            tags_preview += " ..."

        embed = discord.Embed(
            title=f"Post #{post['id']} — {post['source_name']}",
            url=post.get("source_url", ""),
            color=0xE91E63,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Tags", value=tags_preview or "N/A", inline=False)
        embed.add_field(name="Score", value=str(post.get("score", 0)), inline=True)
        embed.add_field(name="Source", value=post["source_name"], inline=True)
        embed.set_footer(text=f"Result {self.index + 1}/{len(self.results)}")

        media = _media_type(display_url)
        if media == "video":
            embed.add_field(
                name="📹 Video",
                value=f"[Click to view]({display_url})",
                inline=False,
            )
        else:
            embed.set_image(url=display_url)

        return embed

    def _video_content(self) -> str:
        """If current post is a video, return the URL as plain text, else empty."""
        post = self.results[self.index]
        url_key = "sample_url" if self.safe_preview else "file_url"
        display_url = post.get(url_key) or post["file_url"]
        if _media_type(display_url) == "video":
            return display_url
        return ""

    async def _is_author(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran this command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_author(interaction):
            return
        self.index = (self.index - 1) % len(self.results)
        content = self._video_content()
        await interaction.response.edit_message(content=content or None, embed=self._build_embed(), view=self)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_author(interaction):
            return
        self.index = (self.index + 1) % len(self.results)
        content = self._video_content()
        await interaction.response.edit_message(content=content or None, embed=self._build_embed(), view=self)

    @discord.ui.button(emoji="⭐", style=discord.ButtonStyle.primary)
    async def fav_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_author(interaction):
            return
        post = self.results[self.index]
        saved = await self.cog.db.save_favorite(
            user_id=interaction.user.id,
            guild_id=self.guild_id,
            post_id=post["id"],
            file_url=post["file_url"],
            tags=post["tags"],
            source=post["source_name"],
        )
        msg = "⭐ Saved to favorites!" if saved else "⭐ Already in favorites."
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_author(interaction):
            return
        random.shuffle(self.results)
        self.index = 0
        content = self._video_content()
        await interaction.response.edit_message(content=content or None, embed=self._build_embed(), view=self)

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_author(interaction):
            return
        await interaction.message.delete()
        self.stop()


# ══════════════════════════════════════════════════════════════════
#  Favorites confirm-clear view
# ══════════════════════════════════════════════════════════════════

class ConfirmClearView(discord.ui.View):
    def __init__(self, cog: "R34Cog", user_id: int, guild_id: int):
        super().__init__(timeout=30)
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id

    @discord.ui.button(label="Confirm Clear", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your action.", ephemeral=True)
            return
        count = await self.cog.db.clear_favorites(self.user_id, self.guild_id)
        await interaction.response.edit_message(
            content=f"🗑️ Cleared {count} favorites.", embed=None, view=None
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()


# ══════════════════════════════════════════════════════════════════
#  Trending search button
# ══════════════════════════════════════════════════════════════════

class TrendingSearchButton(discord.ui.Button):
    def __init__(self, tag: str, cog: "R34Cog"):
        super().__init__(label=f"Search: {tag}", style=discord.ButtonStyle.primary)
        self.tag = tag
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog._do_search(interaction, self.tag, "auto", False)


class TrendingView(discord.ui.View):
    def __init__(self, tags: list, cog: "R34Cog"):
        super().__init__(timeout=120)
        for t in tags[:5]:  # max 5 buttons
            self.add_item(TrendingSearchButton(t["tag"], cog))


# ══════════════════════════════════════════════════════════════════
#  Cog
# ══════════════════════════════════════════════════════════════════

class R34Cog(commands.Cog, name="R34"):
    """NSFW content search across multiple booru APIs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return an open aiohttp session, creating one if needed."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        return self._session

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Internal search runner ────────────────────────────────────

    async def _do_search(
        self,
        interaction: discord.Interaction,
        tags: str,
        source: str = "auto",
        safe_preview: bool = False,
    ):
        """Shared search logic for slash + prefix commands."""
        if interaction.guild is None:
            await interaction.response.send_message("❌ This command only works in servers.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not channel.is_nsfw():
            await interaction.response.send_message(
                "❌ This command only works in NSFW channels.", ephemeral=True
            )
            return

        await interaction.response.defer()

        session = await self._ensure_session()
        results = await _multi_source_fetch(session, tags, preferred=source)

        if not results:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="❌ No results found for those tags.",
                    color=discord.Color.red(),
                )
            )
            return

        random.shuffle(results)

        # Log + trending
        await self.db.log_search(
            user_id=interaction.user.id,
            guild_id=interaction.guild.id,
            query=tags,
            source=results[0]["source_name"],
            result_count=len(results),
        )
        for t in tags.split():
            clean = t.strip().lower()
            if clean and clean not in ("sort:random", "rating:explicit", "rating:safe") and not clean.startswith("score:"):
                await self.db.increment_tag_trending(clean)

        view = ResultNavigator(self, results, interaction, safe_preview=safe_preview)
        embed = view._build_embed()
        content = view._video_content()
        await interaction.followup.send(content=content or None, embed=embed, view=view)

    # ══════════════════════════════════════════════════════════════
    #  Slash commands
    # ══════════════════════════════════════════════════════════════

    @app_commands.command(name="r34", description="Search NSFW content by tags.")
    @app_commands.describe(
        tags="Space-separated tags to search for",
        source="API source to use",
        safe_preview="Use lower-res sample images instead of full size",
    )
    @app_commands.choices(source=SOURCE_CHOICES)
    async def r34_slash(
        self,
        interaction: discord.Interaction,
        tags: str,
        source: app_commands.Choice[str] | None = None,
        safe_preview: bool = False,
    ):
        src = source.value if source else "auto"
        await self._do_search(interaction, tags, src, safe_preview)

    @app_commands.command(name="r34random", description="Random explicit content.")
    @app_commands.describe(min_score="Minimum score filter (default 10)")
    async def r34random_slash(
        self, interaction: discord.Interaction, min_score: int = 10
    ):
        tags = f"sort:random score:>{min_score} rating:explicit"
        await self._do_search(interaction, tags)

    @app_commands.command(name="r34tags", description="Search with tag autocomplete.")
    @app_commands.describe(tags="Tags to search (autocomplete from trending)")
    async def r34tags_slash(self, interaction: discord.Interaction, tags: str):
        await self._do_search(interaction, tags)

    @r34tags_slash.autocomplete("tags")
    async def _tags_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        trending = await self.db.get_trending_tags(limit=25)
        choices = []
        for t in trending:
            tag_name = t["tag"]
            if current.lower() in tag_name.lower():
                choices.append(app_commands.Choice(name=tag_name, value=tag_name))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="r34trending", description="Show top trending tags.")
    async def r34trending_slash(self, interaction: discord.Interaction):
        trending = await self.db.get_trending_tags(limit=10)
        if not trending:
            await interaction.response.send_message(
                embed=discord.Embed(description="No trending data yet.", color=0x2B2D31),
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="🔥 Trending Tags", color=0xE91E63, timestamp=datetime.utcnow())
        lines = []
        for i, t in enumerate(trending, 1):
            lines.append(f"**{i}.** `{t['tag']}` — {t['count']} searches")
        embed.description = "\n".join(lines)

        view = TrendingView(trending, self)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="r34favorites", description="Manage your saved favorites.")
    @app_commands.describe(action="What to do with favorites")
    @app_commands.choices(action=[
        app_commands.Choice(name="View", value="view"),
        app_commands.Choice(name="Clear All", value="clear"),
        app_commands.Choice(name="Remove One", value="remove"),
    ])
    async def r34favorites_slash(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str] | None = None,
    ):
        guild_id = interaction.guild.id if interaction.guild else 0
        user_id = interaction.user.id
        act = action.value if action else "view"

        if act == "view":
            favs = await self.db.get_favorites(user_id, guild_id, limit=10)
            if not favs:
                await interaction.response.send_message(
                    "You have no saved favorites.", ephemeral=True
                )
                return
            embed = discord.Embed(title="⭐ Your Favorites", color=0xFFD700)
            for i, f in enumerate(favs, 1):
                embed.add_field(
                    name=f"{i}. Post #{f['post_id']} ({f['source']})",
                    value=f"[View]({f['file_url']})\nTags: `{' '.join(f['tags'].split()[:5])}`",
                    inline=False,
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif act == "clear":
            view = ConfirmClearView(self, user_id, guild_id)
            await interaction.response.send_message(
                "⚠️ Are you sure you want to clear **all** your favorites?",
                view=view,
                ephemeral=True,
            )

        elif act == "remove":
            favs = await self.db.get_favorites(user_id, guild_id, limit=25)
            if not favs:
                await interaction.response.send_message("No favorites to remove.", ephemeral=True)
                return
            lines = [f"**{i}.** Post #{f['post_id']} ({f['source']})" for i, f in enumerate(favs, 1)]
            embed = discord.Embed(
                title="Remove a Favorite",
                description="\n".join(lines) + "\n\nReply with the number to remove.",
                color=0xFFD700,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

            def check(m):
                return m.author.id == user_id and m.channel.id == interaction.channel.id

            try:
                msg = await self.bot.wait_for("message", check=check, timeout=30)
                idx = int(msg.content) - 1
                if 0 <= idx < len(favs):
                    removed = await self.db.remove_favorite(user_id, guild_id, favs[idx]["post_id"])
                    resp = "✅ Removed!" if removed else "❌ Could not remove."
                else:
                    resp = "❌ Invalid number."
            except (asyncio.TimeoutError, ValueError):
                resp = "❌ Timed out or invalid input."

            try:
                await msg.reply(resp, delete_after=10)
            except Exception:
                pass

    @app_commands.command(name="r34stats", description="Your personal NSFW search stats.")
    async def r34stats_slash(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id if interaction.guild else 0
        stats = await self.db.get_user_search_stats(interaction.user.id, guild_id)

        embed = discord.Embed(title="📊 Your Stats", color=0xE91E63, timestamp=datetime.utcnow())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Total Searches", value=str(stats["total_searches"]), inline=True)
        embed.add_field(name="Favorites", value=str(stats["favorite_count"]), inline=True)
        embed.add_field(name="Most Searched", value=stats["most_searched_tag"], inline=True)
        if stats["first_search_date"]:
            ts = int(stats["first_search_date"].timestamp())
            embed.add_field(name="First Search", value=f"<t:{ts}:R>", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="r34source", description="Look up an image's source by URL.")
    @app_commands.describe(url="Direct image URL to search for")
    async def r34source_slash(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=True)
        session = await self._ensure_session()

        # Try Danbooru MD5 lookup
        # Extract potential MD5 from URL (common in booru URLs)
        md5_match = re.search(r"([a-fA-F0-9]{32})", url)
        if md5_match:
            md5 = md5_match.group(1)
            danbooru_url = f"https://danbooru.donmai.us/posts.json?tags=md5:{md5}"
            try:
                async with session.get(danbooru_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data:
                            post = data[0]
                            embed = discord.Embed(
                                title="🔍 Source Found",
                                url=f"https://danbooru.donmai.us/posts/{post['id']}",
                                color=discord.Color.green(),
                            )
                            embed.add_field(name="ID", value=str(post["id"]), inline=True)
                            embed.add_field(name="Score", value=str(post.get("score", 0)), inline=True)
                            tags_preview = " ".join(post.get("tag_string", "").split()[:10])
                            embed.add_field(name="Tags", value=f"`{tags_preview}`", inline=False)
                            embed.set_thumbnail(url=post.get("preview_file_url", ""))
                            await interaction.followup.send(embed=embed, ephemeral=True)
                            return
            except Exception as e:
                logger.warning(f"Source lookup error: {e}")

        await interaction.followup.send(
            embed=discord.Embed(
                description="🔍 Source lookup unavailable for this image.",
                color=0x2B2D31,
            ),
            ephemeral=True,
        )

    # ══════════════════════════════════════════════════════════════
    #  Prefix commands
    # ══════════════════════════════════════════════════════════════

    async def _prefix_search(self, ctx: commands.Context, tags: str):
        """Adapter: run the search logic via a fake interaction-like flow."""
        if not isinstance(ctx.channel, discord.TextChannel) or not ctx.channel.is_nsfw():
            await ctx.send("❌ This command only works in NSFW channels.")
            return

        session = await self._ensure_session()
        results = await _multi_source_fetch(session, tags)

        if not results:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ No results found.",
                    color=discord.Color.red(),
                )
            )
            return

        random.shuffle(results)

        guild_id = ctx.guild.id if ctx.guild else 0
        await self.db.log_search(
            user_id=ctx.author.id,
            guild_id=guild_id,
            query=tags,
            source=results[0]["source_name"],
            result_count=len(results),
        )
        for t in tags.split():
            clean = t.strip().lower()
            if clean and clean not in ("sort:random", "rating:explicit", "rating:safe") and not clean.startswith("score:"):
                await self.db.increment_tag_trending(clean)

        post = results[0]
        embed = discord.Embed(
            title=f"Post #{post['id']} — {post['source_name']}",
            url=post.get("source_url", ""),
            color=0xE91E63,
        )
        tags_preview = " ".join(f"`{t}`" for t in post["tags"].split()[:10])
        embed.add_field(name="Tags", value=tags_preview or "N/A", inline=False)
        embed.add_field(name="Score", value=str(post.get("score", 0)), inline=True)

        media = _media_type(post["file_url"])
        content = None
        if media == "video":
            embed.add_field(name="📹 Video", value=f"[Click to view]({post['file_url']})", inline=False)
            content = post["file_url"]
        else:
            embed.set_image(url=post["file_url"])

        await ctx.send(content=content, embed=embed)

    @commands.command(name="r34")
    async def r34_prefix(self, ctx: commands.Context, *, tags: str = "rating:explicit"):
        """Search by tags (prefix version)."""
        await self._prefix_search(ctx, tags)

    @commands.command(name="r34r")
    async def r34r_prefix(self, ctx: commands.Context):
        """Random explicit post."""
        await self._prefix_search(ctx, "sort:random score:>10 rating:explicit")

    @commands.command(name="r34g")
    async def r34g_prefix(self, ctx: commands.Context):
        """1girl solo explicit."""
        await self._prefix_search(ctx, "1girl solo rating:explicit")

    @commands.command(name="r34a")
    async def r34a_prefix(self, ctx: commands.Context):
        """Anime explicit."""
        await self._prefix_search(ctx, "anime rating:explicit")

    @commands.command(name="r34h")
    async def r34h_prefix(self, ctx: commands.Context):
        """Hentai explicit."""
        await self._prefix_search(ctx, "hentai rating:explicit")


# ══════════════════════════════════════════════════════════════════
#  Extension setup
# ══════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    await bot.add_cog(R34Cog(bot))
    logger.info("R34Cog loaded")
