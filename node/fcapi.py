#!/usr/bin/env python3
"""Firecracker node-side control (v2): layered disks, snapshot/restore/clone, health watch.

Each VM lives in its own directory with relative paths:
  base.ext4  -> symlink to the shared read-only base layer (never copied)
  upper.ext4 -> this VM's writable layer (sparse copy of an empty template, or of a snapshot source)
  api.sock / v.sock / serial.log
Guest fc-init mounts overlay(lower=base, upper=upper) as root.

usage: fcapi.py demo <vmlinux> <images-dir> [--clones N] [--vcpu N] [--mem MiB]
"""
import argparse, hashlib, http.client, json, os, shutil, socket, subprocess, sys, threading, time, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sbxclient import Guest, wait_for_guest  # noqa: E402
import memchain, uffd_handler  # noqa: E402

UFFD = os.environ.get("UFFD", "1") == "1"   # chains longer than one file are served on demand by a userfaultfd handler

MAX_OWN_LAYERS = 3      # a paused VM keeps at most this many of its own Diff layers before they are squashed

BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off quiet init=/sbin/fc-init"


class UDSHTTP(http.client.HTTPConnection):
    def __init__(self, path): super().__init__("localhost"); self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.sock.connect(self.path)


class VM:
    def __init__(self, name, kernel, base, upper_src, vcpu=2, mem=512, root="/tmp/vms", qos="normal",
                 layers=(), pmem_base=False, balloon=True, jail=None, track_dirty=True, disk_mib=None, volumes=()):
        """jail = {"uid", "gid", "base_dir", "cgroup_parent", "images_dir", "layers_dir"}: run under the Firecracker
        jailer (chroot + unprivileged uid + cgroup limits). Inside the jail the chroot root is `/`, so the image
        directories are bind-mounted read-only into it and all API paths are chroot-relative."""
        self.name, self.kernel, self.vcpu, self.mem, self.qos = name, kernel, vcpu, mem, qos
        self.kernel_src = kernel
        self.layers, self.pmem_base, self.balloon, self.jail = list(layers), pmem_base, balloon, jail
        self.volumes = [(p, bool(ro)) for p, ro in volumes]   # (host path of an ext4 image, read-only): extra virtio-block drives after the layers (docs/002 沙箱侧 5 Volume)
        self.track_dirty = track_dirty      # dirty-page tracking makes snapshots Diff (only pages written since boot/load)
        self.mem_chain = []                 # memory layers the running guest was loaded from (see memchain.py)
        self.boot_mode = None; self.snap_s = None; self.merge_s = None; self.quiesced = False; self.uffd = None
        self.dir = f"{jail['base_dir']}/firecracker/{name}/root" if jail else f"{root}/{name}"
        os.makedirs(self.dir, exist_ok=True)
        t0 = time.monotonic()
        if jail:
            self._bind(jail["images_dir"], f"{self.dir}/images"); self._bind(jail["layers_dir"], f"{self.dir}/layers")
            self.kernel = "/images/" + os.path.relpath(kernel, jail["images_dir"])
            self.base_path = "/images/" + os.path.relpath(base, jail["images_dir"])
            self.layer_paths = ["/layers/" + os.path.basename(lp) for lp in self.layers]
        else:
            self.base_path, self.layer_paths = "base.ext4", [f"layer{i}.ext4" for i in range(len(self.layers))]
            if not os.path.lexists(f"{self.dir}/base.ext4"):
                os.symlink(os.path.abspath(base), f"{self.dir}/base.ext4")
            for i, lp in enumerate(self.layers):
                if not os.path.lexists(f"{self.dir}/layer{i}.ext4"): os.symlink(os.path.abspath(lp), f"{self.dir}/layer{i}.ext4")
        self.base_src = base
        self.volume_paths = []
        for i, (vp, ro) in enumerate(self.volumes):             # a hardlink inside the VM dir (the chroot when jailed): same inode, writes are shared
            os.makedirs(f"{self.dir}/vol", exist_ok=True); local = f"{self.dir}/vol/{i}-{os.path.basename(vp)}"
            if not os.path.exists(local): os.link(vp, local)
            if jail and not ro: os.chown(local, jail["uid"], jail["gid"])
            self.volume_paths.append(f"/vol/{os.path.basename(local)}" if jail else local)
        if upper_src and not os.path.exists(f"{self.dir}/upper.ext4"):
            subprocess.check_call(["cp", "--sparse=always", "--reflink=auto", upper_src, f"{self.dir}/upper.ext4"])
            if disk_mib and disk_mib << 20 > os.path.getsize(f"{self.dir}/upper.ext4"):      # bigger writable layer: grow the sparse file + fs offline
                subprocess.check_call(["truncate", "-s", f"{disk_mib}M", f"{self.dir}/upper.ext4"])
                subprocess.run(["resize2fs", f"{self.dir}/upper.ext4"], check=True, capture_output=True)
        if jail: self._own(f"{self.dir}/upper.ext4")
        self.create_s = time.monotonic() - t0
        self.proc = None; self.guest = None; self.status = "created"; self._watch = None
        self.lock = threading.RLock(); self.net = None       # netmgr.VMNet, attached by the node agent

    # -- jail helpers --
    def _bind(self, src, dst):
        os.makedirs(dst, exist_ok=True)
        if not os.path.ismount(dst):
            subprocess.check_call(["mount", "--bind", "-o", "ro", src, dst])
            subprocess.check_call(["mount", "-o", "remount,bind,ro", dst])
    def _own(self, path):
        if self.jail and os.path.exists(path): os.chown(path, self.jail["uid"], self.jail["gid"])
    def sock_api(self): return f"{self.dir}/run/firecracker.socket" if self.jail else f"{self.dir}/api.sock"
    def p(self, rel):
        """host path -> path as Firecracker sees it (chroot-relative when jailed)."""
        return f"/{rel}" if self.jail else rel
    @staticmethod
    def _unmount_all(path):
        """os.path.ismount misses bind mounts on the same filesystem; ask the kernel instead."""
        for _ in range(8):
            if subprocess.run(["mountpoint", "-q", path]).returncode != 0: return
            subprocess.run(["umount", "-l", path], capture_output=True)

    def cleanup(self, log=None):
        # jailed layout is <base>/firecracker/<id>/root; remove <base>/firecracker/<id> (ONE level up), never the shared parent
        target = os.path.dirname(self.dir) if self.jail else self.dir
        assert os.path.basename(target) == self.name, f"refusing to remove {target} for vm {self.name}"
        parent = os.path.dirname(target)
        before = sorted(os.listdir(parent)) if os.path.isdir(parent) else []
        if self.jail:
            for d in ("images", "layers"): self._unmount_all(f"{self.dir}/{d}")
            try: os.rmdir(f"/sys/fs/cgroup/{self.jail['cgroup_parent']}/{self.name}")   # jailer leaves its cgroup behind
            except OSError: pass
        mounts_left = [l.split()[4] for l in open("/proc/self/mountinfo") if target in l]
        shutil.rmtree(target, ignore_errors=True)
        after = sorted(os.listdir(parent)) if os.path.isdir(parent) else []
        if log: log(op="cleanup", vm=self.name, target=target, mounts_left=mounts_left, siblings_before=before, siblings_after=after)

    # -- process --
    def _start_process(self):
        for sock in ("api.sock", "v.sock", "run/firecracker.socket"):   # stale sockets from a killed process block the bind
            try: os.unlink(f"{self.dir}/{sock}")
            except FileNotFoundError: pass
        self.log = open(f"{self.dir}/serial.log", "ab")
        if self.jail:
            j = self.jail
            for leftover in ("dev", "run", "firecracker", "firecracker.pid", "old_root"):   # jailer recreates these
                lp = f"{self.dir}/{leftover}"
                shutil.rmtree(lp, ignore_errors=True) if os.path.isdir(lp) else (os.path.exists(lp) and os.unlink(lp))
            cmd = ["jailer", "--id", self.name, "--exec-file", shutil.which("firecracker"), "--uid", str(j["uid"]),
                   "--gid", str(j["gid"]), "--chroot-base-dir", j["base_dir"], "--cgroup-version", "2",
                   "--parent-cgroup", j["cgroup_parent"],
                   "--cgroup", f"memory.max={(self.mem + 256) * 1048576}",       # guest RAM + VMM overhead
                   "--cgroup", f"cpu.max={self.vcpu * 100000} 100000",           # vcpu count worth of CPU
                   "--cgroup", "pids.max=256",
                   "--", "--api-sock", "/run/firecracker.socket"]
        else:
            cmd = ["firecracker", "--api-sock", "api.sock"]
        self.proc = subprocess.Popen(cmd, cwd=self.dir, stdout=self.log, stderr=subprocess.STDOUT)
        self.core_sched = None
        if self.qos == "be":   # best-effort: yields CPU to anything else runnable (DSec §5.2)
            os.sched_setscheduler(self.proc.pid, os.SCHED_IDLE, os.sched_param(0))
        elif self.qos == "ls": # latency-sensitive: own core-scheduling cookie, so BE work never shares its SMT sibling
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            PR_SCHED_CORE, PR_SCHED_CORE_CREATE, PIDTYPE_TGID = 62, 1, 1
            rc = libc.prctl(PR_SCHED_CORE, PR_SCHED_CORE_CREATE, self.proc.pid, PIDTYPE_TGID, 0)
            self.core_sched = "ok" if rc == 0 else f"errno {ctypes.get_errno()}"
        for _ in range(500):
            if os.path.exists(self.sock_api()): return
            if self.proc.poll() is not None: raise RuntimeError(f"vmm exited early rc={self.proc.returncode}; see {self.dir}/serial.log")
            time.sleep(0.01)
        raise RuntimeError("api socket never appeared")

    def api(self, method, path, body=None):
        c = UDSHTTP(self.sock_api())
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse(); data = r.read().decode()
        if r.status >= 300: raise RuntimeError(f"{method} {path} -> {r.status}: {data}")
        return json.loads(data) if data else None

    # -- lifecycle --
    def boot(self):
        t0 = time.monotonic(); self._start_process()
        if self.pmem_base:   # base layer as virtio-pmem: guest maps it with DAX, host page cache is shared by all VMs
            self.api("PUT", "/boot-source", {"kernel_image_path": self.kernel, "boot_args": BOOT_ARGS + " rootflags=dax"})
            self.api("PUT", "/pmem/base", {"id": "base", "path_on_host": self.base_path, "root_device": True, "read_only": True})
        else:
            self.api("PUT", "/boot-source", {"kernel_image_path": self.kernel, "boot_args": BOOT_ARGS})
            self.api("PUT", "/drives/base", {"drive_id": "base", "path_on_host": self.base_path, "is_root_device": True, "is_read_only": True})
        self.api("PUT", "/drives/upper", {"drive_id": "upper", "path_on_host": self.p("upper.ext4"), "is_root_device": False, "is_read_only": False})
        for i, lp in enumerate(self.layer_paths):
            self.api("PUT", f"/drives/layer{i}", {"drive_id": f"layer{i}", "path_on_host": lp, "is_root_device": False, "is_read_only": True})
        for i, vp in enumerate(self.volume_paths):
            self.api("PUT", f"/drives/vol{i}", {"drive_id": f"vol{i}", "path_on_host": vp, "is_root_device": False, "is_read_only": self.volumes[i][1]})
        self.api("PUT", "/machine-config", {"vcpu_count": self.vcpu, "mem_size_mib": self.mem, "track_dirty_pages": self.track_dirty})
        if self.balloon:     # free-page reporting lets the guest hand idle pages back to the host (DSec §5.2)
            self.api("PUT", "/balloon", {"amount_mib": 0, "deflate_on_oom": True, "stats_polling_interval_s": 0, "free_page_reporting": True})
        self.api("PUT", "/vsock", {"guest_cid": 3, "uds_path": self.p("v.sock")})
        if self.net: self.api("PUT", "/network-interfaces/eth0", self.net.fc_iface())
        self.api("PUT", "/actions", {"action_type": "InstanceStart"})
        self.guest, _ = wait_for_guest(f"{self.dir}/v.sock"); self.status = "running"
        self.mem_chain = []; self.boot_mode = "cold"
        self._push_net(); self._start_watch()
        return time.monotonic() - t0

    def volume_devs(self):
        """[(guest block device, read_only)] in drive order: virtio-blk names devices vda, vdb, … in the order the drives were added."""
        n = (0 if self.pmem_base else 1) + 1 + len(self.layer_paths)       # base (unless pmem), upper, layers, then the volumes
        return [(f"/dev/vd{chr(ord('a') + n + i)}", ro) for i, (_, ro) in enumerate(self.volumes)]

    def _push_net(self):
        if self.net and self.guest: self.guest.call("net_config", **self.net.guest_cfg())

    def new_conn(self):
        """A fresh vsock connection: exec/session traffic uses its own connection so it never blocks lifecycle ops."""
        return Guest(f"{self.dir}/v.sock")

    def pause(self):  self.api("PATCH", "/vm", {"state": "Paused"}); self.status = "paused"
    def resume(self): self.api("PATCH", "/vm", {"state": "Resumed"}); self.status = "running"

    def snapshot(self, snap_dir, mem_file=None, full=False):
        """Pause and snapshot: device state into snap_dir/snapshot.bin, memory into mem_file (default snap_dir/mem.bin).
        Diff by default: only pages dirtied since boot / last load. snap_dir/chain.json lists the memory layers
        this snapshot sits on (self.mem_chain + the new file); a Full snapshot starts a new chain."""
        mem_file = mem_file or f"{snap_dir}/mem.bin"
        for d in (snap_dir, os.path.dirname(mem_file)): os.makedirs(d, exist_ok=True); self._own(d)
        self._stop_watch()
        if self.guest:
            try: self.quiesced = self.guest.call("quiesce")["frozen"]      # consistent writable layer in the snapshot
            except Exception: self.quiesced = False                     # noqa: BLE001
            self.guest.close(); self.guest = None
        t0 = time.monotonic(); self.pause()
        self.api("PUT", "/snapshot/create", {"snapshot_type": "Full" if full else "Diff",
                                             "snapshot_path": self._rel(f"{snap_dir}/snapshot.bin"), "mem_file_path": self._rel(mem_file)})
        for f in (f"{snap_dir}/snapshot.bin", mem_file): os.chmod(f, 0o640)     # readable by other VMs of the same gid (clones, template users)
        chain = ([] if full else list(self.mem_chain)) + [mem_file]
        json.dump(chain, open(f"{snap_dir}/chain.json", "w"))
        self.snap_s = time.monotonic() - t0; return self.snap_s

    def _stamp(self): return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"

    def _localize(self, path):
        """Memory/state files must live under the VM dir (the chroot when jailed): hardlink outside files into ext/."""
        if os.path.abspath(path).startswith(self.dir + "/"): return path
        d = f"{self.dir}/ext"; os.makedirs(d, exist_ok=True); self._own(d)
        local = f"{d}/{hashlib.sha1(path.encode()).hexdigest()[:12]}-{os.path.basename(path)}"
        if not os.path.exists(local): os.link(path, local)
        return local

    def _pinned(self):
        """Memory layers a checkpoint still needs (docs/002 沙箱侧 5: a Checkpoint is a chain prefix plus a copy of the writable layer)."""
        keep = set(); d = f"{self.dir}/ckpt"
        if os.path.isdir(d):
            for name in os.listdir(d):
                try: keep.update(json.load(open(f"{d}/{name}/chain.json")))
                except (OSError, ValueError): pass
        return keep

    def _gc_mem(self):
        """Drop own memory layers and hardlinks that neither the chain nor a checkpoint needs."""
        keep = set(self.mem_chain) | self._pinned()
        for sub in ("mem", "ext"):
            d = f"{self.dir}/{sub}"
            if os.path.isdir(d):
                for f in os.listdir(d):
                    if f"{d}/{f}" not in keep: os.unlink(f"{d}/{f}")

    def _rel(self, host_path):
        """Snapshot dirs live under self.dir; map to the chroot view when jailed."""
        return "/" + os.path.relpath(host_path, self.dir) if self.jail else host_path

    def restore(self, snap_dir, fixup=True, merge_cache=None):
        """New process in self.dir (base symlink + its own upper.ext4), load the snapshot, resume, reconnect.
        A chain longer than one file is materialized into a temporary merged file first (unlinked after the load:
        the mapping keeps it alive, the disk keeps only the layers). With merge_cache (a path next to a checkpoint) the merged file is built once and kept:
        the VMM maps it MAP_PRIVATE, so every later rollback and every clone of that checkpoint loads the same read-only file instead of merging again."""
        t0 = time.monotonic(); self._start_process(); ph = {"spawn_s": time.monotonic() - t0}; t1 = time.monotonic()
        try: chain = json.load(open(f"{snap_dir}/chain.json"))
        except OSError: chain = [f"{snap_dir}/mem.bin"]
        chain = [self._localize(p) for p in chain]; state = self._localize(f"{snap_dir}/snapshot.bin")
        merged = None; self.merge_s = 0.0; self._stop_uffd()
        if len(chain) > 1 and UFFD:        # no merge: the handler answers page faults from the layers directly
            sock = f"{self.dir}/uffd.sock"
            self.uffd = uffd_handler.ChainServer(chain, sock, self.mem * 1048576, owner=(self.jail["uid"], self.jail["gid"]) if self.jail else None)
            self.uffd.start(); backend = {"backend_type": "Uffd", "backend_path": self._rel(sock)}
        else:
            if len(chain) > 1 and merge_cache:
                if not os.path.exists(merge_cache):
                    t1 = time.monotonic(); tmp = f"{merge_cache}.{self._stamp()}.tmp"
                    memchain.materialize(chain, tmp); os.chmod(tmp, 0o640); self._own(tmp); os.rename(tmp, merge_cache); self.merge_s = time.monotonic() - t1
                use = self._localize(merge_cache)
            elif len(chain) > 1:
                t1 = time.monotonic(); merged = f"{self.dir}/merged-{self._stamp()}.bin"
                memchain.materialize(chain, merged); self._own(merged); self.merge_s = time.monotonic() - t1
            backend = {"backend_type": "File", "backend_path": self._rel(use if len(chain) > 1 and merge_cache else (merged or chain[0]))}
        load = {"snapshot_path": self._rel(state), "resume_vm": True, "track_dirty_pages": self.track_dirty, "mem_backend": backend}
        if self.net: load["network_overrides"] = [{"iface_id": "eth0", "host_dev_name": self.net.tap}]
        ph["prep_s"] = time.monotonic() - t1; t1 = time.monotonic()
        try: self.api("PUT", "/snapshot/load", load)
        except Exception:
            self._stop_uffd(); self.kill(); raise
        ph["load_s"] = time.monotonic() - t1; t1 = time.monotonic()
        if merged: os.unlink(merged)
        self.mem_chain = chain; self.boot_mode = "restore"
        self.guest, _ = wait_for_guest(f"{self.dir}/v.sock"); self.status = "running"
        ph["guest_s"] = time.monotonic() - t1; t1 = time.monotonic()
        try: self.guest.call("thaw")                                   # the snapshot was taken with the upper layer frozen
        except Exception: pass                                           # noqa: BLE001
        if fixup:   # clones must not share clock or RNG state
            self.guest.call("set_time", epoch=time.time())
            self.guest.call("add_entropy", data_b64=__import__("base64").b64encode(os.urandom(64)).decode())
        self._push_net()          # new tap / new IP after a restore
        self._start_watch()
        ph["fixup_s"] = time.monotonic() - t1; self.restore_phases = {k: round(v, 3) for k, v in ph.items()}   # where a restore's time went (spawn / chain+uffd setup / snapshot load / guest reconnect / clock+net)
        return time.monotonic() - t0

    def _stop_uffd(self):
        if self.uffd: self.uffd.stop(); self.uffd_last = self.uffd.summary(); self.uffd = None

    def apply_qos(self, qos):
        """Change the scheduling class of a running VMM (all its threads): used when a pooled VM is handed out."""
        if qos == self.qos or not self.proc or self.proc.poll() is not None: return
        pol = os.SCHED_IDLE if qos == "be" else os.SCHED_OTHER
        for tid in os.listdir(f"/proc/{self.proc.pid}/task"):
            try: os.sched_setscheduler(int(tid), pol, os.sched_param(0))
            except OSError: pass
        if qos == "ls":
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True); rc = libc.prctl(62, 1, self.proc.pid, 1, 0)
            self.core_sched = "ok" if rc == 0 else f"errno {ctypes.get_errno()}"
        self.qos = qos

    def shutdown(self, timeout=10):
        self._stop_watch()
        try:
            if self.guest: self.guest.call("shutdown")
            rc = self.proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            self.proc.kill(); rc = -9
        self._stop_uffd(); self.status = "stopped"; return rc

    def kill(self):
        self._stop_watch()
        if self.proc and self.proc.poll() is None: self.proc.kill(); self.proc.wait()
        self._stop_uffd(); self.status = "stopped"

    # -- pause/resume in place (DSec §6.3 microVM path): snapshot, kill process, restore later --
    def pause_to_disk(self):
        """Diff-snapshot into <dir>/paused/ (memory layer in <dir>/mem/) and kill the process; memory is released,
        state kept on disk. Once the VM owns more than MAX_OWN_LAYERS layers they are squashed into one file."""
        d = f"{self.dir}/paused"; layer = f"{self.dir}/mem/{self._stamp()}.bin"
        t = self.snapshot(d, mem_file=layer); self.kill()
        self.mem_chain = json.load(open(f"{d}/chain.json")); self.squashed = False
        if sum(p.startswith(f"{self.dir}/mem/") for p in self.mem_chain) > MAX_OWN_LAYERS: self._squash(d, "full"); self.squashed = True
        self.status = "paused"; return t

    def _squash(self, snap_dir, tag):
        """Fold snap_dir's chain into one sparse resumable file in mem/ and drop the layers it replaces."""
        t1 = time.monotonic(); full = f"{self.dir}/mem/{self._stamp()}-{tag}.bin"
        memchain.materialize(self.mem_chain, full); self._own(full); os.chmod(full, 0o640)
        json.dump([full], open(f"{snap_dir}/chain.json", "w")); self.mem_chain = [full]; self._gc_mem()
        self.merge_s = time.monotonic() - t1

    def resume_from_disk(self):
        d = f"{self.dir}/paused"
        if not os.path.exists(f"{d}/snapshot.bin"): raise RuntimeError("not paused")
        t = self.restore(d); shutil.rmtree(d, ignore_errors=True); return t

    # -- checkpoints: a point the sandbox can come back to, in place (docs/002 沙箱侧 5 状态; D34) --
    def snapshot_live(self, snap_dir, mem_file, copy_upper=None):
        """Snapshot the running VM (state + a Diff layer that joins the chain, optionally a sparse copy of the writable layer to `copy_upper`) and keep
        running: resume, reconnect, thaw, watch again. Returns seconds the guest was paused. Checkpoints and template commits are built on this."""
        if self.status != "running": raise RuntimeError("needs a running sandbox")
        t0 = time.monotonic()
        self.snapshot(snap_dir, mem_file=mem_file)                         # pauses, freezes the writable layer, writes state + memory
        if copy_upper:
            subprocess.check_call(["cp", "--sparse=always", f"{self.dir}/upper.ext4", copy_upper]); self._own(copy_upper); os.chmod(copy_upper, 0o640)
        self.mem_chain = json.load(open(f"{snap_dir}/chain.json"))
        self.resume(); self.guest = Guest(f"{self.dir}/v.sock")
        try: self.guest.call("thaw")
        except Exception: pass                                             # noqa: BLE001
        self._start_watch(); return time.monotonic() - t0

    def checkpoint(self, name):
        """Snapshot the running VM into <dir>/ckpt/<name>/ (device state, a Diff memory layer that joins the chain, a sparse copy of the writable layer)
        and keep running. Returns seconds the guest was paused."""
        d = f"{self.dir}/ckpt/{name}"
        if os.path.exists(d): raise FileExistsError(name)
        return self.snapshot_live(d, f"{self.dir}/mem/{self._stamp()}-ck.bin", copy_upper=f"{d}/upper.ext4")

    def rollback(self, name):
        """Back to checkpoint `name` in place: kill the process, put the checkpoint's writable layer back, load its memory. Same id, dir and tap;
        the chain becomes the checkpoint's; layers written after it stay on disk only while another checkpoint pins them."""
        d = f"{self.dir}/ckpt/{name}"
        if not os.path.exists(f"{d}/snapshot.bin"): raise KeyError(f"no checkpoint {name}")
        t0 = time.monotonic()
        if self.status == "running": self.kill()
        shutil.rmtree(f"{self.dir}/paused", ignore_errors=True)
        t1 = time.monotonic()
        subprocess.check_call(["cp", "--sparse=always", f"{d}/upper.ext4", f"{self.dir}/upper.ext4"])   # same inode: the jail's view sees the new content
        cp_s = time.monotonic() - t1
        for attempt in range(20):                                           # the dead VMM's tap is released by the kernel a moment after the process is gone
            try: self.restore(d, fixup=False, merge_cache=f"{d}/merged.bin"); break
            except Exception as e:  # noqa: BLE001
                if "Resource busy" not in str(e) or attempt == 19: raise
                time.sleep(0.1)
        self._gc_mem(); self.restore_phases = {"kill_cp_upper_s": round(cp_s, 3), **self.restore_phases}
        return time.monotonic() - t0

    def checkpoint_drop(self, name):
        shutil.rmtree(f"{self.dir}/ckpt/{name}", ignore_errors=True); self._gc_mem()

    def fork(self, names, root=None, qos="be", on_clone=None):
        """Snapshot this VM, spawn clones with their own copy of the upper disk, resume everyone.
        on_clone(vm) runs before each clone restores (used to attach its own tap)."""
        snap = f"{self.dir}/fork-snap"          # Diff, then squash: one sparse memory file every clone maps and shares
        self.snapshot(snap, mem_file=f"{self.dir}/mem/{self._stamp()}.bin"); self.kill()
        self.mem_chain = json.load(open(f"{snap}/chain.json")); self._squash(snap, "fork")
        clones = [VM(n, self.kernel_src, self.base_src, f"{self.dir}/upper.ext4",
                     self.vcpu, self.mem, root or os.path.dirname(self.dir), qos,
                     layers=self.layers, pmem_base=self.pmem_base, balloon=self.balloon,
                     jail=({**self.jail, "uid": self.jail["uid"] + 1 + i} if self.jail else None)) for i, n in enumerate(names)]
        self.restore(snap, fixup=False)
        for c in clones:
            if on_clone: on_clone(c)
            c.restore(snap)
        shutil.rmtree(snap, ignore_errors=True); self._gc_mem()   # clones hold hardlinks to the fork layer
        return clones

    def fork_from_checkpoint(self, name, names, root=None, qos="be", on_clone=None):
        """Clones started from checkpoint `name` (its memory chain and its copy of the writable layer); the parent keeps running untouched
        (docs/002 沙箱侧 5 Fork: a Fork is from a Snapshot, and a Checkpoint is one)."""
        d = f"{self.dir}/ckpt/{name}"
        if not os.path.exists(f"{d}/snapshot.bin"): raise KeyError(f"no checkpoint {name}")
        clones = [VM(n, self.kernel_src, self.base_src, f"{d}/upper.ext4",
                     self.vcpu, self.mem, root or os.path.dirname(self.dir), qos,
                     layers=self.layers, pmem_base=self.pmem_base, balloon=self.balloon,
                     jail=({**self.jail, "uid": self.jail["uid"] + 1 + i} if self.jail else None)) for i, n in enumerate(names)]
        for c in clones:
            if on_clone: on_clone(c)
            c.restore(d, merge_cache=f"{d}/merged.bin")
        return clones

    # -- health: a dedicated connection pinging; channel loss or process exit => failed (DSec aether/edge) --
    def _start_watch(self):
        self._watch_stop = False; self._watch_gen = getattr(self, "_watch_gen", 0) + 1
        self._watch = threading.Thread(target=self._watch_loop, args=(self._watch_gen, self.proc), daemon=True); self._watch.start()
    def _stop_watch(self):
        self._watch_stop = True
    def _watch_loop(self, gen, proc):
        """Only the newest watcher may declare a failure: a watcher stopped by kill() can still be inside a ping when the next restore starts a new one
        (rollback kills and restores within 150 ms), and its late error must not take the restored VM down."""
        try: h = Guest(f"{self.dir}/v.sock")
        except Exception: h = None
        def current(): return not self._watch_stop and gen == self._watch_gen
        while current():
            if proc.poll() is not None:
                if current(): self.status = f"failed(exit={proc.returncode})"; self.failed_at = time.monotonic()
                return
            try:
                if h is None: h = Guest(f"{self.dir}/v.sock")
                h.call("ping")
            except Exception:  # noqa: BLE001
                if current(): self.status = "failed(channel)"; self.failed_at = time.monotonic()
                return
            time.sleep(0.25)


