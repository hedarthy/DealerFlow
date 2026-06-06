"""One-time helper: register the global ``/gex`` slash command with Discord.

The serverless function only *handles* interactions; the command still has to exist on the
application. The gateway bot registers it on startup, but for a pure-serverless deployment
run this once instead (no always-on process needed):

    # uses values from the repo-root .env, or pass them as env vars
    python discord-endpoint/register_commands.py            # global (visible everywhere, ~1h to propagate)
    DISCORD_GUILD_ID=123 python discord-endpoint/register_commands.py   # one guild, instant

Reads:
  TICKER_GEX_DISCORD_BOT_TOKEN  (or DISCORD_BOT_TOKEN)  the bot token
  DISCORD_APP_ID                (or DISCORD_CLIENT_ID)  the application/client id
  DISCORD_GUILD_ID              (optional) register to one guild for instant availability
"""
import json
import os
import sys

import requests

try:
    from dotenv import load_dotenv
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_root, ".env"))
except Exception:  # noqa: BLE001 - dotenv is optional here
    pass

TOKEN = os.environ.get("TICKER_GEX_DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN")
APP_ID = os.environ.get("DISCORD_APP_ID") or os.environ.get("DISCORD_CLIENT_ID")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "").strip()

COMMAND = {
    "name": "gex",
    "description": "Post the gamma/vanna/charm dealerflow heatmaps for a ticker",
    "type": 1,  # CHAT_INPUT
    "options": [
        {
            "name": "ticker",
            "description": "Ticker symbol, e.g. SPY, QQQ, TSLA",
            "type": 3,  # STRING
            "required": True,
        }
    ],
}


def main():
    if not TOKEN:
        sys.exit("Set TICKER_GEX_DISCORD_BOT_TOKEN (or DISCORD_BOT_TOKEN).")
    if not APP_ID:
        sys.exit("Set DISCORD_APP_ID (or DISCORD_CLIENT_ID) to your application id.")

    base = f"https://discord.com/api/v10/applications/{APP_ID}"
    url = f"{base}/guilds/{GUILD_ID}/commands" if GUILD_ID else f"{base}/commands"
    scope = f"guild {GUILD_ID}" if GUILD_ID else "global"

    resp = requests.put(
        url,
        headers={"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"},
        data=json.dumps([COMMAND]),
        timeout=15,
    )
    if resp.status_code in (200, 201):
        print(f"Registered /gex ({scope}). Discord returned {resp.status_code}.")
        if not GUILD_ID:
            print("Global commands can take up to ~1h to appear in every server.")
    else:
        sys.exit(f"Failed ({resp.status_code}): {resp.text[:500]}")


if __name__ == "__main__":
    main()
