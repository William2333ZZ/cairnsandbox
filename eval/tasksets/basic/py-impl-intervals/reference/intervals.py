"""Closed integer intervals [start, end]. See prompt.md for the spec."""


def _check(intervals):
    out = []
    for iv in intervals:
        s, e = iv
        if s > e: raise ValueError(f"invalid interval {iv!r}")
        out.append((s, e))
    return out


def merge(intervals):
    """Merge overlapping or touching intervals; returns sorted list of [start, end]."""
    out = []
    for s, e in sorted(_check(intervals)):
        if out and s <= out[-1][1]: out[-1][1] = max(out[-1][1], e)
        else: out.append([s, e])
    return out


def total_length(intervals):
    """Sum of (end - start) over the merged intervals."""
    return sum(e - s for s, e in merge(intervals))


def gaps(intervals, lo, hi):
    """Sub-intervals of [lo, hi] not covered by any interval, as sorted [start, end] with start < end."""
    if lo > hi: raise ValueError("lo > hi")
    out, cur = [], lo
    for s, e in merge(intervals):
        if e < lo or s > hi: continue
        if s > cur: out.append([cur, s])
        cur = max(cur, e)
    if cur < hi: out.append([cur, hi])
    return out
