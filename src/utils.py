import json, requests, os

DATA_FILE = "previous_close.json"


def load_previous_close():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return None


def save_current_close(results):
    data = [{"contract": r[0], "key_levels": r[3]} for r in results]
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


def send_discord(content, png_path=None):
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("⚠️  DISCORD_WEBHOOK_URL not set; skipping Discord post")
        return
    content = (content or "")[:2000]
    try:
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
    except requests.RequestException as e:
        print(f"⚠️  Discord post failed: {e}")
