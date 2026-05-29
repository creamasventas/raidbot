# 🤖 Discord Bot — Beginner's Guide

A beginner-friendly Discord bot built with **Python + discord.py**, featuring
slash commands, modular cogs, SQLite, async support, and Docker.

---

## 📁 Project Structure

```
discord-bot/
├── cogs/                   # One file per feature group (modular)
│   ├── __init__.py
│   ├── general.py          # /ping
│   ├── profile.py          # /profile  +  passive XP listener
│   └── leaderboard.py      # /leaderboard
├── utils/
│   ├── __init__.py
│   └── database.py         # Async SQLite wrapper
├── data/                   # SQLite database lives here (git-ignored)
├── config.py               # Centralised settings from env vars
├── main.py                 # Entry point
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example            # Template — copy to .env
└── .gitignore
```

---

## 🛠️ Step 1 — Create a Discord Application & Bot

1. Go to <https://discord.com/developers/applications>
2. Click **New Application** → give it a name → **Create**
3. In the left sidebar click **Bot**
4. Click **Add Bot** → **Yes, do it!**
5. Under **Token** click **Reset Token** and copy it — you'll need it shortly
6. Scroll down to **Privileged Gateway Intents** and enable:
   - ✅ **Server Members Intent**
   - ✅ **Message Content Intent**
7. Click **Save Changes**

> ⚠️ Never share your token publicly. Treat it like a password.

---

## 🔗 Step 2 — Invite the Bot to Your Server

1. In the sidebar click **OAuth2** → **URL Generator**
2. Under **Scopes** tick: `bot` and `applications.commands`
3. Under **Bot Permissions** tick:
   - `Send Messages`
   - `Embed Links`
   - `Read Message History`
   - `View Channels`
4. Copy the generated URL and open it in your browser
5. Select your server → **Authorise**

---

## ⚙️ Step 3 — Configure Environment Variables

```bash
cp .env.example .env
```

Open `.env` and set:

```dotenv
DISCORD_TOKEN=paste_your_token_here

# For faster dev: set this to your server/guild ID (right-click server → Copy ID)
# Commands sync instantly to a single guild instead of waiting up to 1 hour globally.
GUILD_ID=your_guild_id_here
```

To find your Guild ID: in Discord go to **Settings → Advanced** and enable
**Developer Mode**, then right-click your server name → **Copy Server ID**.

---

## 🚀 Step 4 — Run Locally

```bash
# 1. Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the bot
python main.py
```

You should see output like:
```
2024-01-01 12:00:00  INFO      __main__: Logged in as MyBot#1234 (ID: 123456789)
2024-01-01 12:00:00  INFO      __main__: Connected to 1 guild(s).
```

---

## 🐳 Step 5 — Run with Docker

```bash
# Build and start in the background
docker compose up -d

# Stream logs
docker compose logs -f

# Stop
docker compose down
```

The SQLite database is stored in a Docker volume (`db_data`) so it persists
across container restarts.

---

## 💬 Commands

| Command | Description |
|---|---|
| `/ping` | Check bot latency |
| `/profile [user]` | View XP, level, and message count |
| `/leaderboard` | Top 10 members by XP |

XP is earned automatically: **5 XP per message** sent in any channel the bot
can see. Level-up announcements are posted in the same channel.

---

## 🧩 Adding New Commands

1. Create `cogs/mycog.py`:

```python
import discord
from discord import app_commands
from discord.ext import commands

class MyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="hello", description="Say hello!")
    async def hello(self, interaction: discord.Interaction):
        await interaction.response.send_message("Hello! 👋")

async def setup(bot):
    await bot.add_cog(MyCog(bot))
```

2. Register it in `main.py` → `_load_cogs()`:

```python
simple_cogs = ["cogs.general", "cogs.mycog"]   # ← add here
```

3. Restart the bot — the new slash command will appear automatically.
