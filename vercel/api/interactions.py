"""Discord Interactions endpoint (Vercel serverless) that turns ``/gex <ticker>`` into a
GitHub Actions ``workflow_dispatch``.

Why this exists
---------------
A Discord slash command needs *something* that is reachable to (a) acknowledge within 3
seconds and (b) prove it owns the app via an Ed25519 signature. GitHub Actions can do
neither directly — it has no inbound HTTP receiver and a runner can't ACK in 3s. This tiny
function is that "front door": it wakes per request (scales to zero, no host to babysit),
verifies Discord's signature, and fires the ``ticker-gex-run`` workflow with the ticker.
The workflow then renders and posts the 5 dealerflow images to the configured webhook.

Set the function URL as the app's **Interactions Endpoint URL** in the Discord developer
portal. Doing so routes interactions here over HTTP instead of the gateway, so the
always-on bot is no longer required.

Environment variables (set in the Vercel project):
  DISCORD_PUBLIC_KEY   (required) app's public key (portal -> General Information)
  GH_DISPATCH_TOKEN    (required) fine-grained PAT with Actions: read/write on the repo
  GH_OWNER             (optional) repo owner   (default: hedarthy)
  GH_REPO              (optional) repo name    (default: Options-daytrade-screener)
  GH_WORKFLOW          (optional) workflow file(default: ticker-gex-run.yml)
  GH_REF               (optional) branch to run on (default: main; must be a branch where
                                  the workflow exists, and the file must also be on the
                                  default branch for the dispatch API to find it)
  TICKER_GEX_CHANNEL_ID(optional) restrict /gex to one channel id
"""
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler

import requests
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

# Discord interaction + response type numbers.
PING = 1
APPLICATION_COMMAND = 2
PONG = 1
CHANNEL_MESSAGE_WITH_SOURCE = 4
EPHEMERAL = 64

_TICKER_RE = re.compile(r"^[A-Z][A-Z.]{0,5}$")


def normalize_ticker(raw):
    """Uppercase/trim and validate a ticker. Returns (symbol, error_message)."""
    if not raw or not str(raw).strip():
        return None, "Please supply a ticker, e.g. `/gex TSLA`."
    sym = str(raw).strip().upper()
    if not _TICKER_RE.match(sym):
        return None, f"`{sym}` doesn't look like a ticker (use 1-6 letters, e.g. SPY, QQQ, BRK.B)."
    return sym, None


def verify_signature(public_key_hex, signature_hex, timestamp, body_bytes):
    """True iff Discord's Ed25519 signature over (timestamp + body) is valid."""
    if not (public_key_hex and signature_hex and timestamp):
        return False
    try:
        VerifyKey(bytes.fromhex(public_key_hex)).verify(
            timestamp.encode() + body_bytes, bytes.fromhex(signature_hex))
        return True
    except (BadSignatureError, ValueError):
        return False


def timestamp_is_fresh(timestamp, max_age=300, now=None):
    """Reject replays: the signed timestamp must be within max_age seconds of now."""
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    return abs(current - ts) <= max_age


def dispatch_workflow(ticker):
    """Fire the ticker-gex-run workflow_dispatch. Returns (ok, detail)."""
    token = os.environ.get("GH_DISPATCH_TOKEN")
    if not token:
        return False, "server missing GH_DISPATCH_TOKEN"
    owner = os.environ.get("GH_OWNER", "hedarthy")
    repo = os.environ.get("GH_REPO", "Options-daytrade-screener")
    workflow = os.environ.get("GH_WORKFLOW", "ticker-gex-run.yml")
    ref = os.environ.get("GH_REF", "main")
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": ref, "inputs": {"ticker": ticker}},
            # Hard, short budget: we must still ACK Discord within its 3s window, so never
            # let a slow GitHub call run long. (connect, read) seconds.
            timeout=(1.0, 1.5),
        )
    except requests.RequestException as e:
        return False, f"dispatch request failed: {e}"
    if resp.status_code == 204:
        return True, "queued"
    # 404 here almost always means the workflow file isn't on the default branch yet.
    return False, f"GitHub returned {resp.status_code}: {resp.text[:200]}"


def _ephemeral(content):
    return {"type": CHANNEL_MESSAGE_WITH_SOURCE, "data": {"content": content, "flags": EPHEMERAL}}


def _command_ticker(payload):
    """Pull the `ticker` option out of an application-command interaction payload."""
    for opt in (payload.get("data") or {}).get("options") or []:
        if opt.get("name") == "ticker":
            return opt.get("value")
    return None


def build_interaction_response(payload):
    """Pure router: given a verified+parsed interaction, return the Discord response dict."""
    itype = payload.get("type")
    if itype == PING:
        return {"type": PONG}
    if itype != APPLICATION_COMMAND:
        return _ephemeral("Unsupported interaction.")

    channel_id = os.environ.get("TICKER_GEX_CHANNEL_ID", "").strip()
    if channel_id and str(payload.get("channel_id")) != channel_id:
        return _ephemeral(f"Please use <#{channel_id}> for `/gex`.")

    sym, err = normalize_ticker(_command_ticker(payload))
    if err:
        return _ephemeral(f"\u274c {err}")

    ok, detail = dispatch_workflow(sym)
    if ok:
        return _ephemeral(
            f"\u2705 Queued **{sym}** \u2014 the gamma/vanna/charm dealerflow will post to the "
            f"dealerflow channel in ~1 minute.")
    return _ephemeral(f"\u274c Couldn't start the **{sym}** render ({detail}).")


class handler(BaseHTTPRequestHandler):
    def _write(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        public_key = os.environ.get("DISCORD_PUBLIC_KEY", "")
        signature = self.headers.get("X-Signature-Ed25519", "")
        timestamp = self.headers.get("X-Signature-Timestamp", "")

        if not verify_signature(public_key, signature, timestamp, body):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"invalid request signature")
            return

        if not timestamp_is_fresh(timestamp):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"stale request")
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._write(400, {"error": "bad request"})
            return

        self._write(200, build_interaction_response(payload))

    def do_GET(self):
        # Friendly health check; Discord only ever POSTs here.
        self._write(200, {"status": "ok", "service": "ticker-gex discord interactions"})
