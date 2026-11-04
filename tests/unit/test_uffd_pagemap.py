import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "node"))
from uffd_handler import PageMap, PAGE

def _sparse(path, size, pages):
    with open(path, "wb") as f:
        f.truncate(size)
        for i, b in pages.items(): f.seek(i * PAGE); f.write(bytes([b]) * PAGE)

def test_runs_group_pages_by_topmost_layer(tmp_path):
    base, l1 = str(tmp_path / "b"), str(tmp_path / "l1")
    _sparse(base, 8 * PAGE, {0: 1, 1: 1, 2: 1}); _sparse(l1, 8 * PAGE, {2: 2, 3: 2})
    pm = PageMap([base, l1])
    assert pm.pages == 8
    assert pm.runs(0, 8) == [(0, 2, 0), (2, 2, 1), (4, 4, None)]    # base, then l1 overrides page 2, then holes

def test_populated_pages_are_skipped_and_removed_pages_become_zero(tmp_path):
    base = str(tmp_path / "b"); _sparse(base, 4 * PAGE, {0: 1, 1: 1, 2: 1, 3: 1})
    pm = PageMap([base])
    pm.mark(1, 1)
    assert pm.runs(0, 4) == [(0, 1, 0), (2, 2, 0)]
    pm.remove(2, 1)                       # balloon gave page 2 back: it must read as zero from now on
    assert pm.runs(0, 4) == [(0, 1, 0), (2, 1, None), (3, 1, 0)]
