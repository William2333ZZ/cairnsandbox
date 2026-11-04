#!/usr/bin/env python3
"""Userfaultfd page-fault handler: serve a VM's memory on demand straight from a snapshot chain.

With Firecracker's `Uffd` memory backend the VMM maps guest memory anonymously and hands us the userfaultfd
plus the region layout over a unix socket (one JSON message, fd via SCM_RIGHTS, nothing afterwards). Each
missing-page fault is answered by UFFDIO_COPY from the topmost chain layer that has data at that offset, or
UFFDIO_ZEROPAGE when no layer does. A chain is therefore never materialized into a merged file.

Faults are served in blocks (default 2 MiB): one fault populates the whole unpopulated part of its block, so a
guest touching 100 MiB costs ~50 round trips instead of 25k. Pages the balloon gives back (UFFD_EVENT_REMOVE,
from MADV_DONTNEED) are remembered and served as zero pages afterwards.
"""
import array, ctypes, errno, fcntl, json, os, select, socket, struct, threading, time
from bisect import bisect_right

import memchain

PAGE = 4096
UFFD_EVENT_PAGEFAULT, UFFD_EVENT_REMOVE, UFFD_EVENT_UNMAP = 0x12, 0x15, 0x16
UFFDIO_COPY, UFFDIO_ZEROPAGE = 0xC028AA03, 0xC020AA04         # _IOWR(0xAA, 3, uffdio_copy[40]) / _IOWR(0xAA, 4, uffdio_zeropage[32])
HOLE = 0xFF


class PageMap:
    """Which chain layer owns each page (top layer wins); pure bookkeeping, testable without a VM."""
    def __init__(self, chain, size_bytes=None):
        self.chain = list(chain); size = size_bytes or max(os.path.getsize(p) for p in chain)
        self.pages = (size + PAGE - 1) // PAGE
        self.src = bytearray([HOLE]) * self.pages
        for i, layer in enumerate(self.chain):          # bottom-up: later layers overwrite
            for off, length in memchain.data_extents(layer):
                a, b = off // PAGE, min((off + length + PAGE - 1) // PAGE, self.pages)
                self.src[a:b] = bytes([i]) * (b - a)
        self.populated = bytearray(self.pages); self.zeroed = bytearray(self.pages)

    def runs(self, start, n):
        """Unpopulated pages in [start, start+n) grouped into maximal runs with the same source: [(page, count, layer|None)]."""
        out = []; p = start; end = min(start + n, self.pages)
        while p < end:
            if self.populated[p]: p += 1; continue
            s = None if self.zeroed[p] or self.src[p] == HOLE else self.src[p]; q = p + 1
            while q < end and not self.populated[q] and (None if self.zeroed[q] or self.src[q] == HOLE else self.src[q]) == s: q += 1
            out.append((p, q - p, s)); p = q
        return out

    def mark(self, start, n, populated=True):
        self.populated[start:start + n] = bytes([1 if populated else 0]) * n

    def remove(self, start, n):
        self.populated[start:start + n] = bytes(n); self.zeroed[start:start + n] = b"\x01" * n


