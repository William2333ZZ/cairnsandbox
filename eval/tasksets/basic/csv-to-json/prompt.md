# Write `csv2json.py`

Create `csv2json.py` (python3 stdlib) used as `python3 csv2json.py IN.csv OUT.json`. The first CSV row is the header;
every following row becomes one JSON object (keys = header names with surrounding whitespace stripped). Rules:

- Fields may be quoted and may contain commas and newlines (standard CSV quoting).
- Values: strip surrounding whitespace; empty -> `null`; `true`/`false` (any case) -> booleans; integers (optional
  sign) -> JSON integers; decimals / scientific notation -> JSON numbers; everything else -> string unchanged.
- Every CSV has an integer `id` column; the output array is sorted by `id` ascending.
- `OUT.json` must be valid JSON (an array); formatting does not matter.

`data/sample.csv` is an example. Hidden tests generate random CSV files, so solve the general case.
