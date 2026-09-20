# GameAnnouncer

A Discord bot that tracks upcoming game releases and announces them on launch day. It automatically pulls high-profile games from IGDB and Steam's most wishlisted list, and lets users manually add specific games to watch.

## Features

- **Auto-tracking** — pulls upcoming games daily from IGDB (by hype score) and Steam (by wishlist count)
- **Manual tracking** — add any game by name with `/watch`
- **Launch announcements** — posts an embed with cover art on the day a tracked game releases
- **Weekly digest** — automatically posts the full watch list every Friday at 6pm EST
- **Review milestones** — shouts out tracked games that reach Overwhelmingly Positive on Steam after launch
- **Sleeper hits** — spots games *not* on the watch list that hit Overwhelmingly Positive within a month of release
- **Deduplication** — games appearing in both IGDB and Steam are shown only once

## Commands

| Command | Description | Permission |
|---|---|---|
| `/watch <game>` | Add an upcoming game to the watch list by name (already-released games are rejected) | Everyone |
| `/unwatch <game>` | Remove a game from the watch list | Everyone |
| `/watchlist` | Show all watched games sorted by release date | Everyone |
| `/setchannel [#channel]` | Set the channel for announcements | Manage Channels |
| `/syncgames` | Manually sync latest games from IGDB + Steam | Manage Server |
| `/testannounce` | Preview what a launch announcement looks like | Manage Server |
| `/help` | List all commands | Everyone |

## Watch List Tags

- 🔥 — Auto-tracked from IGDB (high hype score)
- 🎮 — Auto-tracked from Steam (most wishlisted)
- 📌 — Manually added via `/watch`

## Setup

### 1. Create a Discord Application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and create a new application
2. Go to **Bot** → click **Reset Token** and copy your token
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**

### 2. Get IGDB Credentials

1. Go to [dev.twitch.tv/console](https://dev.twitch.tv/console) and register a new application
2. Set the OAuth Redirect URL to `http://localhost` and category to **Application Integration**
3. Copy the **Client ID** and generate a **Client Secret**

### 3. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```
DISCORD_TOKEN=your_discord_bot_token
IGDB_CLIENT_ID=your_twitch_client_id
IGDB_CLIENT_SECRET=your_twitch_client_secret
HYPE_THRESHOLD=50
```

`HYPE_THRESHOLD` controls how hyped a game needs to be on IGDB to be auto-tracked. Lower = more games, higher = only the biggest titles.

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Invite the Bot

In the Discord Developer Portal, go to **OAuth2 → URL Generator** and select these scopes and permissions:

- Scopes: `bot`, `applications.commands`
- Permissions: Send Messages, Embed Links, Read Message History

Open the generated URL in your browser to add the bot to your server.

### 6. Run

```bash
python bot.py
```

Once running, use `/setchannel` in Discord to set where announcements should be posted.

## Hosting

The bot is designed to run continuously. [Railway](https://railway.com) is recommended for free cloud hosting — connect your GitHub repo and add the environment variables in the Railway dashboard.

## How It Works

- **Daily at midnight UTC** — syncs IGDB and Steam for new/updated games, announces any games launching that day
- **Every Friday at 6pm EST** — syncs and posts the full watch list to the announcement channel
- **Every 6 hours** — checks Steam reviews for tracked games that just launched, and scans Steam's Popular New Releases for untracked games that hit Overwhelmingly Positive within `SLEEPER_HIT_WINDOW_DAYS` (default 30) of release; each game is announced once
- Game data is stored in a local SQLite database (`games.db`)
- IGDB images (artwork/cover) and Steam header images are embedded in announcements
