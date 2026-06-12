import os
import asyncio
import aiohttp
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
IGDB_CLIENT_ID = os.getenv("IGDB_CLIENT_ID")
IGDB_CLIENT_SECRET = os.getenv("IGDB_CLIENT_SECRET")

# Games with hype_count >= this are auto-tracked
HYPE_THRESHOLD = int(os.getenv("HYPE_THRESHOLD", "50"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_PATH = "games.db"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS watched_games (
            igdb_id     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            release_ts  INTEGER,
            announced   INTEGER DEFAULT 0,
            manual      INTEGER DEFAULT 0,
            image_url   TEXT,
            source      TEXT DEFAULT 'igdb'
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS steam_games (
            steam_id    TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            release_ts  INTEGER,
            announced   INTEGER DEFAULT 0,
            image_url   TEXT
        )
    """)
    cols = [r[1] for r in con.execute("PRAGMA table_info(watched_games)").fetchall()]
    if "image_url" not in cols:
        con.execute("ALTER TABLE watched_games ADD COLUMN image_url TEXT")
    if "source" not in cols:
        con.execute("ALTER TABLE watched_games ADD COLUMN source TEXT DEFAULT 'igdb'")
    con.execute("""
        CREATE TABLE IF NOT EXISTS config (
            guild_id    TEXT PRIMARY KEY,
            channel_id  TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()


def get_channel(guild_id: int) -> int | None:
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT channel_id FROM config WHERE guild_id=?", (str(guild_id),)).fetchone()
    con.close()
    return int(row[0]) if row else None


def set_channel(guild_id: int, channel_id: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (str(guild_id), str(channel_id)))
    con.commit()
    con.close()


def upsert_game(igdb_id: int, name: str, release_ts: int | None, manual: bool = False, image_url: str | None = None, source: str = "igdb"):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO watched_games (igdb_id, name, release_ts, manual, image_url, source)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(igdb_id) DO UPDATE SET
            name=excluded.name,
            release_ts=excluded.release_ts,
            manual=MAX(manual, excluded.manual),
            image_url=COALESCE(excluded.image_url, image_url),
            source=CASE WHEN excluded.source != 'igdb' AND source = 'igdb' THEN 'both'
                        WHEN excluded.source = 'igdb' AND source != 'igdb' THEN 'both'
                        ELSE source END
    """, (igdb_id, name, release_ts, int(manual), image_url, source))
    con.commit()
    con.close()


def remove_game(igdb_id: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM watched_games WHERE igdb_id=?", (igdb_id,))
    con.commit()
    con.close()


def get_watchlist() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT igdb_id, name, release_ts, manual FROM watched_games ORDER BY release_ts").fetchall()
    con.close()
    return [{"igdb_id": r[0], "name": r[1], "release_ts": r[2], "manual": bool(r[3])} for r in rows]


def get_unannounced_launching_today() -> list[dict]:
    now = datetime.now(timezone.utc)
    start = int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp())
    end = start + 86400
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT igdb_id, name, release_ts, image_url FROM watched_games WHERE announced=0 AND release_ts>=? AND release_ts<?",
        (start, end)
    ).fetchall()
    con.close()
    return [{"igdb_id": r[0], "name": r[1], "release_ts": r[2], "image_url": r[3]} for r in rows]


def mark_announced(igdb_id: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE watched_games SET announced=1 WHERE igdb_id=?", (igdb_id,))
    con.commit()
    con.close()


def upsert_steam_game(steam_id: str, name: str, release_ts: int | None, image_url: str | None = None):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO steam_games (steam_id, name, release_ts, image_url)
        VALUES (?,?,?,?)
        ON CONFLICT(steam_id) DO UPDATE SET
            name=excluded.name,
            release_ts=excluded.release_ts,
            image_url=COALESCE(excluded.image_url, image_url)
    """, (steam_id, name, release_ts, image_url))
    con.commit()
    con.close()


def get_steam_watchlist() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT steam_id, name, release_ts FROM steam_games ORDER BY release_ts").fetchall()
    con.close()
    return [{"steam_id": r[0], "name": r[1], "release_ts": r[2]} for r in rows]


def get_steam_launching_today() -> list[dict]:
    now = datetime.now(timezone.utc)
    start = int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp())
    end = start + 86400
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT steam_id, name, release_ts, image_url FROM steam_games WHERE announced=0 AND release_ts>=? AND release_ts<?",
        (start, end)
    ).fetchall()
    con.close()
    return [{"steam_id": r[0], "name": r[1], "release_ts": r[2], "image_url": r[3]} for r in rows]


def mark_steam_announced(steam_id: str):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE steam_games SET announced=1 WHERE steam_id=?", (steam_id,))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# IGDB helpers
# ---------------------------------------------------------------------------

_igdb_token: str | None = None
_igdb_token_expiry: float = 0.0


async def igdb_token(session: aiohttp.ClientSession) -> str:
    global _igdb_token, _igdb_token_expiry
    if _igdb_token and datetime.now().timestamp() < _igdb_token_expiry - 60:
        return _igdb_token
    async with session.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": IGDB_CLIENT_ID,
            "client_secret": IGDB_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }
    ) as resp:
        data = await resp.json()
    _igdb_token = data["access_token"]
    _igdb_token_expiry = datetime.now().timestamp() + data["expires_in"]
    return _igdb_token


