#!/usr/bin/env python3
"""Regenerate docs/decisions-index.md from the headings of docs/decisions.md (one row per D-number)."""
import os, re
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
s = open(os.path.join(R, "docs", "decisions.md"), encoding="utf-8").read()
rows = [(m.group(1), m.group(2).strip(), m.group(3) or "") for m in re.finditer(r"^## (D\d+)\. (.+?)(?:\(([0-9-]+)\))?\s*$", s, re.M)]
out = "# 决定索引\n\n[decisions.md](decisions.md) 只追加;这张表由 `ops/decisions-index.py` 从它的标题生成,方便按编号找。\n\n| 编号 | 一句话 | 日期 |\n| --- | --- | --- |\n" + "".join(f"| {n} | {t} | {d} |\n" for n, t, d in rows)
open(os.path.join(R, "docs", "decisions-index.md"), "w", encoding="utf-8").write(out)
print(f"{len(rows)} decisions")
