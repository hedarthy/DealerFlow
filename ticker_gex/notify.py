"""Discord webhook poster for the ticker alert (vendored, self-contained).

``send_discord(content, png_path, webhook_url)`` posts to an explicit webhook URL when
given one, otherwise resolves it from the environment (``TICKER_GEX_DISCORD_WEBHOOK``,
then the generic ``DISCORD_WEBHOOK_URL``). Raises under ``GITHUB_ACTIONS`` when no
webhook can be resolved so a CI run turns red instead of silently delivering nothing;
stays soft (warn + no-op) for local runs. Images are attached via multipart.
"""
import json
import os
import requests


def resolve_webhook(webhook_url=None):
    """The webhook to post to: the explicit arg, else the env, else ``None``."""
    return (
        webhook_url
        or os.getenv("TICKER_GEX_DISCORD_WEBHOOK")
        or os.getenv("DISCORD_WEBHOOK_URL")
    )


def send_discord(content, png_path=None, webhook_url=None):
    """Post ``content`` (and optionally an image) to a Discord webhook.

    Text-only posts return HTTP 204; image posts return 200. Raises on a non-2xx
    response (callers add retry/backoff).
    """
    webhook = resolve_webhook(webhook_url)
    if not webhook:
        if os.getenv("GITHUB_ACTIONS"):
            raise RuntimeError("No webhook resolved in CI; cannot post alerts")
        print("WARNING: no Discord webhook resolved; skipping Discord post")
        return
    content = (content or "")[:2000]
    if png_path and os.path.exists(png_path):
        with open(png_path, "rb") as fh:
            # Attach the image to the same message via multipart payload_json.
            r = requests.post(
                webhook,
                data={"payload_json": json.dumps({"content": content})},
                files={"file": (os.path.basename(png_path), fh, "image/png")},
                timeout=30,
            )
    else:
        r = requests.post(webhook, json={"content": content}, timeout=15)
    print(f"Discord post: HTTP {r.status_code}")
    r.raise_for_status()
