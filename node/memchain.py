#!/usr/bin/env python3
"""Guest-memory snapshot chains.

Firecracker's Diff snapshots write only the pages dirtied since the last snapshot/load into a sparse file.
A VM's memory is therefore a *chain*: a resumable first file (a Diff taken since boot, or a Full file)
followed by zero or more Diff layers, each overlaying the previous. To load a VM from a chain longer
than one, the layers are materialized into one file: sparse copy of the first, then every data extent
of each later layer written on top (what Firecracker's `snapshot-editor edit-memory rebase` does).
"""
import os, shutil, subprocess

SEEK_DATA, SEEK_HOLE = 3, 4


def data_extents(path):
    """[(offset, length)] of the allocated (non-hole) ranges of a sparse file."""
    out = []; size = os.path.getsize(path); fd = os.open(path, os.O_RDONLY)
    try:
        pos = 0
        while pos < size:
            try: start = os.lseek(fd, pos, SEEK_DATA)
            except OSError: break                                   # ENXIO: no more data
            end = os.lseek(fd, start, SEEK_HOLE)
            out.append((start, end - start)); pos = end
    finally: os.close(fd)
    return out


def allocated_bytes(path):
    return os.stat(path).st_blocks * 512


def apply_layer(base_fd, layer, bufsize=1 << 20):
    """Write every data extent of `layer` into the open file `base_fd` at the same offsets."""
    n = 0
    with open(layer, "rb") as lf:
        for off, length in data_extents(layer):
            lf.seek(off); os.lseek(base_fd, off, os.SEEK_SET); left = length
            while left:
                buf = lf.read(min(bufsize, left)); os.write(base_fd, buf); left -= len(buf); n += len(buf)
    return n


def materialize(chain, out):
    """Merge chain[0] + chain[1:] into `out` (sparse). Returns bytes written by the overlay step."""
    if os.path.exists(out): os.unlink(out)
    if shutil.which("cp"): subprocess.check_call(["cp", "--sparse=always", chain[0], out])
    else: shutil.copyfile(chain[0], out)
    fd = os.open(out, os.O_WRONLY)
    try:
        n = sum(apply_layer(fd, layer) for layer in chain[1:])
        os.fsync(fd)
    finally: os.close(fd)
    return n


def describe(chain):
    return [{"file": os.path.basename(p), "allocated_mib": round(allocated_bytes(p) / 1048576, 1) if os.path.exists(p) else None} for p in chain]
