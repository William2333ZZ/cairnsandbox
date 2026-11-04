"""Closed integer intervals [start, end]. See prompt.md for the spec."""


def merge(intervals):
    """Merge overlapping or touching intervals; returns sorted list of [start, end]."""
    raise NotImplementedError


def total_length(intervals):
    """Sum of (end - start) over the merged intervals."""
    raise NotImplementedError


def gaps(intervals, lo, hi):
    """Sub-intervals of [lo, hi] not covered by any interval, as sorted [start, end] with start < end."""
    raise NotImplementedError
