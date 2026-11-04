#!/usr/bin/env python3
"""csv2json.py IN OUT: convert a CSV file to a JSON array of objects (see prompt.md for the typing rules)."""
import csv, json, re, sys

INT_RE = re.compile(r"^[+-]?\d+$"); FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def convert(v):
    v = v.strip()
    if v == "": return None
    if v.lower() in ("true", "false"): return v.lower() == "true"
    if INT_RE.match(v): return int(v)
    if FLOAT_RE.match(v): return float(v)
    return v


def main(src, dst):
    with open(src, newline="", encoding="utf-8") as f:
        rows = [{k.strip(): convert(v if v is not None else "") for k, v in r.items()} for r in csv.DictReader(f)]
    rows.sort(key=lambda r: r["id"])
    with open(dst, "w", encoding="utf-8") as f: json.dump(rows, f, indent=2)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
