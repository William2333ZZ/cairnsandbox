# Fix the build and the program

`make` in this directory should build the binary `stats` from `src/main.c` and `src/stats.c`, but the build fails.
Once it builds, the program is also wrong: it reads whitespace-separated integers from stdin and must print exactly
one line `count=N sum=S max=M` (N = how many numbers, S = their sum, M = the largest; for no input print
`count=0 sum=0 max=0`). Currently the sum and the max are wrong for some inputs (think about negative numbers).

Fix `Makefile` and the files under `src/` only. Do not rename the target or the binary. `make` must exit 0 and
`make clean` must keep working.
