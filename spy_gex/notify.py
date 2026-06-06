"""Discord webhook poster for the SPY alert (vendored, self-contained).

Reads ``DISCORD_WEBHOOK_URL`` from the environment (the workflow maps the
SPY-specific secret into it). Raises in CI when unset so a scheduled run turns red
instead of silently delivering nothing; stays soft for local runs.
"""
import json
import os
import requests


def send_discord(content, png_path=None):
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        if os.getenv("GITHUB_ACTIONS"):
            raise RuntimeError("DISCORD_WEBHOOK_URL is not set in CI; cannot post alerts")
        print("⚠️  DISCORD_WEBHOOK_URL not set; skipping Discord post")
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
