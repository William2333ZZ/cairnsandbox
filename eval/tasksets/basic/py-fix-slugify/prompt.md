# Fix `slugify.py`

`slugify(text, max_len=50)` in `slugify.py` is supposed to turn arbitrary text into a URL slug, but several
cases are wrong. Fix the function so that it satisfies ALL of the following rules (there are hidden tests):

1. Unicode is normalised (NFKD) and non-ASCII characters are dropped (`"Crème brûlée"` -> `"creme-brulee"`).
2. The result is lower-case.
3. Every run of one or more non-alphanumeric characters becomes exactly one `-`.
4. No leading or trailing `-`.
5. The result is at most `max_len` characters; when truncating, never end with `-` (strip it after cutting).
6. Empty or symbol-only input returns `""`.

Only edit `slugify.py`. Do not change the function signature.
