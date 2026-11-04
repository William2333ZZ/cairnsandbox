#!/usr/bin/env bash
# Produce report.txt from logs/access.log (see prompt.md). Usage: bin/report.sh [LOG] [OUT]
set -euo pipefail
LOG="${1:-logs/access.log}"; OUT="${2:-report.txt}"
{
  echo "total_requests $(wc -l < "$LOG" | tr -d ' ')"
  echo "error_requests $(awk '$9 >= 500 {n++} END {print n+0}' "$LOG")"
  awk '{c[$1]++} END {for (k in c) print c[k], k}' "$LOG" | sort -k1,1nr -k2,2 | head -1 | awk '{print "top_ip", $2, $1}'
  awk '{c[$7]++} END {for (k in c) print c[k], k}' "$LOG" | sort -k1,1nr -k2,2 | head -1 | awk '{print "top_path", $2, $1}'
  echo "bytes_total $(awk '{s += $10} END {print s+0}' "$LOG")"
} > "$OUT"