async def igdb_query(session: aiohttp.ClientSession, endpoint: str, body: str) -> list[dict]:
    token = await igdb_token(session)
    headers = {
        "Client-ID": IGDB_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }
    async with session.post(f"https://api.igdb.com/v4/{endpoint}", headers=headers, data=body) as resp:
        return await resp.json()


async def fetch_cover_url(session: aiohttp.ClientSession, game_id: int) -> str | None:
    """Fetch the artwork/screenshot URL for a game (wide banner preferred, cover fallback)."""
    # Try artworks first (wide banners)
    artworks = await igdb_query(session, "artworks", f"fields image_id; where game={game_id}; limit 1;")
    if artworks and "image_id" in artworks[0]:
        return f"https://images.igdb.com/igdb/image/upload/t_screenshot_big/{artworks[0]['image_id']}.jpg"
    # Fall back to cover art
    covers = await igdb_query(session, "covers", f"fields image_id; where game={game_id}; limit 1;")
    if covers and "image_id" in covers[0]:
        return f"https://images.igdb.com/igdb/image/upload/t_cover_big/{covers[0]['image_id']}.jpg"
    return None


async def search_game(session: aiohttp.ClientSession, name: str) -> list[dict]:
    """Return top matches for a game name."""
    body = f'search "{name}"; fields id,name,hypes,first_release_date; limit 5;'
    return await igdb_query(session, "games", body)


async def fetch_high_profile_upcoming(session: aiohttp.ClientSession) -> list[dict]:
    """Games releasing within the next 90 days with hype >= HYPE_THRESHOLD."""
    now = int(datetime.now(timezone.utc).timestamp())
    future = now + 90 * 86400
    body = (
        f"fields id,name,hypes,first_release_date; "
        f"where hypes >= {HYPE_THRESHOLD} & first_release_date >= {now} & first_release_date <= {future}; "
        f"sort hypes desc; limit 50;"
    )
    return await igdb_query(session, "games", body)


async def fetch_steam_wishlist_games(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch Steam's most wishlisted upcoming games via the search API."""
    from bs4 import BeautifulSoup
    from dateutil import parser as dateparser

    url = (
        "https://store.steampowered.com/search/results/"
        "?filter=popularwishlist&cc=us&l=en&count=20"
        "&category1=998&upcoming=1&infinite=1&json=1"
    )
    headers = {"Accept-Language": "en-US,en;q=0.9"}
    try:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json(content_type=None)
        html = data.get("results_html", "")
    except Exception as e:
        log.error("Failed to fetch Steam wishlist: %s", e)
        return []

    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("a.search_result_row")
    games = []

    for item in items[:20]:
        app_id = item.get("data-ds-appid")
        name_tag = item.select_one(".title")
        if not app_id or not name_tag:
            continue
        name = name_tag.get_text(strip=True)

        # Header image from the capsule
        img_tag = item.select_one("img")
        image_url = img_tag["src"] if img_tag else None
        # Use the larger header image instead of capsule thumbnail
        if app_id:
            image_url = f"https://cdn.akamai.steamstatic.com/steam/apps/{app_id}/header.jpg"

        # Release date — skip vague dates like "2026" or "Q1 2026" (no specific day)
        release_ts = None
        date_tag = item.select_one(".search_released")
        if date_tag:
            date_text = date_tag.get_text(strip=True)
            import re as _re
            is_vague = bool(_re.fullmatch(r'Q?\d{1,2}\s*\d{4}|\d{4}', date_text.strip()))
            if not is_vague:
                try:
                    dt = dateparser.parse(date_text)
                    if dt:
                        release_ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
                except Exception:
                    pass

        games.append({"steam_id": app_id, "name": name, "release_ts": release_ts, "image_url": image_url})

    log.info("Fetched %d games from Steam wishlist", len(games))
    return games


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    await tree.sync()
    log.info("Slash commands synced")
    daily_check.start()
    weekly_watchlist.start()


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@tree.command(name="help", description="List all GameAnnouncer commands")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="GameAnnouncer Commands", color=discord.Color.blurple())
    embed.add_field(name="/watch", value="Add a game to the watch list by name.", inline=False)
    embed.add_field(name="/unwatch", value="Remove a game from the watch list.", inline=False)
    embed.add_field(name="/watchlist", value="Show all watched games with their release dates.", inline=False)
    embed.add_field(name="/setchannel", value="Set the channel where announcements are posted. *(Requires Manage Channels)*", inline=False)
    embed.add_field(name="/syncgames", value="Manually pull the latest high-profile games from IGDB + Steam. *(Requires Manage Server)*", inline=False)
    embed.add_field(name="/testannounce", value="Preview what a launch announcement looks like. *(Requires Manage Server)*", inline=False)
    embed.set_footer(text="🔥 = IGDB high-profile  |  📌 = manually added  |  🎮 = Steam wishlisted")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="setchannel", description="Set the channel where game announcements are posted")
