"""
itch.io NSFW games discovery cog.
Uses itch.io public browse/search endpoints (no API key required).
"""

import logging
import os
import random
import re
from datetime import datetime
from typing import List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

BASE_HEADERS = {
    "User-Agent": "Rule34DiscordBot/2.0 (async; +https://github.com/krishna3251/rule-34)",
    "Accept": "application/json, text/html",
}


# ── Parsing helpers ───────────────────────────────────────────────

def _parse_games_from_json(data: dict | list) -> list:
    """Extract game dicts from itch.io JSON responses.
    itch.io browse endpoints can return either a simple list or an
    object with an inner 'games' key, depending on the endpoint.
    """
    games_raw: list = []
    if isinstance(data, list):
        games_raw = data
    elif isinstance(data, dict):
        # Try common keys
        if "games" in data:
            games_raw = data["games"]
        elif "results" in data:
            games_raw = data["results"]
        else:
            # Maybe top-level dict is one game
            if "title" in data:
                games_raw = [data]

    results = []
    for g in games_raw:
        if not isinstance(g, dict):
            continue
        results.append({
            "title": g.get("title", "Untitled"),
            "url": g.get("url", ""),
            "short_text": (g.get("short_text") or "")[:200],
            "price": g.get("min_price") or g.get("price") or "Free",
            "cover": g.get("cover_url") or g.get("cover") or "",
            "classification": g.get("classification", "game"),
            "created_at": g.get("created_at", ""),
            "published_at": g.get("published_at", ""),
        })
    return results


def _parse_games_from_html(html: str) -> list:
    """Fallback: extract game data from itch.io HTML browse pages
    using regex (no BeautifulSoup dependency).
    """
    results = []
    # itch.io embeds game JSON inside <script> tags or data attributes
    # Try data-game_id patterns in game cells
    cells = re.findall(
        r'<div[^>]*class="game_cell"[^>]*data-game_id="(\d+)"[^>]*>.*?</div>\s*</div>',
        html,
        re.DOTALL,
    )
    for cell_html in cells:
        title_m = re.search(r'<a[^>]*class="title[^"]*"[^>]*>([^<]+)</a>', cell_html)
        link_m = re.search(r'<a[^>]*class="title[^"]*"[^>]*href="([^"]+)"', cell_html)
        img_m = re.search(r'<img[^>]*(?:data-lazy_src|src)="([^"]+)"', cell_html)
        price_m = re.search(r'class="price_value"[^>]*>\$?([\d.]+)', cell_html)
        desc_m = re.search(r'<div class="game_text">([^<]+)</div>', cell_html)

        results.append({
            "title": title_m.group(1).strip() if title_m else "Untitled",
            "url": link_m.group(1) if link_m else "",
            "short_text": (desc_m.group(1).strip() if desc_m else "")[:200],
            "price": f"${price_m.group(1)}" if price_m else "Free",
            "cover": img_m.group(1) if img_m else "",
            "classification": "game",
            "created_at": "",
            "published_at": "",
        })
    return results


# ── Fetch functions ───────────────────────────────────────────────

