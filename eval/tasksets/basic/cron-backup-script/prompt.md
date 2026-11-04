# Write a rotating backup script

Create `bin/rotate.sh` (bash; it will be run from cron, so it must validate its inputs and never depend on cwd):

    bin/rotate.sh SRC DEST KEEP

- Wrong number of arguments -> print a usage line to stderr, exit 2.
- `SRC` not an existing directory -> message on stderr, exit 3.
- `KEEP` not a positive integer (`0`, `-1`, `abc`, `` are all invalid) -> message on stderr, exit 4.
- Otherwise create `DEST` if needed and write a gzip tar archive of the *contents* of `SRC` (i.e. `tar -C SRC ...`,
  so a file `SRC/a.txt` is stored as `./a.txt` or `a.txt`) named `DEST/backup-NNNN.tar.gz`, where `NNNN` is a
  zero-padded 4-digit sequence number: one more than the highest existing `backup-NNNN.tar.gz` in `DEST`, starting
  at `0001`. Then delete the oldest archives (lowest numbers) so that at most `KEEP` remain. Print the path of the
  archive just created on stdout and exit 0. Other files in `DEST` must not be touched.

Only create files under `bin/`.
