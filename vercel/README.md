# `/gex` via Vercel → GitHub Actions (no always-on host)

This folder is a tiny **Vercel serverless function** that lets `/gex <ticker>` work without
hosting a persistent bot. It is the "front door" Discord requires:

```
Discord  ──slash /gex TSLA──▶  Vercel function  ──workflow_dispatch──▶  GitHub Actions
                                     │                                        │
                                     └─ verifies Ed25519 signature            └─ renders + posts
                                        ACKs in <3s (ephemeral)                   5 images to your
                                                                                  webhook channel
```

Why a function at all? A slash command must be acknowledged within 3 seconds and proven via
an Ed25519 signature. GitHub Actions can do neither directly. This function scales to zero
(you pay nothing when idle), wakes per command, verifies the signature, and fires the
`ticker-gex-run` workflow. **Setting this as the app's Interactions Endpoint URL replaces
the always-on gateway bot** — you no longer need to host `ticker_gex/bot.py`.

---

## Prerequisites (do these first)

1. **Merge the workflow to your default branch.** `workflow_dispatch` via the REST API only
   finds a workflow if `ticker-gex-run.yml` exists on the repo's **default branch (`main`)**.
   Merge this branch first, then keep `GH_REF=main`.
2. **Add the Actions secret** `TICKER_GEX_DISCORD_WEBHOOK` in the repo
   (Settings → Secrets and variables → **Actions**) — the workflow posts the 5 images to it.
3. **Create a fine-grained PAT** for the function to dispatch the workflow:
   GitHub → Settings → Developer settings → **Fine-grained tokens** → only this repo →
   Repository permissions → **Actions: Read and write** (Metadata read is implied). Copy it.

---

## Deploy

1. `npm i -g vercel` (or use the Vercel dashboard “Import Project”).
2. **Set the project Root Directory to `vercel`** (Vercel dashboard → Project → Settings →
   General → Root Directory = `vercel`). This is important: it makes Vercel install this
   folder's tiny `requirements.txt` (PyNaCl + requests) instead of the repo's heavy
   pandas/matplotlib stack.
3. Add **Environment Variables** (Project → Settings → Environment Variables):

   | Variable | Required | Example / default | Notes |
   |----------|----------|-------------------|-------|
   | `DISCORD_PUBLIC_KEY` | ✅ | `ab12…` | Discord portal → **General Information** → Public Key |
   | `GH_DISPATCH_TOKEN` | ✅ | `github_pat_…` | The fine-grained PAT from above |
   | `GH_OWNER` | — | `hedarthy` | Repo owner (default already correct) |
   | `GH_REPO` | — | `DealerFlow` | Repo name (default already correct) |
   | `GH_WORKFLOW` | — | `ticker-gex-run.yml` | Workflow file (default) |
   | `GH_REF` | — | `main` | Branch the workflow runs on |
   | `TICKER_GEX_CHANNEL_ID` | — | `123…` | Restrict `/gex` to one channel |

4. Deploy. Note the function URL: `https://<your-app>.vercel.app/api/interactions`.

## Point Discord at it

1. Discord developer portal → your app → **General Information** → **Interactions Endpoint
   URL** → paste `https://<your-app>.vercel.app/api/interactions` → **Save**. Discord sends a
   signed PING; if the function verifies and replies `PONG`, the URL is accepted.
2. Make sure the `/gex` command is registered. If you already ran the gateway bot once, it
   is. Otherwise register it without any host:
   ```bash
   # from repo root, with the bot token + app id available (e.g. in .env)
   DISCORD_APP_ID=1512703597283115078 DISCORD_GUILD_ID=<your-server-id> \
     python vercel/register_commands.py     # instant for one server
   ```
3. In Discord, run `/gex TSLA`. You'll get an instant ephemeral “Queued **TSLA**…”, and the
   5 dealerflow images post to the webhook channel ~1 minute later (Actions runner spin-up +
   render).

---

## Notes & tradeoffs

- **Latency:** expect ~30–60s from command to images (GitHub runner cold start + render),
  versus ~5–15s for the always-on gateway bot. The webhook posts the result, not the
  function.
- **3-second ACK:** the function dispatches the workflow synchronously, then ACKs. The
  dispatch is a single fast API call; if GitHub is ever slow, Discord may show “interaction
  failed” even though the run still starts.
- **Gateway bot vs this:** they are mutually exclusive — once an Interactions Endpoint URL
  is set, Discord delivers interactions over HTTP and the gateway bot stops receiving them.
  Pick one. Keep `ticker_gex/bot.py` if you'd rather host a worker (see `ticker_gex/README.md`).
- **Security:** every request is Ed25519-verified against `DISCORD_PUBLIC_KEY`; unsigned or
  tampered requests get `401`, and signed requests older than 5 minutes are rejected as
  replays. The PAT and public key live only in Vercel env vars.

## Test locally

```bash
python vercel/test_interactions.py   # offline: signature matrix + routing, no network
```
