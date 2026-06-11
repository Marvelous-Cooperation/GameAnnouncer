import os
import asyncio
import aiohttp
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import discord
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
    """Scrape Steam's most wishlisted upcoming games. Returns list of {name, release_ts, image_url}."""
    url = "https://store.steampowered.com/explore/upcoming/"
    headers = {"Accept-Language": "en-US,en;q=0.9"}
    try:
        async with session.get(url, headers=headers) as resp:
            html = await resp.text()
    except Exception as e:
        log.error("Failed to fetch Steam upcoming page: %s", e)
        return []

    import re
    games = []

    # Extract app IDs and names from the wishlist section
    # Each entry looks like: data-ds-appid="XXXXXX" ... <span class="tab_item_name">Game Name</span>
    app_blocks = re.findall(r'data-ds-appid="(\d+)".*?<span class="tab_item_name">(.*?)</span>', html, re.DOTALL)

    for app_id, raw_name in app_blocks[:20]:
        name = re.sub(r'<[^>]+>', '', raw_name).strip()
        if not name:
            continue

        # Fetch release date and header image from Steam API
        release_ts = None
        image_url = None
        try:
            api_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&filters=basic,release_date"
            async with session.get(api_url) as api_resp:
                data = await api_resp.json()
            app_data = data.get(str(app_id), {}).get("data", {})
            image_url = app_data.get("header_image")
            rd = app_data.get("release_date", {})
            if rd and not rd.get("coming_soon") and rd.get("date"):
                try:
                    from dateutil import parser as dateparser
                    dt = dateparser.parse(rd["date"])
                    if dt:
                        release_ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
                except Exception:
                    pass
        except Exception as e:
            log.warning("Could not fetch Steam details for app %s: %s", app_id, e)

        games.append({"steam_id": app_id, "name": name, "release_ts": release_ts, "image_url": image_url})

    log.info("Fetched %d games from Steam wishlist", len(games))
    return games


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    daily_check.start()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.command(name="helpGA")
async def help_ga(ctx: commands.Context):
    """List all GameAnnouncer commands."""
    embed = discord.Embed(
        title="GameAnnouncer Commands",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="!watch <game>", value="Add a game to the watch list by name.", inline=False)
    embed.add_field(name="!unwatch <game>", value="Remove a game from the watch list.", inline=False)
    embed.add_field(name="!watchlist", value="Show all watched games with their release dates.", inline=False)
    embed.add_field(name="!setchannel [#channel]", value="Set the channel where launch announcements are posted. Defaults to the current channel. *(Requires Manage Channels)*", inline=False)
    embed.add_field(name="!syncgames", value="Manually pull the latest high-profile upcoming games from IGDB. *(Requires Manage Server)*", inline=False)
    embed.add_field(name="!testannounce", value="Send a sample launch announcement to preview what it looks like. *(Requires Manage Server)*", inline=False)
    embed.set_footer(text="🔥 = auto-tracked high-profile  |  📌 = manually added")
    await ctx.send(embed=embed)


@bot.command(name="setchannel")
@commands.has_permissions(manage_channels=True)
async def setchannel(ctx: commands.Context, channel: discord.TextChannel | None = None):
    """Set the channel where game announcements will be posted."""
    target = channel or ctx.channel
    set_channel(ctx.guild.id, target.id)
    await ctx.send(f"Announcements will be posted in {target.mention}.")


@bot.command(name="watch")
async def watch(ctx: commands.Context, *, game_name: str):
    """Add a game to the watch list by name."""
    async with aiohttp.ClientSession() as session:
        results = await search_game(session, game_name)
        if not results:
            await ctx.send(f"No games found matching **{game_name}**.")
            return
        game = results[0]
        image_url = await fetch_cover_url(session, game["id"])

    release_ts = game.get("first_release_date")
    upsert_game(game["id"], game["name"], release_ts, manual=True, image_url=image_url)

    if release_ts:
        release_dt = datetime.fromtimestamp(release_ts, tz=timezone.utc)
        await ctx.send(
            f"Now watching **{game['name']}** — releases {discord.utils.format_dt(release_dt, 'D')}."
        )
    else:
        await ctx.send(f"Now watching **{game['name']}** (no release date yet).")


@bot.command(name="unwatch")
async def unwatch(ctx: commands.Context, *, game_name: str):
    """Remove a game from the watch list."""
    watchlist = get_watchlist()
    name_lower = game_name.lower()
    matches = [g for g in watchlist if name_lower in g["name"].lower()]

    if not matches:
        await ctx.send(f"No watched game matches **{game_name}**.")
        return

    for g in matches:
        remove_game(g["igdb_id"])

    names = ", ".join(f"**{g['name']}**" for g in matches)
    await ctx.send(f"Removed {names} from the watch list.")


@bot.command(name="watchlist")
async def watchlist_cmd(ctx: commands.Context):
    """Show all currently watched games."""
    igdb_games = get_watchlist()
    steam_games = get_steam_watchlist()

    if not igdb_games and not steam_games:
        await ctx.send("No games on the watch list yet.")
        return

    now_ts = datetime.now(timezone.utc).timestamp()

    def date_str(release_ts):
        if not release_ts:
            return "TBA"
        dt = datetime.fromtimestamp(release_ts, tz=timezone.utc)
        prefix = "released" if release_ts < now_ts else "releases"
        return f"{prefix} {discord.utils.format_dt(dt, 'D')}"

    lines = []
    for g in igdb_games:
        tag = "📌" if g["manual"] else "🔥"
        lines.append(f"{tag} **{g['name']}** — {date_str(g['release_ts'])}")
    for g in steam_games:
        lines.append(f"🎮 **{g['name']}** — {date_str(g['release_ts'])}")

    embed = discord.Embed(
        title="Game Watch List",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="🔥 = IGDB high-profile  |  📌 = manually added  |  🎮 = Steam wishlisted")
    await ctx.send(embed=embed)


@bot.command(name="testannounce")
@commands.has_permissions(manage_guild=True)
async def testannounce(ctx: commands.Context):
    """Send a sample launch announcement to see what it looks like."""
    async with aiohttp.ClientSession() as session:
        # Use The Blood of Dawnwalker (id 282831) as a real example
        image_url = await fetch_cover_url(session, 282831)
    embed = discord.Embed(
        title="🎮 Game Launch Today!",
        description="**The Blood of Dawnwalker** is out today!",
        color=discord.Color.green(),
    )
    embed.add_field(name="Release Date", value=discord.utils.format_dt(datetime.now(timezone.utc), "D"))
    if image_url:
        embed.set_image(url=image_url)
    await ctx.send(embed=embed)


@bot.command(name="syncgames")
@commands.has_permissions(manage_guild=True)
async def syncgames(ctx: commands.Context):
    """Manually trigger a sync of high-profile upcoming games from IGDB."""
    await ctx.send("Syncing high-profile upcoming games from IGDB...")
    count = await _sync_high_profile()
    await ctx.send(f"Done. {count} high-profile game(s) now tracked.")


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
    launching = get_unannounced_launching_today() + get_steam_launching_today()
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
# Error handling
# ---------------------------------------------------------------------------

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to use that command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`.")
    else:
        log.error("Command error: %s", error)
        await ctx.send("An error occurred. Check the bot logs.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    bot.run(DISCORD_TOKEN)
