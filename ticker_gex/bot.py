"""Discord bot that turns ``/gex <ticker>`` into the 5-message dealerflow post.

A Discord **webhook is send-only** — it cannot receive a user's ticker — so receiving
requests needs a real bot (a persistent gateway connection + bot token). This module is
that listener: a ``discord.py`` v2 application that registers an application (slash)
command ``/gex ticker:<symbol>``. Slash commands arrive over the gateway, so no public
HTTP endpoint is required; the bot just needs to stay running.

On invoke it defers (so it beats Discord's 3-second ack window), runs
:func:`ticker_gex.engine.run_for_ticker` in a thread-pool executor (never blocking the
gateway heartbeat), and posts the five images to the configured **webhook**. The five
messages land in the webhook's channel; the invoking user gets an ephemeral status.

Guardrails: a per-user cooldown, a small global concurrency limit, a per-job timeout, an
optional channel restriction, and friendly errors for invalid / illiquid / failing
tickers. An optional ``!gex`` prefix command is available when explicitly enabled (it
needs the privileged Message Content intent).

Configuration (environment):
  TICKER_GEX_DISCORD_BOT_TOKEN   (required) the bot token
  TICKER_GEX_DISCORD_WEBHOOK     (required) webhook the 5 messages are posted to
  TICKER_GEX_CHANNEL_ID          (optional) restrict the command to one channel id
  TICKER_GEX_GUILD_ID            (optional) guild id for instant slash-command sync
  TICKER_GEX_COOLDOWN_SECONDS    (optional, default 20) per-user cooldown
  TICKER_GEX_MAX_CONCURRENCY     (optional, default 2) concurrent renders
  TICKER_GEX_JOB_TIMEOUT         (optional, default 240) seconds before a job is abandoned
  TICKER_GEX_ENABLE_PREFIX       (optional, default off) also enable the !gex prefix command

Run:  ``python -m ticker_gex.bot``
"""
import asyncio
import math
import os
import sys
import tempfile
import time

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from ticker_gex.engine import TickerGexResult, run_for_ticker, validate_ticker

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PKG_DIR)
load_dotenv(os.path.join(_REPO_ROOT, ".env"))


def _int_env(name, default):
    try:
        return int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


BOT_TOKEN = os.getenv("TICKER_GEX_DISCORD_BOT_TOKEN")
WEBHOOK = os.getenv("TICKER_GEX_DISCORD_WEBHOOK")
CHANNEL_ID = _int_env("TICKER_GEX_CHANNEL_ID", 0)
GUILD_ID = _int_env("TICKER_GEX_GUILD_ID", 0)
COOLDOWN_SECONDS = _int_env("TICKER_GEX_COOLDOWN_SECONDS", 20)
MAX_CONCURRENCY = max(1, _int_env("TICKER_GEX_MAX_CONCURRENCY", 2))
JOB_TIMEOUT = max(30, _int_env("TICKER_GEX_JOB_TIMEOUT", 240))
ENABLE_PREFIX = os.getenv("TICKER_GEX_ENABLE_PREFIX", "").strip().lower() in {"1", "true", "yes", "on"}
# Force a single global command sync (for a public, multi-server bot). Default is the
# friendlier path: sync to whatever server(s) the bot is already in, which is instant.
GLOBAL_SYNC = os.getenv("TICKER_GEX_GLOBAL_SYNC", "").strip().lower() in {"1", "true", "yes", "on"}

_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
_last_used = {}  # user_id -> monotonic timestamp of last accepted request


def _cooldown_remaining(user_id):
    """Whole seconds left on this user's cooldown (0 if ready)."""
    if COOLDOWN_SECONDS <= 0:
        return 0
    last = _last_used.get(user_id)
    if last is None:
        return 0
    elapsed = time.monotonic() - last
    return max(0, math.ceil(COOLDOWN_SECONDS - elapsed))


def _mark_cooldown(user_id):
    _last_used[user_id] = time.monotonic()


def _status_text(result: TickerGexResult):
    """Friendly one-line status for the invoking user."""
    if result.ok:
        return f"\u2705 Posted the **{result.ticker}** dealerflow ({result.posted} messages)."
    if result.partial:
        return f"\u26a0\ufe0f {result.message}"
    return f"\u274c {result.message}"


