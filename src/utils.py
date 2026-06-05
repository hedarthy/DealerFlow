import json, requests, os

DATA_FILE = "previous_close.json"


def load_previous_close():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return None


def save_current_close(results):
    data = [{"contract": r[0], "key_levels": r[2]} for r in results]
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


def send_discord(report, png_path=None):
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("⚠️  DISCORD_WEBHOOK_URL not set; skipping Discord post")
        return
    try:
        r = requests.post(webhook, json={"content": report[:2000]}, timeout=15)
        print(f"Discord text post: HTTP {r.status_code}")
        r.raise_for_status()
        if png_path and os.path.exists(png_path):
            with open(png_path, "rb") as fh:
                r2 = requests.post(webhook, files={"file": fh}, timeout=30)
                print(f"Discord image post: HTTP {r2.status_code}")
                r2.raise_for_status()
    except requests.RequestException as e:
        print(f"⚠️  Discord post failed: {e}")