@app_commands.describe(channel="Channel to post in (defaults to current channel)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_setchannel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    target = channel or interaction.channel
    set_channel(interaction.guild.id, target.id)
    await interaction.response.send_message(f"Announcements will be posted in {target.mention}.", ephemeral=True)


@tree.command(name="watch", description="Add a game to the watch list")
@app_commands.describe(game="Name of the game to watch")
async def slash_watch(interaction: discord.Interaction, game: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        results = await search_game(session, game)
        if not results:
            await interaction.followup.send(f"No games found matching **{game}**.")
            return
        result = results[0]
        image_url = await fetch_cover_url(session, result["id"])

    release_ts = result.get("first_release_date")
    upsert_game(result["id"], result["name"], release_ts, manual=True, image_url=image_url)

    if release_ts:
        release_dt = datetime.fromtimestamp(release_ts, tz=timezone.utc)
        await interaction.followup.send(
            f"Now watching **{result['name']}** — releases {discord.utils.format_dt(release_dt, 'D')}."
        )
    else:
        await interaction.followup.send(f"Now watching **{result['name']}** (no release date yet).")


@tree.command(name="unwatch", description="Remove a game from the watch list")
@app_commands.describe(game="Name of the game to remove")
async def slash_unwatch(interaction: discord.Interaction, game: str):
    watchlist = get_watchlist()
    name_lower = game.lower()
    matches = [g for g in watchlist if name_lower in g["name"].lower()]

    if not matches:
        await interaction.response.send_message(f"No watched game matches **{game}**.", ephemeral=True)
        return

    for g in matches:
        remove_game(g["igdb_id"])

    names = ", ".join(f"**{g['name']}**" for g in matches)
    await interaction.response.send_message(f"Removed {names} from the watch list.", ephemeral=True)


@tree.command(name="watchlist", description="Show all currently watched games")
async def slash_watchlist(interaction: discord.Interaction):
    igdb_games = get_watchlist()
    steam_games = get_steam_watchlist()
    if not igdb_games and not steam_games:
        await interaction.response.send_message("No games on the watch list yet.", ephemeral=True)
        return
    await interaction.response.defer()
    await _post_watchlist(interaction.channel)


@tree.command(name="testannounce", description="Preview what a launch announcement looks like")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_testannounce(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        image_url = await fetch_cover_url(session, 282831)
    embed = discord.Embed(
        title="🎮 Game Launch Today!",
        description="**The Blood of Dawnwalker** is out today!",
        color=discord.Color.green(),
    )
    embed.add_field(name="Release Date", value=discord.utils.format_dt(datetime.now(timezone.utc), "D"))
    if image_url:
        embed.set_image(url=image_url)
    await interaction.followup.send(embed=embed)


@tree.command(name="syncgames", description="Manually sync high-profile upcoming games from IGDB and Steam")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_syncgames(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    count = await _sync_high_profile()
    await interaction.followup.send(f"Done. {count} game(s) now tracked.")


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You don't have permission to use that command.", ephemeral=True)
    else:
        log.error("Slash command error: %s", error)
        if not interaction.response.is_done():
            await interaction.response.send_message("An error occurred. Check the bot logs.", ephemeral=True)


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------

@tasks.loop(hours=24)
async def daily_check():
    log.info("Running daily game check")
    await _sync_high_profile()
    await _announce_launches()


@daily_check.before_loop
async def before_daily_check():
    await bot.wait_until_ready()
    # Align to midnight UTC
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    wait_seconds = (midnight - now).total_seconds()
    log.info("Daily check starts in %.0f seconds", wait_seconds)
    await asyncio.sleep(wait_seconds)


@tasks.loop(hours=168)  # weekly
async def weekly_watchlist():
    log.info("Posting weekly watchlist")
    con = sqlite3.connect(DB_PATH)
    channels = con.execute("SELECT channel_id FROM config").fetchall()
    con.close()
    for (channel_id,) in channels:
        channel = bot.get_channel(int(channel_id))
        if channel:
            await _post_watchlist(channel)


@weekly_watchlist.before_loop
async def before_weekly_watchlist():
    await bot.wait_until_ready()
    # Wait until next Friday at 6pm EST (UTC-5, so 23:00 UTC)
    now = datetime.now(timezone.utc)
    days_until_friday = (4 - now.weekday()) % 7  # Friday = weekday 4
    next_friday = (now + timedelta(days=days_until_friday)).replace(hour=23, minute=0, second=0, microsecond=0)
    if next_friday <= now:
        next_friday += timedelta(weeks=1)
    wait_seconds = (next_friday - now).total_seconds()
    log.info("Weekly watchlist posts in %.0f seconds", wait_seconds)
    await asyncio.sleep(wait_seconds)


async def _post_watchlist(channel: discord.TextChannel):
    igdb_games = get_watchlist()
    steam_games = get_steam_watchlist()

    now_ts = datetime.now(timezone.utc).timestamp()

    def date_str(release_ts):
        if not release_ts:
            return "TBA"
        dt = datetime.fromtimestamp(release_ts, tz=timezone.utc)
        prefix = "released" if release_ts < now_ts else "releases"
        return f"{prefix} {discord.utils.format_dt(dt, 'D')}"

    igdb_names = {g["name"].lower() for g in igdb_games}
    all_games = []
    for g in igdb_games:
        all_games.append({"name": g["name"], "release_ts": g["release_ts"], "tag": "📌" if g["manual"] else "🔥"})
    for g in steam_games:
        if g["name"].lower() not in igdb_names:
            all_games.append({"name": g["name"], "release_ts": g["release_ts"], "tag": "🎮"})

    all_games.sort(key=lambda g: g["release_ts"] if g["release_ts"] else float("inf"))

    if not all_games:
        return

    lines = [f"{g['tag']} **{g['name']}** — {date_str(g['release_ts'])}" for g in all_games]
    embed = discord.Embed(
        title="Game Watch List",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="🔥 = IGDB high-profile  |  📌 = manually added  |  🎮 = Steam wishlisted")
    await channel.send(embed=embed)


async def _sync_high_profile() -> int:
    total = 0
    async with aiohttp.ClientSession() as session:
        # IGDB
        games = await fetch_high_profile_upcoming(session)
        if isinstance(games, list):
            valid = [g for g in games if "id" in g and "name" in g]
            for g in valid:
                image_url = await fetch_cover_url(session, g["id"])
                upsert_game(g["id"], g["name"], g.get("first_release_date"), manual=False, image_url=image_url, source="igdb")
            log.info("Synced %d IGDB games", len(valid))
            total += len(valid)
        else:
            log.error("Unexpected IGDB response: %s", games)

        # Steam
        steam_games = await fetch_steam_wishlist_games(session)
        for g in steam_games:
            upsert_steam_game(g["steam_id"], g["name"], g.get("release_ts"), g.get("image_url"))
        total += len(steam_games)

    return total


async def _announce_launches():
    igdb_launching = get_unannounced_launching_today()
    igdb_names = {g["name"].lower() for g in igdb_launching}
    steam_launching = [g for g in get_steam_launching_today() if g["name"].lower() not in igdb_names]
    launching = igdb_launching + steam_launching
    if not launching:
        return

    con = sqlite3.connect(DB_PATH)
    channels = con.execute("SELECT channel_id FROM config").fetchall()
    con.close()

    for game in launching:
        embed = discord.Embed(
            title="🎮 Game Launch Today!",
            description=f"**{game['name']}** is out today!",
            color=discord.Color.green(),
        )
        if game["release_ts"]:
            dt = datetime.fromtimestamp(game["release_ts"], tz=timezone.utc)
            embed.add_field(name="Release Date", value=discord.utils.format_dt(dt, "D"))
        if game.get("image_url"):
            embed.set_image(url=game["image_url"])

        for (channel_id,) in channels:
            channel = bot.get_channel(int(channel_id))
            if channel:
                await channel.send(embed=embed)

        if "igdb_id" in game:
            mark_announced(game["igdb_id"])
        else:
            mark_steam_announced(game["steam_id"])
        log.info("Announced launch of %s", game["name"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    bot.run(DISCORD_TOKEN)
