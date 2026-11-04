# Implement `intervals.py`

Create the module `intervals.py` (a skeleton with docstrings is in the workspace) with three functions over closed
integer intervals given as 2-element lists or tuples `[start, end]`:

- `merge(intervals)` -> list of `[start, end]` lists, sorted by start, with overlapping **or touching** intervals
  merged (`[1,3]` and `[3,5]` -> `[1,5]`). The input must not be modified. Any interval with `start > end` raises
  `ValueError`. Empty input -> `[]`.
- `total_length(intervals)` -> integer sum of `end - start` over the merged intervals.
- `gaps(intervals, lo, hi)` -> the parts of `[lo, hi]` not covered by any interval, as a sorted list of `[start, end]`
  with `start < end` (never zero-length). `lo > hi` raises `ValueError`.

Hidden tests use randomly generated inputs, so solve the general problem. Only edit `intervals.py`.
