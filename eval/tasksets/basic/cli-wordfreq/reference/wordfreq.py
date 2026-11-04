#!/usr/bin/env python3
"""wordfreq.py [-n N] [--min-len L] FILE: print the N most frequent words as `word count` lines."""
import argparse, collections, re, sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=10); ap.add_argument("--min-len", type=int, default=1); ap.add_argument("file")
    a = ap.parse_args()
    try:
        text = open(a.file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        print(f"error: {e}", file=sys.stderr); return 1
    words = [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= a.min_len]
    counts = collections.Counter(words)
    for w, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:a.n]:
        print(f"{w} {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
