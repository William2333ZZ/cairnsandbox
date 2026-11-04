"""LazyFS read/chunk math against an in-process chunk store (no FUSE mount needed)."""
import hashlib, json, os, sys, tempfile, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "node"))
import pytest
fuse = pytest.importorskip("fuse")
import imgstore, lazyfs
from http.server import ThreadingHTTPServer


@pytest.fixture(scope="module")
def store():
    d = tempfile.mkdtemp(); img = os.path.join(d, "img.bin")
    data = bytes(range(256)) * 4096 * 3 + b"\x00" * (1 << 20) + b"tail"      # 3 MiB pattern, 1 MiB zeros, 4 bytes
    open(img, "wb").write(data)
    imgstore.push(d, "img.bin", img, 1)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = srv.server_address[1]; srv.server_close()
    t = threading.Thread(target=imgstore.serve, args=(d, port, 0), daemon=True); t.start()
    import time, urllib.request
    for _ in range(50):
        try: urllib.request.urlopen(f"http://127.0.0.1:{port}/index.json"); break
        except Exception: time.sleep(0.05)
    return f"http://127.0.0.1:{port}", data


def test_reads_match_source_and_fetch_only_touched_chunks(store):
    url, data = store
    fs = lazyfs.LazyFS(url, tempfile.mkdtemp(), readahead=0)
    assert fs.getattr("/img.bin")["st_size"] == len(data)
    assert fs.read("/img.bin", 10, 0, None) == data[:10]
    assert fs.read("/img.bin", 100, (1 << 20) - 50, None) == data[(1 << 20) - 50:(1 << 20) + 50]   # straddles a chunk edge
    assert fs.read("/img.bin", 4096, len(data) - 4, None) == b"tail"                                   # past-EOF read is clipped
    # chunks 0..2 hold the same repeating pattern, so they share one hash: touching 0 and 1 fetches ONE object (dedup)
    assert fs.stats["chunks_fetched"] == 2 and sorted(fs.touched["img.bin"]) == [0, 1, 4]
    fs.read("/img.bin", 1, 0, None)
    assert fs.stats["chunks_fetched"] == 2 and fs.stats["chunks_hit"] >= 2                           # cache hits, no refetch


def test_dedup_of_zero_chunks(store):
    url, data = store
    m = json.loads(__import__("urllib.request").request.urlopen(f"{url}/images/img.bin.json").read())
    assert len(m["chunks"]) == 5 and m["chunks"][3] == hashlib.sha256(b"\x00" * (1 << 20)).hexdigest()
