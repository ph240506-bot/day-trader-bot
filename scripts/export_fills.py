"""Export every filled order (entries AND exits) from Alpaca to journal/fills.csv.

Run on the Pi:  docker compose exec -T bot python - < scripts/export_fills.py
Optional: DAYS=45 to change how far back it goes (default 45).
"""
import csv
import os
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient

from bot.config import Config

cfg = Config()
client = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=cfg.paper,
                       url_override=os.getenv("ALPACA_URL_OVERRIDE") or None)
after = (datetime.now(timezone.utc) - timedelta(days=int(os.getenv("DAYS", "45")))).isoformat()

rows, token = [], None
while True:
    params = {"after": after, "direction": "asc", "page_size": 100}
    if token:
        params["page_token"] = token
    page = client.get("/account/activities/FILL", params)
    if not page:
        break
    rows.extend(page)
    if len(page) < 100:
        break
    token = page[-1]["id"]

out_dir = os.path.dirname(cfg.journal_path) or "."
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, "fills.csv")
fields = ["transaction_time", "symbol", "side", "qty", "price", "cum_qty", "leaves_qty", "type", "order_id"]
with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
print(f"{len(rows)} fills saved to {out}")
