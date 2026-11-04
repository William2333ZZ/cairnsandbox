#!/usr/bin/env python3
"""Build summary.json from data/orders.csv (see prompt.md)."""
import csv, json, sys

src = sys.argv[1] if len(sys.argv) > 1 else "data/orders.csv"
dst = sys.argv[2] if len(sys.argv) > 2 else "summary.json"
latest = {}
for r in csv.DictReader(open(src, newline="")):
    cur = latest.get(r["order_id"])
    if cur is None or r["updated_at"] > cur["updated_at"]: latest[r["order_id"]] = r
out = {}
for r in latest.values():
    if r["status"] == "cancelled": continue
    c = out.setdefault(r["customer"], {"orders": 0, "total": 0.0})
    c["orders"] += 1; c["total"] += float(r["amount"])
for c in out.values(): c["total"] = round(c["total"], 2)
json.dump(out, open(dst, "w"), indent=2, sort_keys=True)