def human(n): return f"{n/1048576:.1f} MiB"
def du(path): return int(subprocess.check_output(["du", "-s", "--apparent-size", "--block-size=1", path]).split()[0]), int(subprocess.check_output(["du", "-s", "--block-size=1", path]).split()[0])


def demo(a):
    base, upper_t = f"{a.images}/base.ext4", f"{a.images}/upper-template.ext4"
    print("== 1. layered boot (ro base + rw upper, overlay root) ==")
    vm = VM("base", a.kernel, base, upper_t, a.vcpu, a.mem)
    print(f"create (symlink base + sparse upper copy): {vm.create_s*1000:.1f} ms;  boot -> ready: {vm.boot():.3f}s")
    g = vm.guest
    h = g.call("health"); print("health:", {k: h[k] for k in ("root_fs", "mem_total_kb", "mem_avail_kb", "sessions")})
    print("mounts:", g.exec("findmnt -n -o SOURCE,FSTYPE,OPTIONS / ; findmnt -n -o SOURCE,FSTYPE /.oldroot 2>/dev/null; df -h / | tail -1", user="root")["stdout"].strip())

    print("== 2. hardening ==")
    r = g.exec("id; echo hi > /bin/bash && echo OVERWROTE || echo 'cannot write /bin/bash'; ls /usr/local/lib/sandboxd 2>&1 | head -1; touch /home/agent/ok && echo 'home writable'")
    print("as agent:", r["stdout"].strip().replace("\n", " | "))
    print("as root :", g.exec("id -u; echo root-ok > /root/x && cat /root/x", user="root")["stdout"].strip().replace("\n", " | "))
    print("daemon peer check: see sandboxd main() — rejects CID != 2; guest kernel has no vsock loopback (CONFIG_VSOCKETS_LOOPBACK unset)")

    print("== 3. sessions ==")
    sid = g.session_open()
    print("cwd/env persist:", g.session_exec(sid, "cd /tmp && export FOO=bar && pwd")["stdout"].strip(),
          "->", g.session_exec(sid, "pwd; echo $FOO")["stdout"].strip().replace("\n", " "))
    chunks = []; t0 = time.monotonic()
    r = g.session_exec(sid, "for i in 1 2 3; do echo tick$i; sleep 0.3; done; echo done", stream=True,
                       on_event=lambda ev: chunks.append((round(time.monotonic() - t0, 2), ev["data"].strip())))
    print(f"streamed chunks (t, data): {chunks}  exit={r['exit']}")
    r = g.session_exec(sid, "sleep 5", timeout=1); print("timeout kills session:", r["timeout"], r.get("session_closed"))
    pid = g.session_open(pty=True); time.sleep(0.3); g.session_read(pid, wait=0.5)
    g.session_write(pid, b"echo PTY-$((6*7)); tty\n"); out = g.session_read(pid, wait=0.5).decode(errors="replace")
    print("pty session:", " ".join(l for l in out.splitlines() if "PTY-" in l or "/dev/pts" in l))
    g.session_close(pid)

    print("== 4. snapshot + clones sharing the base layer ==")
    g.exec("echo 42 > /home/agent/marker")
    snap = f"{vm.dir}/snap"; t = vm.snapshot(snap); vm.kill()
    print(f"snapshot: {t:.3f}s  (mem.bin {human(os.path.getsize(snap + '/mem.bin'))})")
    clones = [VM(f"clone-{i}", a.kernel, base, f"{vm.dir}/upper.ext4", a.vcpu, a.mem, qos="be") for i in range(a.clones)]
    print(f"clone create cost: {sum(c.create_s for c in clones)/len(clones)*1000:.1f} ms avg (upper copy only; base is a symlink)")
    times = [0] * a.clones
    def w(i): times[i] = clones[i].restore(snap)
    t0 = time.monotonic(); th = [threading.Thread(target=w, args=(i,)) for i in range(a.clones)]
    [x.start() for x in th]; [x.join() for x in th]
    print(f"{a.clones} clones restored in {time.monotonic()-t0:.3f}s wall; per-clone {min(times):.3f}..{max(times):.3f}s (incl. time+entropy fixup)")
    for i, c in enumerate(clones): c.guest.exec(f"echo clone{i} > /home/agent/me; hostname clone{i}", user="root")
    seen = [c.guest.exec("cat /home/agent/marker /home/agent/me; hostname; head -c 4 /dev/urandom | od -An -tx1")["stdout"].split() for c in clones]
    print("isolation ok:", all(s[0] == "42" and s[1] == s[2] == f"clone{i}" for i, s in enumerate(seen)),
          " distinct urandom:", len({tuple(s[3:]) for s in seen}) == a.clones)
    drift = int(clones[0].guest.exec("date +%s")["stdout"]) - int(time.time())
    print(f"clock drift after restore+fixup: {drift}s")
    app, real = du(clones[0].dir)
    print(f"per-clone disk: apparent {human(app)}, allocated {human(real)} (base layer not copied)")
    rss = subprocess.run("ps -o rss= -C firecracker | awk '{s+=$1} END {print s}'", shell=True, capture_output=True, text=True).stdout.strip()
    print(f"RSS of {a.clones} firecracker: {int(rss or 0)/1024:.0f} MiB; scheduler of clone-0: {os.sched_getscheduler(clones[0].proc.pid)} (5=SCHED_IDLE)")

    print("== 5. health: crash a clone, expect failed status ==")
    c = clones[0]; t0 = time.monotonic()
    try: c.guest.exec("echo c > /proc/sysrq-trigger", user="root", timeout=2)
    except Exception as e: print("exec during crash:", type(e).__name__)
    for _ in range(80):
        if c.status.startswith("failed"): break
        time.sleep(0.05)
    print(f"status={c.status} detected after {time.monotonic()-t0:.2f}s; others still running: {[x.status for x in clones[1:]]}")
    print("shutdown rcs:", [x.shutdown() for x in clones[1:]])


def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo"); d.add_argument("kernel"); d.add_argument("images")
    d.add_argument("--clones", type=int, default=5); d.add_argument("--vcpu", type=int, default=2); d.add_argument("--mem", type=int, default=512)
    demo(ap.parse_args())


if __name__ == "__main__":
    main()
