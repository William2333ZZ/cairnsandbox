import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "node"))
import memchain

PAGE = 4096

def _sparse(path, size, pages):
    """pages: {index: byte} -> file with only those pages allocated."""
    with open(path, "wb") as f:
        f.truncate(size)
        for i, b in pages.items(): f.seek(i * PAGE); f.write(bytes([b]) * PAGE)

def test_data_extents_finds_only_allocated_ranges(tmp_path):
    p = str(tmp_path / "d.bin"); _sparse(p, 16 * PAGE, {1: 1, 2: 1, 9: 2})
    ext = memchain.data_extents(p)
    covered = set()
    for off, ln in ext: covered.update(range(off // PAGE, (off + ln) // PAGE))
    assert {1, 2, 9} <= covered and 5 not in covered and 15 not in covered
    assert memchain.allocated_bytes(p) < 16 * PAGE

def test_materialize_overlays_layers_in_order(tmp_path):
    base, l1, l2, out = (str(tmp_path / n) for n in ("base", "l1", "l2", "out"))
    _sparse(base, 8 * PAGE, {0: 0xAA, 1: 0xAA, 2: 0xAA, 7: 0xAA})
    _sparse(l1, 8 * PAGE, {1: 0xBB, 4: 0xBB})
    _sparse(l2, 8 * PAGE, {4: 0xCC})
    memchain.materialize([base, l1, l2], out)
    data = open(out, "rb").read()
    assert len(data) == 8 * PAGE
    pg = lambda i: data[i * PAGE]
    assert pg(0) == 0xAA and pg(7) == 0xAA          # untouched base pages survive
    assert pg(1) == 0xBB                            # l1 overrides base
    assert pg(4) == 0xCC                            # l2 overrides l1
    assert pg(3) == 0 and pg(5) == 0                # holes read as zero
    assert memchain.allocated_bytes(out) <= 6 * PAGE + PAGE   # still sparse (fs may round up a block)

def test_materialize_single_file_is_a_copy(tmp_path):
    base, out = str(tmp_path / "b"), str(tmp_path / "o"); _sparse(base, 4 * PAGE, {2: 7})
    assert memchain.materialize([base], out) == 0
    assert open(out, "rb").read() == open(base, "rb").read()
