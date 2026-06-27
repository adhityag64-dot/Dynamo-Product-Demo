import os, csv
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

with open("line_items.csv", newline="") as f:
    rows = list(csv.DictReader(f))

for r in rows:
    supabase.table("line_items").insert({
        "creative_id":   r["creative_id"],
        "creative_name": r["creative_name"],
        "city":          r["city"],
        "latitude":      float(r["latitude"]),
        "longitude":     float(r["longitude"]),
        "state":         r["state"],
        "bid":           float(r["bid_inr"]),
        "daily_budget":  float(r["daily_budget_inr"]),
        "spend_today":   0,
        "override":      "none",
        "current_reason":"seeded from CSV",
    }).execute()

print(f"Loaded {len(rows)} line items")