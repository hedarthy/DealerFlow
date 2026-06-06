# Container image for the on-demand ticker dealerflow Discord bot (ticker_gex/bot.py).
#
# The bot is a persistent gateway worker: it must run 24/7 to receive /gex commands.
# GitHub Actions cannot host a long-lived process, so deploy this image to a small
# always-on host (Railway / Render / Fly.io / a VPS). See ticker_gex/README.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first for better layer caching. ticker_gex/requirements.txt pulls
# in the repo-root requirements via `-r ../requirements.txt`, so copy both.
COPY requirements.txt ./requirements.txt
COPY ticker_gex/requirements.txt ./ticker_gex/requirements.txt
RUN pip install --no-cache-dir -r ticker_gex/requirements.txt

COPY . .

# Required env at runtime: TICKER_GEX_DISCORD_BOT_TOKEN, TICKER_GEX_DISCORD_WEBHOOK.
# Optional: TICKER_GEX_CHANNEL_ID, TICKER_GEX_GUILD_ID, TICKER_GEX_COOLDOWN_SECONDS,
# TICKER_GEX_MAX_CONCURRENCY, TICKER_GEX_JOB_TIMEOUT, TICKER_GEX_ENABLE_PREFIX.
CMD ["python", "-m", "ticker_gex.bot"]
