# ticker_gex — on-demand ticker dealerflow for Discord

Type `/gex TSLA` in Discord and get back the same 5-message dealerflow post the SPY
pipeline ships, for **any** ticker:

1. **Summary card** — magnet table (Call/Put walls, ΣGEX/ΣVanna/ΣCharm per expiry) + caption
2. **Gamma (GEX)** heatmap
3. **Vanna (VEX)** heatmap
4. **Charm (CEX)** heatmap
5. **Front-expiry triptych** (gamma · vanna · charm side by side)

Same math (dealer-signed, VEX per 1.00σ), same `$X,XXX.XK` formatting, same dark style —
just parameterized by ticker. This package is fully self-contained and additive; it does
not modify or depend on the existing SPY pipeline, the screener, or their workflows.

---

## How requests are received

A Discord **webhook is send-only** — it cannot receive a user's ticker. To *receive* `/gex`
you need a "front door". Three ways:

| Path | What it is | Trade-off |
|------|------------|-----------|
| **Serverless** (`discord-endpoint/`) | A tiny Vercel function Discord calls directly; verifies + ACKs, then fires the Actions workflow | No always-on host (~30–60s to images). **Recommended** — see [`discord-endpoint/README.md`](../discord-endpoint/README.md) |
| **Bot** (`ticker_gex/bot.py`) | Always-on `discord.py` app; `/gex` works instantly | Needs a host that stays running |
| **Actions only** (`.github/workflows/ticker-gex-run.yml`) | `workflow_dispatch` with a `ticker` input | No hosting, but you trigger it from the Actions tab, not from chat |

In all cases the 5 images are posted to your **webhook's channel**.

---

## 1. Add the bot to your server

Open this invite URL — replace `<YOUR_APP_ID>` with your application's **Client ID** (Discord
Developer Portal → your app → **General Information** → Application ID). Note **both** scopes —
`bot` *and* `applications.commands`; the second is what lets slash commands register:

```
https://discord.com/oauth2/authorize?client_id=<YOUR_APP_ID>&permissions=8515702525261888&integration_type=0&scope=bot+applications.commands
```

> A `bot`-only invite won't register slash commands. If `/gex` ever fails to appear, re-open
> the URL above to authorize the `applications.commands` scope.

No privileged intents are required for slash commands. (The optional `!gex` text command
needs the **Message Content** privileged intent — leave it off unless you enable
`TICKER_GEX_ENABLE_PREFIX`.)

## 2. Configure secrets

Copy your two secrets into a **gitignored** `.env` at the repo root (never commit it):

```dotenv
TICKER_GEX_DISCORD_BOT_TOKEN=your-bot-token
TICKER_GEX_DISCORD_WEBHOOK=https://discord.com/api/webhooks/.../...
```

## 3. Install dependencies

```bash
pip install -r ticker_gex/requirements.txt
```

## 4. Run the bot

```bash
python -m ticker_gex.bot
```

On startup it prints `Logged in as …`, the servers it is in, and `Synced /gex to N
server(s)`. Type `/gex` in Discord and the command appears. **The bot only responds while
this process is running** — host it (below) so it stays up.

> **macOS note:** if you see `CERTIFICATE_VERIFY_FAILED` on login, your Python lacks CA
> certs. Run the bundled *"Install Certificates.command"* for your Python version, or
> start the bot with `SSL_CERT_FILE=$(python -m certifi)` set. (Linux/Docker hosts already
> have CA certs.)

---

## Configuration (environment variables)

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `TICKER_GEX_DISCORD_BOT_TOKEN` | ✅ | — | Bot token (developer portal → your app → Bot) |
| `TICKER_GEX_DISCORD_WEBHOOK` | ✅ | — | Webhook the 5 messages post to |
| `TICKER_GEX_CHANNEL_ID` | — | (any) | Restrict `/gex` to one channel id |
| `TICKER_GEX_GUILD_ID` | — | (auto) | Pin slash-sync to one server id instead of auto-syncing to all |
| `TICKER_GEX_GLOBAL_SYNC` | — | off | Force one global sync (public multi-server bot; can take ~1h) |
| `TICKER_GEX_COOLDOWN_SECONDS` | — | 20 | Per-user cooldown |
| `TICKER_GEX_MAX_CONCURRENCY` | — | 2 | Concurrent renders |
| `TICKER_GEX_JOB_TIMEOUT` | — | 240 | Seconds before a job is abandoned |
| `TICKER_GEX_ENABLE_PREFIX` | — | off | Also enable `!gex` (needs Message Content intent) |

**Sync behavior:** with no `GUILD_ID` and `GLOBAL_SYNC` off, the bot auto-syncs `/gex` to
every server it is already in — instant availability, no config needed.

---

## Hosting the always-on bot

Any host that runs a long-lived Python worker works. A `Dockerfile` and `Procfile`
(`worker: python -m ticker_gex.bot`) are included at the repo root.

- **Railway / Render / Fly.io** — deploy the repo as a *worker/background* service (not a
  web service; there's no HTTP port). Set `TICKER_GEX_DISCORD_BOT_TOKEN` and
  `TICKER_GEX_DISCORD_WEBHOOK` as environment variables in the dashboard. Start command:
  `python -m ticker_gex.bot`.
- **Docker** (anywhere):
  ```bash
  docker build -t ticker-gex .
  docker run -d --restart=unless-stopped \
    -e TICKER_GEX_DISCORD_BOT_TOKEN=... \
    -e TICKER_GEX_DISCORD_WEBHOOK=... \
    ticker-gex
  ```
- **systemd / VPS** — run `python -m ticker_gex.bot` under a service unit with
  `Restart=always` and the two env vars in the unit's `Environment=`.

---

## No-host fallback: GitHub Actions

If you don't want to host anything, trigger a single render from the **Actions** tab:

1. Add a repo secret `TICKER_GEX_DISCORD_WEBHOOK` (Settings → Secrets and variables →
   Actions).
2. Actions → **ticker-gex-run** → *Run workflow* → enter a `ticker` → Run.

It runs the engine once, posts the 5 messages to the webhook, and uploads the PNGs as
build artifacts. This is on-demand from the Actions UI, not from Discord chat.

---

## Run it once from the CLI (no Discord)

```bash
python -m ticker_gex.engine --ticker QQQ --no-post --out-dir ./out   # render only
python -m ticker_gex.engine --ticker QQQ                              # render + post (uses env webhook)
```

## Tests

Offline, no network, no Discord connection:

```bash
python -m ticker_gex.tests.test_ticker_gex
```

---

## Security

- **Never commit `.env`** — it is gitignored. The bot token and webhook are secrets.
- If a token is ever pasted into chat, a log, or a commit, **reset it** in the Discord
  developer portal (your app → Bot → *Reset Token*) and update `.env`.
- The webhook URL is also a secret — anyone with it can post to that channel.

## Caveats

- On-demand expiry selection includes today's 0DTE only before 16:00 ET. It does **not**
  model NYSE early-close half-days (1:00 PM ET), so on those rare days a closed 0DTE could
  still appear.
- Rendering uses matplotlib's global pyplot state, which is not thread-safe; the engine
  serializes all rendering, so concurrent `/gex` requests queue at the render step.
