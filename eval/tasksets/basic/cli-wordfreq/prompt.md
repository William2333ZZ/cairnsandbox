# Write `wordfreq.py`

Create a command-line script `wordfreq.py` (python3, standard library only) used as:

    python3 wordfreq.py [-n N] [--min-len L] FILE

- A *word* is a maximal run of ASCII letters `a-z` after lower-casing the text (digits, apostrophes and
  punctuation separate words). Words shorter than `L` letters (default 1) are ignored.
- Print the `N` (default 10) most frequent words, one per line, formatted exactly `word count`, ordered by count
  descending and then by word ascending. Fewer than N distinct words -> print them all; an empty file prints nothing.
- Exit 0 on success. If FILE cannot be read, print an error to stderr and exit 1 (print nothing on stdout).

`sample.txt` is an example input. Only create/edit `wordfreq.py`.
