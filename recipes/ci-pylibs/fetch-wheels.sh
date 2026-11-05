#!/bin/bash
# Download the wheels for recipe.sh on a machine WITH network access (the build host usually has none): Linux x86_64, CPython 3.13, binary wheels only.
#   recipes/ci-pylibs/fetch-wheels.sh        -> recipes/ci-pylibs/files/{wheels,requirements.txt}  (about 110 MB; gitignored)
# Needs pip >= 21 on any OS. The packages and their licences are third-party: they are downloaded from PyPI at the versions it serves today, so two runs can differ.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); F="$HERE/files"; mkdir -p "$F/wheels"; cp "$HERE/requirements.txt" "$F/requirements.txt"
python3 -m pip download -q -r "$HERE/requirements.txt" --dest "$F/wheels" --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform any \
  --python-version 3.13 --implementation cp --abi cp313 --only-binary=:all:
echo "$(ls "$F/wheels" | wc -l) wheels, $(du -sh "$F/wheels" | cut -f1) in $F/wheels"