async def _run_job(sym):
    """Run the (blocking) engine off the event loop, guarded by concurrency + timeout."""
    loop = asyncio.get_running_loop()

    def job():
        # A fresh temp dir per request keeps filenames unique and cleans up after posting.
        with tempfile.TemporaryDirectory(prefix=f"ticker_gex_{sym}_") as d:
            return run_for_ticker(sym, webhook_url=WEBHOOK, out_dir=d)

    async with _semaphore:
        try:
            return await asyncio.wait_for(loop.run_in_executor(None, job), timeout=JOB_TIMEOUT)
        except asyncio.TimeoutError:
            return TickerGexResult(
                ok=False, ticker=sym, phase="timeout",
                message=f"Timed out building the {sym} dealerflow after {JOB_TIMEOUT}s — try again.")
        except Exception as e:  # noqa: BLE001 - surface, don't crash the gateway
            return TickerGexResult(
                ok=False, ticker=sym, phase="executor",
                error=str(e), message=f"Unexpected error building {sym}: {e}")


def _intents():
    intents = discord.Intents.default()
    if ENABLE_PREFIX:
        # The !gex text command needs to read message content (a privileged intent that
        # must also be enabled for the bot in the Discord developer portal).
        intents.message_content = True
    return intents


class TickerGexBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=_intents(), help_command=None)
        self._auto_synced = False

    async def setup_hook(self):
        # Pinned guild -> sync to it now (instant, and the only commands that exist, so no
        # duplicates). Explicit global sync -> push once (can take up to ~1h to appear).
        # Otherwise defer to on_ready, where we can see which servers the bot is actually
        # in and sync to each for instant availability.
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
        elif GLOBAL_SYNC:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} global command(s) (may take up to ~1h to appear)")

    async def on_ready(self):
        where = f" \u00b7 restricted to channel {CHANNEL_ID}" if CHANNEL_ID else ""
        print(f"Logged in as {self.user} (id {self.user.id}){where}")
        print(f"In {len(self.guilds)} server(s): {[(g.name, g.id) for g in self.guilds]}")
        if GUILD_ID or GLOBAL_SYNC or self._auto_synced:
            return
        if not self.guilds:
            print("Not in any server yet \u2014 invite the bot with BOTH the `bot` and "
                  "`applications.commands` scopes (see ticker_gex/README.md), then restart.")
            return
        total = 0
        for g in self.guilds:
            try:
                self.tree.copy_global_to(guild=g)
                total += len(await self.tree.sync(guild=g))
            except discord.Forbidden:
                print(f"Missing Access syncing to '{g.name}' ({g.id}) \u2014 re-invite the bot "
                      f"with the `applications.commands` scope.")
            except discord.HTTPException as e:
                print(f"Sync to '{g.name}' ({g.id}) failed: {e}")
        self._auto_synced = True
        print(f"Synced /gex to {len(self.guilds)} server(s); the command is available now.")


bot = TickerGexBot()


@bot.tree.command(name="gex", description="Post the gamma/vanna/charm dealerflow heatmaps for a ticker")
@app_commands.describe(ticker="Ticker symbol, e.g. SPY, QQQ, TSLA")
async def gex(interaction: discord.Interaction, ticker: str):
    if CHANNEL_ID and interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message(
            f"Please use <#{CHANNEL_ID}> for `/gex`.", ephemeral=True)
        return

    sym, err = validate_ticker(ticker)
    if err:
        await interaction.response.send_message(f"\u274c {err}", ephemeral=True)
        return

    remaining = _cooldown_remaining(interaction.user.id)
    if remaining > 0:
        await interaction.response.send_message(
            f"\u23f3 Easy there — try `/gex` again in {remaining}s.", ephemeral=True)
        return
    _mark_cooldown(interaction.user.id)

    # Defer *before* waiting on the concurrency semaphore so we never miss the 3s ack.
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.edit_original_response(content=f"\u23f3 Pulling dealerflow for **{sym}**\u2026")
    result = await _run_job(sym)
    await interaction.edit_original_response(content=_status_text(result))


if ENABLE_PREFIX:
    @bot.command(name="gex")
    async def gex_prefix(ctx: commands.Context, ticker: str = None):
        if CHANNEL_ID and ctx.channel.id != CHANNEL_ID:
            return
        sym, err = validate_ticker(ticker)
        if err:
            await ctx.reply(f"\u274c {err}", mention_author=False)
            return
        remaining = _cooldown_remaining(ctx.author.id)
        if remaining > 0:
            await ctx.reply(f"\u23f3 Easy there — try `!gex` again in {remaining}s.",
                            mention_author=False)
            return
        _mark_cooldown(ctx.author.id)
        status = await ctx.reply(f"\u23f3 Pulling dealerflow for **{sym}**\u2026",
                                 mention_author=False)
        result = await _run_job(sym)
        await status.edit(content=_status_text(result))


def main():
    if not BOT_TOKEN:
        sys.exit("TICKER_GEX_DISCORD_BOT_TOKEN is not set — a bot token is required to "
                 "receive /gex requests.")
    if not WEBHOOK:
        sys.exit("TICKER_GEX_DISCORD_WEBHOOK is not set — set the webhook the 5 dealerflow "
                 "messages should be posted to.")
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