async def _fetch_itch_json(
    session: aiohttp.ClientSession, url: str, params: dict | None = None
) -> list:
    """Fetch an itch.io JSON endpoint and parse games."""
    headers = dict(BASE_HEADERS)
    api_key = os.getenv("ITCH_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning(f"itch.io returned {resp.status} for {url}")
                return []

            content_type = resp.headers.get("Content-Type", "")
            if "json" in content_type:
                data = await resp.json(content_type=None)
                return _parse_games_from_json(data)
            else:
                # Might be HTML — try HTML parse
                html = await resp.text()
                # Check if there's embedded JSON
                json_match = re.search(r'<script[^>]*>.*?GameGrid\.games\s*=\s*(\[.*?\]);', html, re.DOTALL)
                if json_match:
                    import json
                    try:
                        data = json.loads(json_match.group(1))
                        return _parse_games_from_json(data)
                    except Exception:
                        pass
                return _parse_games_from_html(html)

    except aiohttp.ClientError as e:
        logger.error(f"itch.io client error: {e}")
        return []
    except Exception as e:
        logger.error(f"itch.io fetch error: {e}")
        return []


async def _search_itch(session: aiohttp.ClientSession, query: str, limit: int = 10) -> list:
    """Search itch.io for adult games."""
    # Primary: search API with JSON format
    url = "https://itch.io/search"
    params = {"q": f"{query} nsfw", "t": "games", "format": "json"}
    results = await _fetch_itch_json(session, url, params)
    if results:
        return results[:limit]

    # Fallback: tag-based browse
    url_fb = f"https://itch.io/games/tag-adult/tag-18plus?format=json&q={query}"
    results = await _fetch_itch_json(session, url_fb)
    return results[:limit]


async def _trending_itch(session: aiohttp.ClientSession, limit: int = 10) -> list:
    """Get trending adult games from itch.io."""
    url = "https://itch.io/games/tag-adult/tag-18plus/top-rated?format=json"
    results = await _fetch_itch_json(session, url)
    if not results:
        url = "https://itch.io/games/tag-nsfw?format=json"
        results = await _fetch_itch_json(session, url)
    return results[:limit]


async def _new_itch(session: aiohttp.ClientSession, limit: int = 10) -> list:
    """Get newest adult games from itch.io."""
    url = "https://itch.io/games/tag-adult/tag-18plus/newest?format=json"
    results = await _fetch_itch_json(session, url)
    if not results:
        url = "https://itch.io/games/tag-nsfw/newest?format=json"
        results = await _fetch_itch_json(session, url)
    return results[:limit]


async def _random_itch(session: aiohttp.ClientSession) -> list:
    """Get a random adult game by fetching a random page."""
    page = random.randint(1, 20)
    url = f"https://itch.io/games/tag-adult/tag-18plus?format=json&page={page}"
    results = await _fetch_itch_json(session, url)
    if results:
        return [random.choice(results)]

    url = f"https://itch.io/games/tag-nsfw?format=json&page={page}"
    results = await _fetch_itch_json(session, url)
    if results:
        return [random.choice(results)]
    return []


# ── Embed builder ─────────────────────────────────────────────────

def _game_embed(game: dict, index: int = 0, total: int = 1) -> discord.Embed:
    """Build a rich embed for one itch.io game."""
    embed = discord.Embed(
        title=game["title"],
        url=game["url"] if game["url"] else None,
        description=game["short_text"] or "No description available.",
        color=0xFA5C5C,  # itch.io brand red
        timestamp=datetime.utcnow(),
    )
    price_display = game["price"]
    if isinstance(price_display, (int, float)):
        price_display = f"${price_display / 100:.2f}" if price_display > 0 else "Free"
    embed.add_field(name="💰 Price", value=str(price_display), inline=True)
    embed.add_field(name="🏷️ Type", value=game.get("classification", "game").title(), inline=True)

    if game.get("cover"):
        embed.set_thumbnail(url=game["cover"])

    if total > 1:
        embed.set_footer(text=f"🔞 Adult game content • {index + 1}/{total}")
    else:
        embed.set_footer(text="🔞 Adult game content")

    return embed


# ── Paginator View ────────────────────────────────────────────────

class ItchPaginator(discord.ui.View):
    """Paginated game results."""

    def __init__(self, games: list, author_id: int):
        super().__init__(timeout=120)
        self.games = games
        self.index = 0
        self.author_id = author_id
        self._update_buttons()
        # Add link button manually (decorators don't support url= )
        self._update_link_button()

    def _update_buttons(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.games) - 1

    def _update_link_button(self):
        """Add/replace the itch.io link button for the current game."""
        # Remove old link button if present
        for child in self.children[:]:
            if isinstance(child, discord.ui.Button) and child.style == discord.ButtonStyle.link:
                self.remove_item(child)
        url = self.games[self.index].get("url") or "https://itch.io"
        self.add_item(discord.ui.Button(label="🎮 Open on itch.io", style=discord.ButtonStyle.link, url=url))

    def _current_embed(self) -> discord.Embed:
        return _game_embed(self.games[self.index], self.index, len(self.games))

    async def _check_author(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not your session.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary, custom_id="itch_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_author(interaction):
            return
        self.index = max(0, self.index - 1)
        self._update_buttons()
        self._update_link_button()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary, custom_id="itch_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_author(interaction):
            return
        self.index = min(len(self.games) - 1, self.index + 1)
        self._update_buttons()
        self._update_link_button()
        await interaction.response.edit_message(embed=self._current_embed(), view=self)


# ══════════════════════════════════════════════════════════════════
#  Cog
# ══════════════════════════════════════════════════════════════════

class ItchCog(commands.Cog, name="Itch"):
    """Browse NSFW / adult games from itch.io."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        return self._session

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Helper to send paginated results ──────────────────────────

    async def _send_results(
        self, interaction: discord.Interaction, games: list, title: str
    ):
        if not games:
            embed = discord.Embed(
                description=f"❌ No {title.lower()} found.", color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        if len(games) == 1:
            embed = _game_embed(games[0])
            view = discord.ui.View()
            if games[0].get("url"):
                view.add_item(
                    discord.ui.Button(
                        label="🎮 Play on itch.io",
                        style=discord.ButtonStyle.link,
                        url=games[0]["url"],
                    )
                )
            await interaction.followup.send(embed=embed, view=view)
        else:
            # For paginator, we can't use a static link button with dynamic URL easily,
            # so we embed it in the embed itself.
            view = ItchPaginator(games, interaction.user.id)
            await interaction.followup.send(embed=view._current_embed(), view=view)

    # ── Slash commands (grouped under /itch) ──────────────────────

    itch_group = app_commands.Group(name="itch", description="Browse adult games on itch.io")

    @itch_group.command(name="search", description="Search itch.io for adult / NSFW games.")
    @app_commands.describe(query="Game search query", limit="Number of results (1-10)")
    async def itch_search(
        self, interaction: discord.Interaction, query: str, limit: int = 5
    ):
        limit = max(1, min(10, limit))
        await interaction.response.defer()
        session = await self._ensure_session()
        games = await _search_itch(session, query, limit)
        await self._send_results(interaction, games, "results")

    @itch_group.command(name="trending", description="Trending adult games on itch.io.")
    @app_commands.describe(limit="Number of results (1-10)")
    async def itch_trending(self, interaction: discord.Interaction, limit: int = 5):
        limit = max(1, min(10, limit))
        await interaction.response.defer()
        session = await self._ensure_session()
        games = await _trending_itch(session, limit)
        await self._send_results(interaction, games, "trending games")

    @itch_group.command(name="random", description="Random adult game from itch.io.")
    async def itch_random(self, interaction: discord.Interaction):
        await interaction.response.defer()
        session = await self._ensure_session()
        games = await _random_itch(session)
        await self._send_results(interaction, games, "random game")

    @itch_group.command(name="new", description="Newest adult game releases on itch.io.")
    @app_commands.describe(limit="Number of results (1-10)")
    async def itch_new(self, interaction: discord.Interaction, limit: int = 5):
        limit = max(1, min(10, limit))
        await interaction.response.defer()
        session = await self._ensure_session()
        games = await _new_itch(session, limit)
        await self._send_results(interaction, games, "new releases")


# ══════════════════════════════════════════════════════════════════
#  Extension setup
# ══════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    await bot.add_cog(ItchCog(bot))
    logger.info("ItchCog loaded")
