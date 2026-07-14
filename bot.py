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

DB_PATH = os.getenv("DB_PATH", "games.db")


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
    if "steam_app_id" not in cols:
        con.execute("ALTER TABLE watched_games ADD COLUMN steam_app_id TEXT")
    if "platforms" not in cols:
        con.execute("ALTER TABLE watched_games ADD COLUMN platforms TEXT")
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


def upsert_game(igdb_id: int, name: str, release_ts: int | None, manual: bool = False, image_url: str | None = None, source: str = "igdb", steam_app_id: str | None = None, platforms: str | None = None):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO watched_games (igdb_id, name, release_ts, manual, image_url, source, steam_app_id, platforms)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(igdb_id) DO UPDATE SET
            name=excluded.name,
            release_ts=excluded.release_ts,
            manual=MAX(manual, excluded.manual),
            image_url=COALESCE(excluded.image_url, image_url),
            source=CASE WHEN excluded.source != 'igdb' AND source = 'igdb' THEN 'both'
                        WHEN excluded.source = 'igdb' AND source != 'igdb' THEN 'both'
                        ELSE source END,
            steam_app_id=COALESCE(excluded.steam_app_id, steam_app_id),
            platforms=COALESCE(excluded.platforms, platforms)
    """, (igdb_id, name, release_ts, int(manual), image_url, source, steam_app_id, platforms))
    con.commit()
    con.close()


def remove_game(igdb_id: int):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM watched_games WHERE igdb_id=?", (igdb_id,))
    con.commit()
    con.close()


def get_watchlist() -> list[dict]:
    now = int(datetime.now(timezone.utc).timestamp())
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT igdb_id, name, release_ts, manual, steam_app_id, platforms FROM watched_games "
        "WHERE announced=0 AND (release_ts IS NULL OR release_ts > ?) ORDER BY release_ts",
        (now,)
    ).fetchall()
    con.close()
    return [{"igdb_id": r[0], "name": r[1], "release_ts": r[2], "manual": bool(r[3]), "steam_app_id": r[4], "platforms": r[5]} for r in rows]


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


def get_overdue_unannounced() -> list[dict]:
    """Games that have already released but were never announced."""
    now = int(datetime.now(timezone.utc).timestamp())
    con = sqlite3.connect(DB_PATH)
    igdb_rows = con.execute(
        "SELECT igdb_id, name, release_ts, image_url FROM watched_games WHERE announced=0 AND release_ts<?",
        (now,)
    ).fetchall()
    steam_rows = con.execute(
        "SELECT steam_id, name, release_ts, image_url FROM steam_games WHERE announced=0 AND release_ts<?",
        (now,)
    ).fetchall()
    con.close()
    result = [{"igdb_id": r[0], "name": r[1], "release_ts": r[2], "image_url": r[3]} for r in igdb_rows]
    result += [{"steam_id": r[0], "name": r[1], "release_ts": r[2], "image_url": r[3]} for r in steam_rows]
    return result


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
    now = int(datetime.now(timezone.utc).timestamp())
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT steam_id, name, release_ts FROM steam_games "
        "WHERE announced=0 AND (release_ts IS NULL OR release_ts > ?) ORDER BY release_ts",
        (now,)
    ).fetchall()
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


PLATFORM_LABELS = {
    6: None,      # PC — no badge needed, Steam link covers it
    14: None,     # Mac
    48: "PS4",
    167: "PS5",
    49: "XB1",
    169: "XSX",
    130: "NSW",
}


async def fetch_store_info_batch(session: aiohttp.ClientSession, igdb_ids: list[int]) -> dict[int, tuple[str | None, str | None]]:
    """Batch fetch (steam_app_id, platforms_str) for multiple games in 2 API calls."""
    if not igdb_ids:
        return {}
    id_list = ",".join(str(i) for i in igdb_ids)

    # Single call for all Steam app IDs
    ext_rows = await igdb_query(session, "external_games", f"fields game,uid; where game=({id_list}) & category=1; limit 500;")
    steam_map = {r["game"]: str(r["uid"]) for r in ext_rows if "game" in r and "uid" in r}

    # Single call for all platform lists (already have them from the games query but re-fetch to be safe)
    plat_rows = await igdb_query(session, "games", f"fields id,platforms; where id=({id_list}); limit 500;")
    plat_map = {r["id"]: r.get("platforms", []) for r in plat_rows if "id" in r}

    result = {}
    for igdb_id in igdb_ids:
        steam_app_id = steam_map.get(igdb_id)
        platform_ids = plat_map.get(igdb_id, [])
        badges = [PLATFORM_LABELS[p] for p in platform_ids if p in PLATFORM_LABELS and PLATFORM_LABELS[p]]
        platforms_str = ",".join(sorted(set(badges))) if badges else None
        result[igdb_id] = (steam_app_id, platforms_str)
    return result


async def fetch_game_store_info(session: aiohttp.ClientSession, igdb_id: int) -> tuple[str | None, str | None]:
    """Return (steam_app_id, platforms_str) for a single game."""
    results = await fetch_store_info_batch(session, [igdb_id])
    return results.get(igdb_id, (None, None))


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
    asyncio.create_task(_startup_check())


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@tree.command(name="help", description="List all GameAnnouncer commands")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="GameAnnouncer Commands", color=discord.Color.blurple())
    embed.add_field(name="/watch", value="Add a game to the watch list by name.", inline=False)
    embed.add_field(name="/unwatch", value="Remove a game from the watch list.", inline=False)
    embed.add_field(name="/watchlist", value="Show all watched games privately (only you see it).", inline=False)
    embed.add_field(name="/postwatchlist", value="Post the watch list publicly in the channel.", inline=False)
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
        steam_app_id, platforms = await fetch_game_store_info(session, result["id"])

    release_ts = result.get("first_release_date")
    upsert_game(result["id"], result["name"], release_ts, manual=True, image_url=image_url, steam_app_id=steam_app_id, platforms=platforms)

    now_ts = datetime.now(timezone.utc).timestamp()
    if release_ts and release_ts <= now_ts:
        # Game already launched — announce it immediately
        await interaction.followup.send(
            f"**{result['name']}** has already launched! Posting announcement now..."
        )
        con = sqlite3.connect(DB_PATH)
        channels = con.execute("SELECT channel_id FROM config").fetchall()
        con.close()
        embed = discord.Embed(
            title="🎮 Game Launch Today!",
            description=f"**{result['name']}** is out now!",
            color=discord.Color.green(),
        )
        release_dt = datetime.fromtimestamp(release_ts, tz=timezone.utc)
        embed.add_field(name="Release Date", value=discord.utils.format_dt(release_dt, "D"))
        if image_url:
            embed.set_image(url=image_url)
        for (channel_id,) in channels:
            channel = bot.get_channel(int(channel_id))
            if channel:
                await channel.send(embed=embed)
        mark_announced(result["id"])
    elif release_ts:
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


@tree.command(name="watchlist", description="Show all currently watched games (private — only you can see it)")
async def slash_watchlist(interaction: discord.Interaction):
    embed = _build_watchlist_embed()
    if not embed:
        await interaction.response.send_message("No games on the watch list yet.", ephemeral=True)
        return
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="postwatchlist", description="Post the game watch list publicly in this channel")
async def slash_postwatchlist(interaction: discord.Interaction):
    embed = _build_watchlist_embed()
    if not embed:
        await interaction.response.send_message("No games on the watch list yet.", ephemeral=True)
        return
    await interaction.response.defer()
    await interaction.channel.send(embed=embed)


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
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    wait_seconds = (midnight - now).total_seconds()
    log.info("Next daily check in %.0f seconds", wait_seconds)
    await asyncio.sleep(wait_seconds)


async def _startup_check():
    """Run sync and overdue announcements in the background on startup."""
    log.info("Running startup game check")
    await _sync_high_profile()
    await _announce_launches(include_overdue=True)
    log.info("Startup check complete")


@tasks.loop(hours=168)  # weekly
async def weekly_watchlist():
    log.info("Syncing games before weekly watchlist post")
    await _sync_high_profile()
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
    this_friday = (now + timedelta(days=days_until_friday)).replace(hour=23, minute=0, second=0, microsecond=0)

    if this_friday <= now:
        # Missed this week's window — post immediately then resume weekly cadence
        log.info("Missed weekly watchlist window, posting now")
        await weekly_watchlist()
        next_friday = this_friday + timedelta(weeks=1)
    else:
        next_friday = this_friday

    wait_seconds = (next_friday - now).total_seconds()
    log.info("Weekly watchlist posts in %.0f seconds", wait_seconds)
    await asyncio.sleep(wait_seconds)


def _build_watchlist_embed() -> discord.Embed | None:
    igdb_games = get_watchlist()
    steam_games = get_steam_watchlist()

    now_ts = datetime.now(timezone.utc).timestamp()

    def date_str(release_ts):
        if not release_ts:
            return "TBA"
        dt = datetime.fromtimestamp(release_ts, tz=timezone.utc)
        prefix = "released" if release_ts < now_ts else "releases"
        return f"{prefix} {discord.utils.format_dt(dt, 'D')}"

    def store_url(g: dict) -> str | None:
        if g.get("steam_app_id"):
            return f"https://store.steampowered.com/app/{g['steam_app_id']}/"
        if g.get("steam_id"):
            return f"https://store.steampowered.com/app/{g['steam_id']}/"
        return None

    def platform_badges(g: dict) -> str:
        raw = g.get("platforms") or ""
        badges = [b for b in raw.split(",") if b]
        return (" · " + " · ".join(badges)) if badges else ""

    def format_entry(g: dict, tag: str) -> str:
        url = store_url(g)
        name = f"[{g['name']}]({url})" if url else f"**{g['name']}**"
        return f"{tag} {name}{platform_badges(g)} — {date_str(g['release_ts'])}"

    igdb_names = {g["name"].lower() for g in igdb_games}
    all_games = []
    for g in igdb_games:
        all_games.append({**g, "tag": "📌" if g["manual"] else "🔥"})
    for g in steam_games:
        if g["name"].lower() not in igdb_names:
            all_games.append({**g, "tag": "🎮"})

    all_games.sort(key=lambda g: g["release_ts"] if g["release_ts"] else float("inf"))

    if not all_games:
        return None

    lines = [format_entry(g, g["tag"]) for g in all_games]
    embed = discord.Embed(
        title="Game Watch List",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="🔥 = IGDB high-profile  |  📌 = manually added  |  🎮 = Steam wishlisted")
    return embed


async def _post_watchlist(channel: discord.TextChannel):
    embed = _build_watchlist_embed()
    if embed:
        await channel.send(embed=embed)


async def _sync_high_profile() -> int:
    total = 0
    async with aiohttp.ClientSession() as session:
        # IGDB
        games = await fetch_high_profile_upcoming(session)
        if isinstance(games, list):
            valid = [g for g in games if "id" in g and "name" in g]
            store_info = await fetch_store_info_batch(session, [g["id"] for g in valid])
            for g in valid:
                image_url = await fetch_cover_url(session, g["id"])
                steam_app_id, platforms = store_info.get(g["id"], (None, None))
                upsert_game(g["id"], g["name"], g.get("first_release_date"), manual=False, image_url=image_url, source="igdb", steam_app_id=steam_app_id, platforms=platforms)
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


async def _announce_launches(include_overdue: bool = False):
    igdb_launching = get_unannounced_launching_today()
    igdb_names = {g["name"].lower() for g in igdb_launching}
    steam_launching = [g for g in get_steam_launching_today() if g["name"].lower() not in igdb_names]
    launching = igdb_launching + steam_launching

    if include_overdue:
        today_names = {g["name"].lower() for g in launching}
        overdue = [g for g in get_overdue_unannounced() if g["name"].lower() not in today_names]
        launching += overdue
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