class ChainServer(threading.Thread):
    def __init__(self, chain, sock_path, mem_bytes, owner=None, block=2 << 20, accept_timeout=30.0):
        super().__init__(daemon=True)
        self.chain, self.sock_path, self.owner, self.block = chain, sock_path, owner, block
        self.map = PageMap(chain, mem_bytes); self.fds = [os.open(p, os.O_RDONLY) for p in chain]
        self.buf = ctypes.create_string_buffer(block); self.buf_addr = ctypes.addressof(self.buf)
        self.stats = {"faults": 0, "pages_copied": 0, "pages_zero": 0, "removes": 0, "pages_removed": 0, "eagain": 0, "eexist": 0, "serve_s": 0.0}
        self.stop_flag = False; self.error = None; self.mappings = None; self.uffd = None
        if os.path.exists(sock_path): os.unlink(sock_path)
        self.ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.ls.bind(sock_path); self.ls.listen(1)
        self.ls.settimeout(accept_timeout)
        if owner: os.chown(sock_path, owner[0], owner[1])
        os.chmod(sock_path, 0o600 if owner else 0o700)

    # ---- handshake ----
    def _handshake(self):
        conn, _ = self.ls.accept(); conn.settimeout(10)
        msg, anc, _, _ = conn.recvmsg(4096, socket.CMSG_SPACE(4))
        fds = array.array("i")
        for lvl, typ, data in anc:
            if lvl == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS: fds.frombytes(data[:len(data) - len(data) % 4])
        if not fds: raise RuntimeError("firecracker sent no userfaultfd")
        self.uffd = fds[0]; self.mappings = json.loads(msg.decode())
        self.mappings.sort(key=lambda m: m["offset"]); self.conn = conn
        self.region_starts = [m["offset"] for m in self.mappings]

    def _region_for_addr(self, addr):
        for m in self.mappings:
            if m["base_host_virt_addr"] <= addr < m["base_host_virt_addr"] + m["size"]: return m
        raise RuntimeError(f"fault outside any region: {addr:#x}")

    # ---- serving ----
    def _ioctl(self, req, packed):
        buf = bytearray(packed); fcntl.ioctl(self.uffd, req, buf, True); return buf

    def _copy(self, dst, src, length):
        return self._ioctl(UFFDIO_COPY, struct.pack("QQQQq", dst, src, length, 0, 0))

    def _zero(self, dst, length):
        return self._ioctl(UFFDIO_ZEROPAGE, struct.pack("QQQq", dst, length, 0, 0))

    def _serve_block(self, region, page):
        """Populate the unpopulated remainder of the block around `page` (file-offset page index)."""
        r0 = region["offset"] // PAGE; rn = region["size"] // PAGE; bp = self.block // PAGE
        bstart = max(r0, (page // bp) * bp); bend = min(r0 + rn, bstart + bp)
        for p, n, src in self.map.runs(bstart, bend - bstart):
            dst = region["base_host_virt_addr"] + (p - r0) * PAGE
            try:
                if src is None:
                    self._zero(dst, n * PAGE); self.stats["pages_zero"] += n
                else:
                    got = os.preadv(self.fds[src], [memoryview(self.buf)[:n * PAGE]], p * PAGE)
                    if got < n * PAGE: ctypes.memset(self.buf_addr + got, 0, n * PAGE - got)
                    self._copy(dst, self.buf_addr, n * PAGE); self.stats["pages_copied"] += n
                self.map.mark(p, n)
            except OSError as e:
                if e.errno == errno.EEXIST: self.map.mark(p, n); self.stats["eexist"] += 1     # someone populated it meanwhile
                elif e.errno == errno.EAGAIN: self.stats["eagain"] += 1; return False           # a remove landed: re-read events, retry
                else: raise
        return True

    def _handle(self, msg):
        ev = msg[0]
        if ev == UFFD_EVENT_PAGEFAULT:
            flags, addr = struct.unpack_from("QQ", msg, 8); self.stats["faults"] += 1
            region = self._region_for_addr(addr); page = (region["offset"] + addr - region["base_host_virt_addr"]) // PAGE
            for _ in range(16):
                if self._serve_block(region, page): break
                self._drain_events()
        elif ev == UFFD_EVENT_REMOVE or ev == UFFD_EVENT_UNMAP:
            start, end = struct.unpack_from("QQ", msg, 8); self.stats["removes"] += 1
            for m in self.mappings:
                a = max(start, m["base_host_virt_addr"]); b = min(end, m["base_host_virt_addr"] + m["size"])
                if a < b:
                    p0 = (m["offset"] + a - m["base_host_virt_addr"]) // PAGE; n = (b - a) // PAGE
                    self.map.remove(p0, n); self.stats["pages_removed"] += n

    def _drain_events(self):
        while True:
            r, _, _ = select.select([self.uffd], [], [], 0)
            if not r: return
            try: msg = os.read(self.uffd, 32)
            except BlockingIOError: return
            if len(msg) == 32 and msg[0] != UFFD_EVENT_PAGEFAULT: self._handle(msg)
            elif len(msg) == 32: self._pending.append(msg)

    def run(self):
        try:
            self._handshake(); self._pending = []
            while not self.stop_flag:
                if self._pending: msg = self._pending.pop(0)
                else:
                    r, _, _ = select.select([self.uffd], [], [], 0.5)
                    if not r: continue
                    try: msg = os.read(self.uffd, 32)
                    except BlockingIOError: continue
                    if len(msg) != 32: break
                t0 = time.monotonic(); self._handle(msg); self.stats["serve_s"] += time.monotonic() - t0
        except Exception as e:  # noqa: BLE001
            self.error = repr(e)
        finally:
            self.close()

    def stop(self): self.stop_flag = True

    def close(self):
        for fd in self.fds:
            try: os.close(fd)
            except OSError: pass
        self.fds = []
        try: self.ls.close()
        except OSError: pass
        if self.uffd is not None:
            try: os.close(self.uffd)
            except OSError: pass
            self.uffd = None
        try: os.unlink(self.sock_path)
        except OSError: pass

    def summary(self):
        return {**self.stats, "serve_s": round(self.stats["serve_s"], 3), "layers": len(self.chain), "error": self.error,
                "populated_mib": round(sum(self.map.populated) * PAGE / 1048576, 1)}
