#!/usr/bin/env python3
"""Lazy image filesystem (FUSE): exposes every image in a chunk store as a read-only file; chunks are fetched
from the store on first access into a content-addressed local cache (shared by all images and VMs) and served
from there afterwards. Readahead fetches the next chunks; a per-image boot profile can be prefetched.
Stand-in for DSec's on-demand loading (EROFS over 3FS / OverlayBD over ublk) on a host without ublk/nbd.

  lazyfs.py <store_url> <cache_dir> <mountpoint> [--readahead N] [--cache-max-mib N]
Control files inside the mount: /.stats.json, /.profile/<image> (chunk indices touched so far)
"""
import errno, json, os, stat, sys, threading, time, urllib.request
from fuse import FUSE, Operations, FuseOSError


class LazyFS(Operations):
    def __init__(self, store, cache, readahead=4, cache_max_mib=4096):
        self.store, self.cache, self.ra = store.rstrip("/"), cache, readahead
        self.cache_max = cache_max_mib << 20
        os.makedirs(f"{cache}/chunks", exist_ok=True)
        threading.Thread(target=self._evict_loop, daemon=True).start()
        self.images = {}; self.touched = {}; self.locks = {}; self.glock = threading.Lock()
        self.stats = {"chunks_fetched": 0, "bytes_fetched": 0, "chunks_hit": 0, "fetch_s": 0.0, "reads": 0}
        self.refresh()

    # ---- store access ----
    def _get(self, path):
        with urllib.request.urlopen(f"{self.store}{path}", timeout=120) as r: return r.read()

    def refresh(self):
        for name in json.loads(self._get("/index.json"))["images"]:
            if name not in self.images:
                self.images[name] = json.loads(self._get(f"/images/{name}.json")); self.touched[name] = set()

    def _lock(self, h):
        with self.glock: return self.locks.setdefault(h, threading.Lock())

    def _ensure(self, h):
        p = f"{self.cache}/chunks/{h}"
        if os.path.exists(p): return p
        with self._lock(h):
            if os.path.exists(p): return p
            t = time.monotonic(); data = self._get(f"/chunks/{h}")
            with open(p + ".tmp", "wb") as f: f.write(data)
            os.rename(p + ".tmp", p)
            self.stats["chunks_fetched"] += 1; self.stats["bytes_fetched"] += len(data); self.stats["fetch_s"] += time.monotonic() - t
        return p

    def _evict_loop(self):
        """Keep the cache under cache_max: drop least-recently-read chunks (atime) down to 90% of the cap."""
        while True:
            time.sleep(10)
            try:
                d = f"{self.cache}/chunks"; ents = []
                for n in os.listdir(d):
                    try: st = os.stat(f"{d}/{n}"); ents.append((st.st_atime, st.st_size, n))
                    except FileNotFoundError: pass
                total = sum(e[1] for e in ents)
                if total <= self.cache_max: continue
                ents.sort()
                for atime, size, n in ents:
                    if total <= self.cache_max * 0.9: break
                    try: os.unlink(f"{d}/{n}"); total -= size; self.stats["evicted"] = self.stats.get("evicted", 0) + 1
                    except FileNotFoundError: pass
            except Exception:  # noqa: BLE001
                pass

    def prefetch(self, name, indices=None):
        m = self.images[name]; idx = indices if indices is not None else m.get("profile", [])
        def run():
            for i in idx: self._ensure(m["chunks"][i])
        th = threading.Thread(target=run, daemon=True); th.start(); return th

    # ---- FUSE ops ----
    def getattr(self, path, fh=None):
        now = time.time(); base = {"st_atime": now, "st_mtime": now, "st_ctime": now, "st_uid": 0, "st_gid": 0}
        if path in ("/", "/.profile"): return {**base, "st_mode": stat.S_IFDIR | 0o755, "st_nlink": 2}
        if path == "/.stats.json": return {**base, "st_mode": stat.S_IFREG | 0o444, "st_nlink": 1, "st_size": len(self._stats_json())}
        if path.startswith("/.profile/"):
            n = path[len("/.profile/"):]
            if n in self.images: return {**base, "st_mode": stat.S_IFREG | 0o444, "st_nlink": 1, "st_size": len(self._profile_json(n))}
        n = path[1:]
        if n in self.images:   # mtime is content-derived, so (size, mtime) identifies the image across remounts and nodes
            return {**base, "st_mtime": self._digest_time(n), "st_ctime": self._digest_time(n),
                    "st_mode": stat.S_IFREG | 0o444, "st_nlink": 1, "st_size": self.images[n]["size"]}
        raise FuseOSError(errno.ENOENT)

    def _digest_time(self, n):
        import hashlib
        return int(hashlib.sha1("".join(self.images[n]["chunks"]).encode()).hexdigest()[:8], 16)

    def readdir(self, path, fh):
        if path == "/": return [".", "..", ".stats.json", ".profile", *self.images]
        if path == "/.profile": return [".", "..", *self.images]
        raise FuseOSError(errno.ENOENT)

    # control files are padded to a fixed size: FUSE reports st_size in getattr, and a size that changes
    # between getattr and read hands the reader a truncated JSON document
    STATS_LEN, PROFILE_LEN = 1024, 256 << 10
    def _stats_json(self):
        return (json.dumps({**self.stats, "cached_chunks": len(os.listdir(f"{self.cache}/chunks"))}) + "\n").encode().ljust(self.STATS_LEN)
    def _profile_json(self, n): return (json.dumps(sorted(self.touched[n])) + "\n").encode().ljust(self.PROFILE_LEN)

    def read(self, path, size, offset, fh):
        if path == "/.stats.json": return self._stats_json()[offset:offset + size]
        if path.startswith("/.profile/"): return self._profile_json(path[len("/.profile/"):])[offset:offset + size]
        n = path[1:]; m = self.images.get(n)
        if not m: raise FuseOSError(errno.ENOENT)
        cs, out, pos, end = m["chunk_size"], [], offset, min(offset + size, m["size"]); self.stats["reads"] += 1
        while pos < end:
            i = pos // cs; h = m["chunks"][i]; self.touched[n].add(i)
            p = f"{self.cache}/chunks/{h}"
            if os.path.exists(p): self.stats["chunks_hit"] += 1
            else:
                self._ensure(h)
                for j in range(i + 1, min(i + 1 + self.ra, len(m["chunks"]))):      # readahead
                    threading.Thread(target=self._ensure, args=(m["chunks"][j],), daemon=True).start()
            off = pos - i * cs; take = min(cs - off, end - pos)
            with open(p, "rb") as f: f.seek(off); out.append(f.read(take))
            pos += take
        return b"".join(out)


def main():
    store, cache, mnt = sys.argv[1:4]; opts = dict(zip(sys.argv[4::2], sys.argv[5::2]))
    ra = int(opts.get("--readahead", 4)); cap = int(opts.get("--cache-max-mib", os.environ.get("LAZY_CACHE_MAX_MIB", 4096)))
    os.makedirs(mnt, exist_ok=True)
    fs = LazyFS(store, cache, ra, cap)
    print(f"lazyfs: {len(fs.images)} images from {store}, cache {cache}, mount {mnt}", flush=True)
    FUSE(fs, mnt, foreground=True, ro=True, allow_other=True, nothreads=False, big_writes=False, max_read=1 << 20)


if __name__ == "__main__":
    main()
