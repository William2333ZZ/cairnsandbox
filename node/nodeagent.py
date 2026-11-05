#!/usr/bin/env python3
"""Resident node agent (DSec "edge" role) for Firecracker sandboxes.

HTTP/JSON on a TCP port. One process manages many VMs under STATE_DIR.
- node-local admission (max VMs, memory budget, vCPU overcommit)
- transparent resume: any op on a paused sandbox restores it first
- TTL loop: idle -> pause (persistent) / destroy (ephemeral); max lifetime -> destroy
- fork: snapshot a live sandbox into N clones that share the base layer
- append-only audit log (JSONL) on the node

Env: IMAGES_DIR, STATE_DIR, PORT, TLS_CERT / TLS_KEY (serve HTTPS), MAX_VMS, FENCE_AFTER_S (self-fence when the control plane is silent that long; 0 = off), MEM_BUDGET_MIB, VCPU_OVERCOMMIT, HOST_VCPUS
"""
import collections, glob, base64, hashlib, json, os, re, shlex, shutil, signal, socket, ssl, subprocess, sys, threading, time, urllib.error, urllib.parse, uuid
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fcapi  # noqa: E402
from fcapi import VM  # noqa: E402
import netmgr, egress_proxy, memchain, imgstore  # noqa: E402

IMAGES = os.environ.get("IMAGES_DIR", "/work/images")
STATE = os.environ.get("STATE_DIR", "/var/lib/fc-node")
PORT = int(os.environ.get("PORT", "8800"))
MAX_VMS = int(os.environ.get("MAX_VMS", "64"))
MEM_BUDGET = int(os.environ.get("MEM_BUDGET_MIB", "16384"))
VCPU_OVERCOMMIT = float(os.environ.get("VCPU_OVERCOMMIT", "4"))
HOST_VCPUS = int(os.environ.get("HOST_VCPUS", str(os.cpu_count() or 2)))
LAZY_STORE = os.environ.get("LAZY_STORE")            # e.g. http://fc-store:8900 -> images are fetched on demand
STORE = os.environ.get("STORE") or LAZY_STORE         # where paused sandboxes / template snapshots are published for other nodes
NODE_NAME = os.environ.get("NODE_NAME", os.uname().nodename)
LAZY_MNT = os.environ.get("LAZY_MNT", "/work/lazy"); LAZY_CACHE = os.environ.get("LAZY_CACHE", f"{STATE}/cache")
IMG_ROOT = LAZY_MNT if LAZY_STORE else IMAGES
TEMPLATES = {"ubuntu-24.04": {"kernel": f"{IMG_ROOT}/vmlinux-6.1.155", "base": f"{IMG_ROOT}/base.ext4" if LAZY_STORE else f"{IMAGES}/layers/base.ext4",
                              "upper": f"{IMAGES}/layers/upper-template.ext4"}}   # the writable template stays local (it is copied, not read lazily)
# more templates from images/templates/<name>.json: {"kernel": "<file under images/>", "base": "<file under images/ (or the store when lazy)>"}
for _tf in sorted(glob.glob(f"{os.environ.get('TEMPLATES_DIR', f'{IMAGES}/templates')}/*.json")):
    try:
        _t = json.load(open(_tf)); _n = os.path.splitext(os.path.basename(_tf))[0]
        TEMPLATES[_n] = {"kernel": f"{IMG_ROOT}/{os.path.basename(_t['kernel'])}" if LAZY_STORE else f"{IMAGES}/{_t['kernel']}",   # lazy mode: everything the jail sees comes from the store
                         "base": f"{IMG_ROOT}/{os.path.basename(_t['base'])}" if LAZY_STORE else f"{IMAGES}/{_t['base']}",
                         "upper": f"{IMAGES}/{_t.get('upper', 'layers/upper-template.ext4')}"}
    except (OSError, ValueError, KeyError) as _e: print(f"template {_tf}: {_e}", flush=True)

DEFAULT_TEMPLATE = os.environ.get("DEFAULT_TEMPLATE") or ("debian-13" if "debian-13" in TEMPLATES else "ubuntu-24.04")   # the Rust-runtime template when it is registered
if DEFAULT_TEMPLATE not in TEMPLATES: print(f"DEFAULT_TEMPLATE {DEFAULT_TEMPLATE} unknown, using ubuntu-24.04", flush=True); DEFAULT_TEMPLATE = "ubuntu-24.04"
LAZY_PROC = None
def start_lazyfs():
    """Mount the chunk store as a lazy filesystem; wait for it to appear."""
    import subprocess
    global LAZY_PROC
    os.makedirs(LAZY_MNT, exist_ok=True); os.makedirs(LAZY_CACHE, exist_ok=True)
    LAZY_PROC = subprocess.Popen(["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "lazyfs.py"),
                                  LAZY_STORE, LAZY_CACHE, LAZY_MNT, "--readahead", os.environ.get("LAZY_READAHEAD", "4")])
    for _ in range(100):
        if subprocess.run(["mountpoint", "-q", LAZY_MNT]).returncode == 0: return
        time.sleep(0.1)
    raise RuntimeError("lazyfs did not mount")

def lazy_stats():
    try: return json.load(open(f"{LAZY_MNT}/.stats.json"))
    except OSError: return None

os.makedirs(f"{STATE}/vms", exist_ok=True); os.makedirs(f"{STATE}/layers", exist_ok=True)
TPL_DIR = f"{STATE}/templates"; os.makedirs(TPL_DIR, exist_ok=True)
TPL_SNAPS = {}              # template-snapshot key -> meta (a ready guest, Diff-since-boot memory + its upper disk)
for _f in os.listdir(TPL_DIR):
    if _f.endswith(".json"):
        _m = json.load(open(f"{TPL_DIR}/{_f}")); TPL_SNAPS[_m["key"]] = _m
DRAINING = [False]
MAX_IDEMP = 2000
POOL = {}                   # template-snapshot key -> {"spec", "want", "ready": [Sandbox], "building": bool}
POOL_LOCK = threading.Lock()
TEMPLATE_POOL = json.loads(os.environ.get("TEMPLATE_POOL", "[]"))   # e.g. [{"mem":512,"vcpu":2,"ready":2}] warmed at start

def _node_token():
    """Bearer token for the HTTP API. From NODE_TOKEN, else generated once and kept in STATE/node-token (0600)."""
    t = os.environ.get("NODE_TOKEN"); p = f"{STATE}/node-token"
    if t: return t
    if os.path.exists(p): return open(p).read().strip()
    t = uuid.uuid4().hex + uuid.uuid4().hex
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600); os.write(fd, t.encode()); os.close(fd); return t
NODE_TOKEN = _node_token()
PUBLIC_ROUTES = {"/health"}
PMEM_BASE_DEFAULT = os.environ.get("PMEM_BASE", "0") == "1"
JAIL = os.environ.get("JAIL", "0") == "1"
JAIL_BASE = f"{STATE}/jail"; JAIL_GID = 10000; CG_PARENT = "fc"
_uid_counter = [0]
def jail_cfg(sid):
    if not JAIL: return None
    _uid_counter[0] += 1
    return {"uid": 10000 + (_uid_counter[0] * 8) % 50000, "gid": JAIL_GID, "base_dir": JAIL_BASE,
            "cgroup_parent": CG_PARENT, "images_dir": IMG_ROOT, "layers_dir": f"{STATE}/layers"}

def setup_cgroups():
    """Make the container's cgroup tree writable and delegate controllers so the jailer can create per-VM groups."""
    import subprocess
    subprocess.run(["mount", "-o", "remount,rw", "/sys/fs/cgroup"], capture_output=True)
    os.makedirs("/sys/fs/cgroup/init", exist_ok=True)
    for pid in open("/sys/fs/cgroup/cgroup.procs").read().split():
        try: open("/sys/fs/cgroup/init/cgroup.procs", "w").write(pid)
        except OSError: pass
    try: open("/sys/fs/cgroup/cgroup.subtree_control", "w").write("+cpu +memory +pids")
    except OSError as e: audit(op="cgroup_setup_failed", error=str(e))
    os.makedirs(f"/sys/fs/cgroup/{CG_PARENT}", exist_ok=True)
BALLOON_DEFAULT = os.environ.get("BALLOON", "1") == "1"
def _uffd_available():
    """A jailed (unprivileged, no capabilities) VMM can only create a userfaultfd when the host allows it."""
    if not fcapi.UFFD: return False, "disabled (UFFD=0)"
    if not JAIL: return True, "node runs the VMM as root with CAP_SYS_PTRACE"
    try: ok = open("/proc/sys/vm/unprivileged_userfaultfd").read().strip() == "1"
    except OSError: ok = False
    return ok, "ok" if ok else "jailed VMM lacks CAP_SYS_PTRACE and vm.unprivileged_userfaultfd=0: chains are merged into a file instead"
UFFD_OK, UFFD_WHY = _uffd_available(); fcapi.UFFD = UFFD_OK
VOLUMES = {}                # volume id -> {"id", "name", "project", "size_mib", "path", "created", "attached": {sandbox: "rw"|"ro"}}   (docs/002 沙箱侧 5; D35)
VOL_LOCK = threading.Lock()
os.makedirs(f"{STATE}/volumes", exist_ok=True)
for _f in os.listdir(f"{STATE}/volumes"):
    if _f.endswith(".json"):
        _m = json.load(open(f"{STATE}/volumes/{_f}")); _m["attached"] = {}; VOLUMES[_m["id"]] = _m     # attachments are rebuilt from the sandboxes on recovery
LAYERS = {}                 # layer id -> {"path", "bytes", "name", "created", "from"}
for _f in os.listdir(f"{STATE}/layers"):
    if _f.endswith(".json"):
        _m = json.load(open(f"{STATE}/layers/{_f}")); LAYERS[_m["id"]] = _m
AUDIT = open(f"{STATE}/audit.jsonl", "ab", buffering=0)
REG = {}                    # id -> Sandbox
REG_LOCK = threading.Lock()
IDEMP = {}                  # Idempotency-Key -> response (bounded, see h_create)


def audit(**ev):
    ev["ts"] = round(time.time(), 3)
    AUDIT.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())


class Sandbox:
    def __init__(self, sid, spec, vm):
        self.id, self.spec, self.vm = sid, spec, vm
        vm.guest_env = dict(spec.get("env") or {})
        self.created = time.time(); self.last_used = time.time(); self.lock = threading.RLock()
        self.boot_mode = None; self.tpl_key = None
    def touch(self): self.last_used = time.time()
    def info(self):
        return {"id": self.id, "status": self.vm.status, **{k: self.spec.get(k) for k in ("template", "class", "vcpu", "mem", "disk", "ttl_idle_s", "ttl_max_s", "layers")},
                "created": self.created, "idle_s": round(time.time() - self.last_used, 1), "dir": self.vm.dir,
                "ip": self.vm.net.ip if self.vm.net else None, "network": self.spec.get("network"),
                "layers": self.spec.get("layers", []), "pmem_base": self.spec.get("pmem_base"), "qos": self.spec.get("qos"),
                "boot_mode": self.boot_mode, "template_snapshot": self.tpl_key, "node": NODE_NAME,
                "retain_paused_s": self.spec.get("retain_paused_s", 0), "paused_at": self.spec.get("paused_at"), "env_keys": sorted(self.spec.get("env") or {}), "labels": self.spec.get("labels") or {},
                "durable": self.spec.get("durable", False), "bundle": self.spec.get("bundle"), "checkpoints": [c["name"] for c in self.spec.get("checkpoints", [])], "volumes": self.spec.get("volumes", []),
                "disk_used_mib": round(memchain.allocated_bytes(f"{self.vm.dir}/upper.ext4") / 1048576, 1) if os.path.exists(f"{self.vm.dir}/upper.ext4") else None,   # what the writable layer really holds (metering)
                "egress": egress_proxy.traffic(self.vm.net.ip) if self.vm.net else {"out": 0, "in": 0},
                "cpu_usec": cpu_usec_of(self.id) if self.vm.status == "running" and self.vm.jail else None,   # cumulative CPU time the guest used (D67: the "active CPU" billing basis)
                "mem_chain": memchain.describe(self.vm.mem_chain), "snapshot_s": self.vm.snap_s, "merge_s": self.vm.merge_s,
                "uffd": self.vm.uffd.summary() if self.vm.uffd else getattr(self.vm, "uffd_last", None),
                "jail": ({"uid": self.vm.jail["uid"], "cgroup": f"{CG_PARENT}/{self.id}", "pid": self.vm.proc.pid if self.vm.proc else None}
                         if self.vm.jail else None)}
    def ensure_running(self):
        """Transparent resume (DSec §6.3)."""
        if self.vm.status == "paused":
            t = self.vm.resume_from_disk(); audit(op="auto_resume", sandbox=self.id, s=round(t, 3))
        if not self.vm.status == "running":
            raise RuntimeError(f"sandbox is {self.vm.status}")


# ---------------- admission ----------------
def usage():
    with REG_LOCK:
        live = [s for s in REG.values() if s.vm.status == "running"]
        pooled = [s for p in POOL.values() for s in p["ready"]]       # warm, not yet handed out: they hold memory too
        return {"vms_total": len(REG) + len(pooled), "vms_running": len(live) + len(pooled), "vms_pooled": len(pooled),
                "mem_mib_running": sum(s.spec["mem"] for s in live + pooled),
                "vcpu_running": sum(s.spec["vcpu"] for s in live + pooled), "max_vms": MAX_VMS, "mem_budget_mib": MEM_BUDGET,
                "vcpu_budget": int(HOST_VCPUS * VCPU_OVERCOMMIT)}


def admit(spec, n=1):
    u = usage()
    if u["vms_total"] + n > MAX_VMS: return f"max_vms {MAX_VMS} reached"
    if u["mem_mib_running"] + spec["mem"] * n > MEM_BUDGET: return f"memory budget {MEM_BUDGET} MiB exceeded"
    if u["vcpu_running"] + spec["vcpu"] * n > u["vcpu_budget"]: return f"vcpu budget {u['vcpu_budget']} exceeded"
    return None


def new_spec(body):
    snap = TPL_SNAPS.get(str(body.get("template_key") or ""))
    if snap:     # a named template fixes the guest configuration: fields the caller leaves out are taken from it (ones it sets must still match, resolve_template_key)
        body = {**{k: snap[k] for k in ("template", "vcpu", "mem", "layers", "pmem_base", "balloon", "disk") if k in snap}, **body}
    tpl = body.get("template", DEFAULT_TEMPLATE)
    if tpl not in TEMPLATES: raise ValueError(f"unknown template {tpl}")
    cls = body.get("class", "persistent")
    if cls not in ("persistent", "ephemeral"): raise ValueError("class must be persistent|ephemeral")
    net = body.get("network") or {}
    mode = net.get("mode") or ("allowlist" if net.get("allow_hosts") else "closed")
    if mode not in egress_proxy.MODES: raise ValueError("network.mode must be closed|allowlist|audited")
    rules = egress_proxy.validate_rules(net.get("rules") or []); limits = egress_proxy.validate_limits(net.get("limits") or {})
    retain = int(body.get("retain_paused_s", 0))
    if retain < 0 or retain > 86400 * 365: raise ValueError("retain_paused_s out of range")
    labels = body.get("labels") or {}
    if not isinstance(labels, dict) or len(labels) > 64 or not all(isinstance(k, str) and 0 < len(k) <= 128 and isinstance(v, str) and len(v) <= 1024 for k, v in labels.items()):
        raise ValueError("labels: up to 64 string pairs")       # the control plane's bookkeeping (project, priority, E2B metadata): kept here so a lost registry can be rebuilt from the nodes
    env = body.get("env") or {}
    if not isinstance(env, dict) or len(env) > 64 or not all(isinstance(k, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k) and isinstance(v, str) and len(v) <= 4096 for k, v in env.items()):
        raise ValueError("env: up to 64 NAME=string pairs")
    for lid in body.get("layers", []):
        if lid not in LAYERS: raise ValueError(f"unknown layer {lid}")
    vols = []
    for v in body.get("volumes", []) or []:
        v = {"id": v} if isinstance(v, str) else dict(v)
        if v.get("id") not in VOLUMES: raise ValueError(f"unknown volume {v.get('id')}")
        mount = str(v.get("mount") or f"/mnt/{VOLUMES[v['id']]['name']}")
        if not mount.startswith("/") or ".." in mount: raise ValueError("volume mount must be an absolute path")
        vols.append({"id": v["id"], "mount": mount, "ro": bool(v.get("ro", False))})
    if len({v["id"] for v in vols}) != len(vols): raise ValueError("a volume is listed twice")
    qos = body.get("qos") or ("be" if cls == "ephemeral" else "normal")
    if qos not in ("normal", "be", "ls"): raise ValueError("qos must be normal|be|ls")
    boot = body.get("boot", "auto")
    if boot not in ("auto", "cold", "snapshot"): raise ValueError("boot must be auto|cold|snapshot")
    disk = int(body.get("disk", 512))
    if not 512 <= disk <= 16384: raise ValueError("disk must be 512..16384 MiB")
    tkey = str(body.get("template_key") or "")
    if tkey and not re.match(r"^[a-z0-9][a-z0-9_.-]{0,39}$", tkey): raise ValueError("bad template_key")
    durable = bool(body.get("durable", False))        # every pause is also published to the store (survives node loss)
    if durable and not STORE: raise ValueError("durable needs a STORE")
    return {"template": tpl, "class": cls, "vcpu": int(body.get("vcpu", 2)), "mem": int(body.get("mem", 512)),
            "ttl_idle_s": int(body.get("ttl_idle_s", 1800 if cls == "persistent" else 600)),
            "ttl_max_s": int(body.get("ttl_max_s", 0)), "retain_paused_s": retain, "env": dict(env), "labels": dict(labels), "qos": qos, "boot": boot, "durable": durable, "disk": disk,
            "layers": list(body.get("layers", [])), "pmem_base": bool(body.get("pmem_base", PMEM_BASE_DEFAULT)),
            "balloon": bool(body.get("balloon", BALLOON_DEFAULT)), "volumes": vols, **({"template_key": tkey} if tkey else {}),
            "network": {"mode": mode, "allow_hosts": list(net.get("allow_hosts", [])), "allow_direct": list(net.get("allow_direct", [])), "rules": rules, "limits": limits}}


def vm_kwargs(spec, sid):
    return {"qos": spec.get("qos", "normal"), "layers": [LAYERS[l]["path"] for l in spec.get("layers", [])], "disk_mib": spec.get("disk", 512),
            "pmem_base": spec.get("pmem_base", False), "balloon": spec.get("balloon", True), "jail": jail_cfg(sid),
            "volumes": [(VOLUMES[v["id"]]["path"], v["ro"]) for v in spec.get("volumes", []) if v["id"] in VOLUMES]}


# ---------------- images: named templates committed from a sandbox, layers pulled from OCI images (docs/002 沙箱侧 4; D37) ----------------
TPL_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
REGISTRY = os.environ.get("REGISTRY_MIRROR", "https://registry-1.docker.io").rstrip("/")

def commit_template(sb, name, note=""):
    """The sandbox's state right now (memory + writable layer) becomes template `name`; new sandboxes with template_key=name restore from it, warm."""
    if not TPL_NAME_RE.match(name): raise ValueError("template name: a letter, then letters, digits, _ . -, up to 40 chars")
    if name in TPL_SNAPS: raise FileExistsError(name)
    with sb.lock:
        sb.ensure_running(); sb.touch(); vm = sb.vm; t0 = time.monotonic()
        d = f"{TPL_DIR}/{name}"; shutil.rmtree(d, ignore_errors=True); os.makedirs(d); work = f"{vm.dir}/tplc"
        snap_s = vm.snapshot_live(work, f"{vm.dir}/mem/{vm._stamp()}-tpl.bin", copy_upper=f"{d}/upper.ext4")
        memchain.materialize(vm.mem_chain, f"{d}/mem.bin")                 # one resumable file every sandbox from this template maps and shares
        shutil.move(f"{work}/snapshot.bin", f"{d}/snapshot.bin"); shutil.rmtree(work, ignore_errors=True)
        json.dump([f"{d}/mem.bin"], open(f"{d}/chain.json", "w"))
        for f in ("mem.bin", "snapshot.bin", "upper.ext4"):
            os.chmod(f"{d}/{f}", 0o640)
            if vm.jail: os.chown(f"{d}/{f}", vm.jail["uid"], vm.jail["gid"])
    meta = {"key": name, "name": name, "dir": d, **{k: sb.spec.get(k) for k in ("template", "vcpu", "mem", "pmem_base", "balloon", "layers", "disk")},
            "from": sb.id, "note": str(note)[:200], "created": time.time(), "snapshot_s": round(snap_s, 3),
            "mem_mib": round(memchain.allocated_bytes(f"{d}/mem.bin") / 1048576, 1), "upper_mib": round(memchain.allocated_bytes(f"{d}/upper.ext4") / 1048576, 1), "s": round(time.monotonic() - t0, 3)}
    json.dump(meta, open(f"{TPL_DIR}/{name}.json", "w")); TPL_SNAPS[name] = meta
    audit(op="template_commit", **{k: v for k, v in meta.items() if k != "dir"}); return meta

def oci_ref(image):
    """registry, repository, reference. docker.io shorthand: `python:3.12` -> library/python on the mirror."""
    rest = image; reg = REGISTRY
    first = rest.split("/", 1)[0]
    if ("." in first or ":" in first or first == "localhost") and "/" in rest:
        reg, rest = "https://" + first, rest.split("/", 1)[1]
    if "@" in rest: repo, ref = rest.split("@", 1)
    elif ":" in rest.rsplit("/", 1)[-1]: repo, ref = rest.rsplit(":", 1)
    else: repo, ref = rest, "latest"
    if "/" not in repo and "docker.io" in reg or ("/" not in repo and reg == REGISTRY): repo = "library/" + repo
    return reg, repo, ref

MANIFEST_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
                  "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")

def _reg_open(url, accept=None, token=None):
    """GET from a registry; a 401 with a Bearer challenge is answered with an anonymous token (docker hub style)."""
    import urllib.request, urllib.error
    def go(tok):
        req = urllib.request.Request(url, headers={**({"Accept": ", ".join(accept)} if accept else {}), **({"Authorization": f"Bearer {tok}"} if tok else {})})
        return urllib.request.urlopen(req, timeout=120)
    try: return go(token), token
    except urllib.error.HTTPError as e:
        if e.code != 401 or token: raise
        ch = e.headers.get("WWW-Authenticate", "")
        if not ch.startswith("Bearer "): raise
        parts = dict(re.findall(r'(\w+)="([^"]*)"', ch))
        q = urllib.parse.urlencode({k: v for k, v in (("service", parts.get("service")), ("scope", parts.get("scope"))) if v})
        tok = json.load(urllib.request.urlopen(f"{parts['realm']}?{q}", timeout=60)).get("token")
        return go(tok), tok

def _apply_oci_layer(tree, blob, gz):
    """One image layer onto `tree`: whiteouts first (.wh.<name> removes <name> from below, .wh..wh..opq empties the directory), then the files."""
    import tarfile
    with tarfile.open(blob, "r:gz" if gz else "r") as tf:
        for m in tf:
            base = os.path.basename(m.name); parent = os.path.join(tree, os.path.dirname(m.name))
            if base == ".wh..wh..opq":
                for child in (os.listdir(parent) if os.path.isdir(parent) else []):
                    cp = os.path.join(parent, child)
                    shutil.rmtree(cp, ignore_errors=True) if os.path.isdir(cp) and not os.path.islink(cp) else os.unlink(cp)
            elif base.startswith(".wh."):
                victim = os.path.join(parent, base[4:])
                if os.path.isdir(victim) and not os.path.islink(victim): shutil.rmtree(victim, ignore_errors=True)
                elif os.path.lexists(victim): os.unlink(victim)
    subprocess.run(["tar", "--xattrs", "--xattrs-include=*", "--numeric-owner", "--exclude=.wh.*", "--exclude=*/.wh.*", "-C", tree, "-x" + ("z" if gz else ""), "-f", blob], check=True, capture_output=True)

# An image's root filesystem is stacked on our base, and the base wins: every file the base already has is dropped from the image's tree
# (busybox brings /bin/bash -> busybox and /etc/passwd without the agent user; either one keeps the guest from coming up). The base's file list
# comes from a throwaway sandbox (`find / -xdev`), once per base image, cached under state/.
_BASE_PATHS = {}

def _base_paths(template=None):
    """(files, dirlinks) of a template's base image: every file and symlink as a path without the leading slash, and the symlinks that point at directories
    (`bin -> usr/bin` under Debian's merged /usr) as {rel: target rel}. Listed once from a throwaway sandbox and cached under state/ by the base's signature."""
    template = template or DEFAULT_TEMPLATE; t = TEMPLATES[template]; sig = hashlib.sha1(json.dumps([_fsig(t["kernel"]), _fsig(t["base"])]).encode()).hexdigest()[:12]
    if sig in _BASE_PATHS: return _BASE_PATHS[sig]
    cache = f"{STATE}/base-paths-{sig}.v2.txt"
    def parse(lines):
        paths = set(); links = {}
        for l in lines:
            if not l: continue
            if l.startswith("@") and "\t" in l:
                rel, tgt = l[1:].split("\t", 1); tgt = tgt.lstrip("/") if tgt.startswith("/") else os.path.normpath(os.path.join(os.path.dirname(rel), tgt))
                links[rel] = tgt
            elif not l.startswith(("run/", "tmp/", "home/agent/", "root/", "var/log/")): paths.add(l)
        return paths, links
    if os.path.exists(cache):
        _BASE_PATHS[sig] = parse(open(cache).read().split("\n")); return _BASE_PATHS[sig]
    spec = new_spec({"template": template, "class": "ephemeral", "mem": 256, "vcpu": 1, "ttl_idle_s": 60, "boot": "cold", "network": {}})
    sb = create(spec, name=f"basels-{uuid.uuid4().hex[:6]}")
    try:
        r = sb.vm.guest.exec("find / -xdev \\( -type f -o -type l \\) 2>/dev/null | sed 's|^/||'; find / -xdev -type l -xtype d -printf '@%P\\t%l\\n' 2>/dev/null", user="root", timeout=120)
        lines = r.get("stdout", "").split("\n")
    finally: destroy(sb, "base_paths")
    paths, links = parse(lines)
    if not paths: raise RuntimeError("could not list the base image's files")
    open(cache, "w").write("\n".join(sorted(l for l in lines if l)))
    _BASE_PATHS[sig] = (paths, links); audit(op="base_paths", template=template, files=len(paths), dirlinks=len(links)); return _BASE_PATHS[sig]

def _tree_to_layer(tree, lid, under_base=True):
    """Pack an unpacked image tree as a layer. under_base: the layer sits on our base, so (1) a directory the image has where the base has a symlink to a directory
    (`/bin` on merged /usr) is folded into the symlink's target, else the directory would hide the whole base /usr/bin; (2) a file the base has is dropped, the base wins
    (an image's /bin/sh, /etc/passwd or libc must not replace ours, or sandboxd does not come up); (3) run/proc/sys/dev/tmp are the guest's."""
    if under_base:
        base, links = _base_paths(); dropped = moved = 0
        for rel, tgt in sorted(links.items(), key=lambda kv: -len(kv[0])):
            d = os.path.join(tree, rel)
            if not os.path.isdir(d) or os.path.islink(d): continue
            dst = os.path.join(tree, tgt); os.makedirs(dst, exist_ok=True)
            for name in os.listdir(d):
                s, t = os.path.join(d, name), os.path.join(dst, name)
                if os.path.lexists(t): shutil.rmtree(t) if os.path.isdir(t) and not os.path.islink(t) else os.unlink(t)
                shutil.move(s, t); moved += 1
            os.rmdir(d)
        for root, dirs, files in os.walk(tree):
            for f in files:
                p = os.path.join(root, f); rel = os.path.relpath(p, tree)
                if rel in base: os.unlink(p); dropped += 1
        for rel in ("run", "proc", "sys", "dev", "tmp"):
            shutil.rmtree(os.path.join(tree, rel), ignore_errors=True)
        audit(op="layer_under_base", layer=lid, dropped=dropped, folded=moved)
    used = int(subprocess.check_output(["du", "-sb", tree]).split()[0])
    img = f"{STATE}/layers/{lid}.ext4"; subprocess.check_call(["truncate", "-s", str(max(used * 2, 16 << 20) + (8 << 20)), img])
    subprocess.check_call(["mkfs.ext4", "-q", "-F", "-L", f"sbx-layer-{lid}", "-d", tree, img]); return img

def _is_gz(path):
    with open(path, "rb") as f: return f.read(2) == b"\x1f\x8b"

def layer_from_archive(data, name):
    """An image saved by `docker save` (manifest.json + layer tars) or an OCI image layout (index.json + blobs/) as one tar, uploaded as the body:
    the way to bring an image in where the node cannot reach a registry. Same unpacking as layer_from_oci."""
    import tarfile, tempfile
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{0,39}$", str(name)): raise ValueError("layer name: lowercase letters, digits, _ . -, up to 40 chars")
    t0 = time.monotonic(); lid = f"L-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(dir=f"{STATE}/layers") as tmp:
        arc = f"{tmp}/image.tar"
        with open(arc, "wb") as f: f.write(data)
        src = f"{tmp}/src"; os.makedirs(src)
        subprocess.run(["tar", "-C", src, "-xf", arc], check=True, capture_output=True)
        layers = []
        if os.path.exists(f"{src}/manifest.json"):                            # docker save
            man = json.load(open(f"{src}/manifest.json"))[0]; layers = [f"{src}/{p}" for p in man["Layers"]]; ident = man.get("RepoTags") or [man.get("Config")]
        elif os.path.exists(f"{src}/index.json"):                             # OCI layout
            idx = json.load(open(f"{src}/index.json")); d = idx["manifests"][0]["digest"]
            def blob(dg): return f"{src}/blobs/{dg.replace(':', '/')}"
            man = json.load(open(blob(d)))
            if "manifests" in man:
                pick = next((m for m in man["manifests"] if (m.get("platform") or {}).get("architecture") in (None, "amd64")), man["manifests"][0]); man = json.load(open(blob(pick["digest"])))
            layers = [blob(l["digest"]) for l in man["layers"]]; ident = [d]
        else: raise ValueError("not a docker-save or OCI-layout archive (no manifest.json or index.json)")
        tree = f"{tmp}/tree"; os.makedirs(tree)
        for l in layers: _apply_oci_layer(tree, l, _is_gz(l))
        img = _tree_to_layer(tree, lid)
    meta = {"id": lid, "name": name, "path": img, "bytes": os.path.getsize(img), "from": f"archive:{ident[0] if ident else '?'}", "oci": {"layers": len(layers), "archive_bytes": len(data)},
            "created": time.time(), "s": round(time.monotonic() - t0, 3)}
    json.dump(meta, open(f"{STATE}/layers/{lid}.json", "w")); LAYERS[lid] = meta
    audit(op="layer_from_archive", **{k: v for k, v in meta.items() if k != "path"}); return meta

BUILD_LOGS = {}      # build id -> {"lines": [...], "t": last write}; kept in memory for an hour after the last line (D64)

def blog(bid, msg):
    """One line of a build's log, with the seconds since it started. No-op without an id."""
    if not bid: return
    now = time.time(); b = BUILD_LOGS.setdefault(bid, {"lines": [], "t0": now, "t": now})
    b["lines"].append(f"[{now - b['t0']:6.1f}s] {msg}"); b["t"] = now
    if len(b["lines"]) > 500: del b["lines"][:100]
    for k in [k for k, v in BUILD_LOGS.items() if now - v["t"] > 3600]: BUILD_LOGS.pop(k, None)


def layer_from_oci(image, name, bid=None):
    """Pull an OCI image (linux/amd64) from the registry and turn its root filesystem into one of our read-only layers: whiteouts applied in order,
    the tree written into a fresh ext4 image with mkfs -d (no mounting), registered like a packed layer."""
    import tarfile, tempfile, urllib.parse
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{0,39}$", str(name)): raise ValueError("layer name: lowercase letters, digits, _ . -, up to 40 chars")
    reg, repo, ref = oci_ref(image); t0 = time.monotonic(); lid = f"L-{uuid.uuid4().hex[:8]}"
    blog(bid, f"resolving {image} on {reg}")
    r, tok = _reg_open(f"{reg}/v2/{repo}/manifests/{ref}", accept=MANIFEST_TYPES); man = json.load(r); ctype = r.headers.get("Content-Type", "")
    if "manifests" in man:                                                   # an index: pick linux/amd64
        pick = next((m for m in man["manifests"] if (m.get("platform") or {}).get("os") == "linux" and (m.get("platform") or {}).get("architecture") == "amd64"), None)
        if not pick: raise ValueError("no linux/amd64 manifest in the image index")
        r, tok = _reg_open(f"{reg}/v2/{repo}/manifests/{pick['digest']}", accept=MANIFEST_TYPES, token=tok); man = json.load(r)
    layers = man.get("layers") or []
    if not layers: raise ValueError("manifest has no layers")
    blog(bid, f"manifest: {len(layers)} layers")
    with tempfile.TemporaryDirectory(dir=f"{STATE}/layers") as tmp:
        tree = f"{tmp}/tree"; os.makedirs(tree); fetched = 0
        for i, l in enumerate(layers):
            blob = f"{tmp}/{i}.tar"; r, tok = _reg_open(f"{reg}/v2/{repo}/blobs/{l['digest']}", token=tok)
            with open(blob, "wb") as f:
                for chunk in iter(lambda: r.read(1 << 20), b""): f.write(chunk); fetched += len(chunk)
            blog(bid, f"layer {i + 1}/{len(layers)} {l['digest'][7:19]} downloaded ({fetched / 1048576:.1f} MiB so far)")
            _apply_oci_layer(tree, blob, "gzip" in l.get("mediaType", "") or _is_gz(blob)); os.unlink(blob)
            blog(bid, f"layer {i + 1}/{len(layers)} applied")
        blog(bid, "writing the ext4 image (mkfs -d, no mounting)")
        img = _tree_to_layer(tree, lid)
    meta = {"id": lid, "name": name, "path": img, "bytes": os.path.getsize(img), "from": image, "oci": {"registry": reg, "repository": repo, "reference": ref, "config": (man.get("config") or {}).get("digest"), "layers": len(layers), "fetched_bytes": fetched},
            "created": time.time(), "s": round(time.monotonic() - t0, 3)}
    json.dump(meta, open(f"{STATE}/layers/{lid}.json", "w")); LAYERS[lid] = meta
    blog(bid, f"done: {lid} {meta['bytes'] / 1048576:.1f} MiB in {meta['s']} s")
    audit(op="layer_from_oci", **{k: v for k, v in meta.items() if k != "path"}); return meta


# ---------------- volumes: ext4 images with a life of their own, attached as extra block devices (docs/002 沙箱侧 5; D35) ----------------
def _vol_save(v): json.dump({k: x for k, x in v.items() if k != "attached"}, open(f"{STATE}/volumes/{v['id']}.json", "w"))

def volume_create(body, content=None):
    """A new ext4 image; with `content` (a tar's bytes) the filesystem is populated from it with mkfs -d, without mounting (a harness directory, a cache)."""
    import tempfile
    name = str(body.get("name") or "")
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{0,39}$", name): raise ValueError("volume name: lowercase letters, digits, _ . -, up to 40 chars")
    size = int(body.get("size_mib", 1024))
    if not 64 <= size <= 65536: raise ValueError("size_mib must be 64..65536")
    vid = str(body.get("id") or f"vol-{uuid.uuid4().hex[:8]}")
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{0,47}$", vid): raise ValueError("bad volume id")
    with VOL_LOCK:
        if vid in VOLUMES: raise FileExistsError(vid)
        path = f"{STATE}/volumes/{vid}.ext4"
        subprocess.check_call(["truncate", "-s", f"{size}M", path])
        if content:
            with tempfile.TemporaryDirectory(dir=f"{STATE}/volumes") as tmp:
                tree = f"{tmp}/tree"; os.makedirs(tree); arc = f"{tmp}/c.tar"
                with open(arc, "wb") as f: f.write(content)
                subprocess.run(["tar", "--no-same-owner", "-C", tree, "-xf", arc], check=True, capture_output=True)
                subprocess.run(["chown", "-R", "2000:2000", tree], check=True, capture_output=True)          # the agent user's files
                subprocess.run(["mkfs.ext4", "-q", "-F", "-L", name[:16], "-d", tree, path], check=True, capture_output=True)
        else:
            subprocess.run(["mkfs.ext4", "-q", "-F", "-L", name[:16], path], check=True, capture_output=True)
        os.chmod(path, 0o644)                                                   # readable by any jail uid (read-only attach); a writer gets chown at attach
        v = {"id": vid, "name": name, "project": str(body.get("project") or "default"), "size_mib": size, "path": path, "created": time.time(), "attached": {},
             "content_bytes": len(content) if content else 0}
        VOLUMES[vid] = v; _vol_save(v)
    audit(op="volume_create", volume=vid, name=name, size_mib=size, content_bytes=len(content) if content else 0); return v

def volume_info(v):
    return {**{k: x for k, x in v.items() if k != "path"}, "allocated_mib": round(memchain.allocated_bytes(v["path"]) / 1048576, 1) if os.path.exists(v["path"]) else None,
            "attached": dict(v["attached"])}

def volume_attach(spec, sid):
    """Bookkeeping before a sandbox with volumes starts: a writer is exclusive; readers may share, but not with a writer."""
    with VOL_LOCK:
        for s in spec.get("volumes", []):
            v = VOLUMES[s["id"]]; others = {k: m for k, m in v["attached"].items() if k != sid}
            if not s["ro"] and others: raise PermissionError(f"volume {s['id']} is attached to {sorted(others)}; a writer must be alone")
            if s["ro"] and "rw" in others.values(): raise PermissionError(f"volume {s['id']} has a writer; cannot attach read-only")
        for s in spec.get("volumes", []): VOLUMES[s["id"]]["attached"][sid] = "ro" if s["ro"] else "rw"

def volume_detach(sid):
    with VOL_LOCK:
        for v in VOLUMES.values(): v["attached"].pop(sid, None)

def volume_mount(sb):
    """After boot: mount each volume where the spec says (the guest sees them as /dev/vdX in drive order)."""
    for s, (dev, ro) in zip(sb.spec.get("volumes", []), sb.vm.volume_devs()):
        opts = "-o ro,noload " if ro else ""                      # noload: a reader must not replay the journal (the device is read-only; a writer may have left one)
        cmd = f"mkdir -p {shlex.quote(s['mount'])} && mount {opts}{dev} {shlex.quote(s['mount'])}" + ("" if ro else f" && chown agent:agent {shlex.quote(s['mount'])}")
        r = sb.vm.guest.exec(cmd, user="root")
        if r.get("exit") != 0: audit(op="volume_mount_failed", sandbox=sb.id, volume=s["id"], dev=dev, stderr=str(r.get("stderr", ""))[:200])


def _apply_policy(vm, sid, net): egress_proxy.set_policy(vm.net.ip, sid, net["allow_hosts"], net.get("mode"), net.get("rules") or [], net.get("limits") or {})

def attach_net(vm, sid, spec):
    vm.net = netmgr.VMNet(sid, owner=(vm.jail["uid"], vm.jail["gid"]) if vm.jail else None)
    _apply_policy(vm, sid, spec["network"])
    if spec["network"]["allow_direct"]: vm.net.set_direct(spec["network"]["allow_direct"])


def detach_net(vm):
    if vm.net:
        egress_proxy.clear_policy(vm.net.ip); egress_proxy.forget(vm.name); vm.net.destroy(); vm.net = None


def save_spec(sb):
    json.dump({"id": sb.id, "spec": sb.spec, "created": sb.created}, open(f"{sb.vm.dir}/spec.json", "w"))


def _fsig(path):
    try: st = os.stat(path); return [st.st_size, int(st.st_mtime)]
    except OSError: return None


def tpl_key(spec):
    """A template snapshot is only valid for the exact guest configuration it was taken with (device set, memory size,
    layers) and the exact images; all of that goes into the key."""
    t = TEMPLATES[spec["template"]]
    ident = {k: spec.get(k) for k in ("template", "vcpu", "mem", "pmem_base", "balloon", "layers", "disk")}
    ident["jail"] = JAIL                     # a snapshot taken unjailed is not readable by a jailed VMM and vice versa
    ident["images"] = [_fsig(t["kernel"]), _fsig(t["base"])] + [_fsig(LAYERS[l]["path"]) for l in spec["layers"]]
    return hashlib.sha1(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:16]


def take_from_pool(key):
    with POOL_LOCK:
        p = POOL.get(key)
        while p and p["ready"]:
            sb = p["ready"].pop(0)
            if sb.vm.status == "running": return sb
            threading.Thread(target=destroy, args=(sb, "pool_unhealthy"), daemon=True).start()
    return None


def adopt(sb, spec):
    """Hand a warm pooled sandbox to a caller: apply the caller's policy (network, qos, ttl, class), refresh clock
    and entropy, and register it. Guest config (template, vcpu, mem, layers, devices) already matches by key."""
    vm = sb.vm; sb.spec = spec; sb.created = time.time(); sb.touch()
    _apply_policy(vm, sb.id, spec["network"])
    if spec["network"]["allow_direct"]: vm.net.set_direct(spec["network"]["allow_direct"])
    vm.apply_qos(spec["qos"])
    vm.guest.call("set_time", epoch=time.time())
    vm.guest.call("add_entropy", data_b64=base64.b64encode(os.urandom(64)).decode())
    sb.boot_mode = "pool"; save_spec(sb)
    with REG_LOCK: REG[sb.id] = sb
    return sb


def resolve_template_key(spec):
    """A named template (committed from a sandbox, docs/002 沙箱侧 4) wins; a 16-hex key is the control plane's placement hint for the spec's own
    template snapshot and is ignored when this node lacks it. A named template must have been taken with the same guest configuration."""
    key = spec.get("template_key") or ""
    if not key: return tpl_key(spec)
    tpl = TPL_SNAPS.get(key)
    if tpl is None:
        if re.fullmatch(r"[0-9a-f]{16}", key): return tpl_key(spec)
        raise ValueError(f"no template {key}")
    if spec.get("boot") == "cold": return tpl_key(spec)       # D80: the template's base and layers at any size, booted fresh: nothing of its memory (a running database, a warm cache) comes along
    for k in ("template", "vcpu", "mem", "pmem_base", "balloon", "layers"):
        if tpl.get(k) != spec.get(k):
            raise ValueError(f"template {key} was taken with {k}={tpl.get(k)!r}, this sandbox asks {spec.get(k)!r}: a template is a snapshot of one exact size. "
                             f"Leave {k} out to take the template's, or add \"boot\": \"cold\" to boot its base and layers fresh at the size you ask (without its memory state)")
    return key

def create(spec, name=None, pooled=False):
    if DRAINING[0]: raise PermissionError("node is draining")
    key = resolve_template_key(spec)
    if not pooled and spec.get("boot", "auto") != "cold":
        sb = take_from_pool(key)
        if sb:
            t0 = time.monotonic(); adopt(sb, spec)
            audit(op="create", sandbox=sb.id, spec=spec, boot_s=round(time.monotonic() - t0, 3), boot_mode="pool", template_snapshot=key, ip=sb.vm.net.ip)
            return sb
    err = admit(spec)
    if err: raise PermissionError(err)
    sid = name or f"sb-{uuid.uuid4().hex[:8]}"; t = TEMPLATES[spec["template"]]
    tpl = TPL_SNAPS.get(key) if spec.get("boot", "auto") != "cold" else None
    if spec.get("boot") == "snapshot" and not tpl: raise ValueError("no template snapshot for this spec; POST /templates/snapshot first")
    if spec.get("volumes"):                                                     # a template snapshot has no volume drives: volumes boot cold
        if spec.get("boot") == "snapshot": raise ValueError("a sandbox with volumes cannot boot from a template snapshot")
        tpl = None; volume_attach(spec, sid)
    vm = VM(sid, t["kernel"], t["base"], f"{tpl['dir']}/upper.ext4" if tpl else t["upper"], spec["vcpu"], spec["mem"], f"{STATE}/vms", **vm_kwargs(spec, sid))
    attach_net(vm, sid, spec)
    sb = Sandbox(sid, spec, vm); save_spec(sb)
    if not pooled:
        with REG_LOCK: REG[sid] = sb
    t0 = time.monotonic()
    if tpl:   # DSec-style warm start: map the template's memory (page cache shared by every sandbox started from it)
        try:
            vm.restore(tpl["dir"]); sb.boot_mode, sb.tpl_key = "snapshot", key
            vm.guest.exec(f"hostname {sid}", user="root")
        except Exception as e:  # noqa: BLE001 - a stale/unreadable template must not make creates fail: drop it, boot cold
            audit(op="template_restore_failed", sandbox=sid, key=key, error=repr(e)[:200])
            try: vm.kill()
            except Exception: pass  # noqa: BLE001
            if spec.get("boot") == "snapshot": detach_net(vm); REG.pop(sid, None); raise
            drop_template_snapshot(key)
            vm = VM(sid, t["kernel"], t["base"], t["upper"], spec["vcpu"], spec["mem"], f"{STATE}/vms", **vm_kwargs(spec, sid))
            vm.net = sb.vm.net; vm.guest_env = dict(sb.spec.get("env") or {}); sb.vm = vm; vm.boot(); sb.boot_mode = "cold"; sb.tpl_key = None
    else:
        vm.boot(); sb.boot_mode = "cold"
        if spec.get("volumes"): volume_mount(sb)
    audit(op="pool_create" if pooled else "create", sandbox=sid, spec=spec, boot_s=round(time.monotonic() - t0, 3), boot_mode=sb.boot_mode,
          template_snapshot=sb.tpl_key, ip=vm.net.ip, core_sched=vm.core_sched)
    return sb


# ---------------- template pool: template snapshot + N warm sandboxes restored from it, refilled in the background ----------------
def drop_template_snapshot(key):
    m = TPL_SNAPS.pop(key, None)
    if not m: return
    pool_drop(key) if key in POOL else None
    for f in (f"{TPL_DIR}/{key}.json",):
        try: os.remove(f)
        except OSError: pass
    shutil.rmtree(m.get("dir", f"{TPL_DIR}/{key}"), ignore_errors=True)
    audit(op="template_dropped", key=key)


def pool_set(spec, want):
    """Ensure the template snapshot exists (build it if not) and keep `want` warm sandboxes for it."""
    key = tpl_key(spec)
    if key not in TPL_SNAPS: build_template_snapshot(spec)
    pspec = {**spec, "class": "ephemeral", "qos": "be", "boot": "snapshot", "network": {"allow_hosts": [], "allow_direct": []}, "ttl_idle_s": 0, "ttl_max_s": 0}
    with POOL_LOCK:
        p = POOL.setdefault(key, {"key": key, "spec": pspec, "want": 0, "ready": [], "building": False, "created": time.time()})
        p["want"] = int(want); p["spec"] = pspec
        extra = p["ready"][want:]; del p["ready"][want:]
    for sb in extra: destroy(sb, "pool_shrink")
    audit(op="pool_set", key=key, want=int(want)); return pool_info(key)


def pool_info(key):
    p = POOL[key]
    return {"key": key, "want": p["want"], "ready": len(p["ready"]), "building": p["building"],
            "spec": {k: p["spec"][k] for k in ("template", "vcpu", "mem", "pmem_base", "balloon", "layers")},
            "sandboxes": [s.id for s in p["ready"]]}


def pool_fill_loop():
    while True:
        time.sleep(0.5)
        if DRAINING[0]: continue
        for key, p in list(POOL.items()):
            with POOL_LOCK:
                p["ready"] = [s for s in p["ready"] if s.vm.status == "running"]     # drop ones that died while waiting
                need = p["want"] > len(p["ready"]) and not p["building"] and key in TPL_SNAPS
                if need: p["building"] = True
            if not need: continue
            try:
                sb = create(p["spec"], pooled=True)
                with POOL_LOCK:
                    if p["want"] > len(p["ready"]): p["ready"].append(sb); sb = None
                if sb: destroy(sb, "pool_overfull")
            except Exception as e:  # noqa: BLE001
                audit(op="pool_fill_error", key=key, error=repr(e)[:200]); time.sleep(5)
            finally:
                p["building"] = False


def pool_drop(key, reason="api"):
    with POOL_LOCK: p = POOL.pop(key, None)
    if not p: raise KeyError(f"no pool {key}")
    for sb in p["ready"]: destroy(sb, reason)
    audit(op="pool_drop", key=key, reason=reason)


def build_template_snapshot(spec):
    """Boot the template once, let the guest settle, Diff-snapshot it (resumable: taken since boot) and keep
    memory + state + the booted upper disk under TPL_DIR/<key>/. New sandboxes with the same key restore from it."""
    key = tpl_key(spec); sid = f"tpl-{key[:8]}"; t = TEMPLATES[spec["template"]]
    vm = VM(sid, t["kernel"], t["base"], t["upper"], spec["vcpu"], spec["mem"], f"{STATE}/vms", **vm_kwargs(spec, sid))
    attach_net(vm, sid, spec)
    try:
        boot_s = vm.boot(); vm.guest.exec("true"); time.sleep(0.5)          # let boot-time work (udev, journald) finish
        snap_s = vm.snapshot(f"{vm.dir}/tpl"); vm.kill()
        d = f"{TPL_DIR}/{key}"; shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
        for f in ("snapshot.bin", "mem.bin"): shutil.move(f"{vm.dir}/tpl/{f}", f"{d}/{f}")
        subprocess.check_call(["cp", "--sparse=always", f"{vm.dir}/upper.ext4", f"{d}/upper.ext4"])
        json.dump([f"{d}/mem.bin"], open(f"{d}/chain.json", "w"))
    finally:
        detach_net(vm); vm.cleanup(log=audit)
    meta = {"key": key, "dir": d, **{k: spec[k] for k in ("template", "vcpu", "mem", "pmem_base", "balloon", "layers")},
            "created": time.time(), "boot_s": round(boot_s, 3), "snapshot_s": round(snap_s, 3),
            "mem_mib": round(memchain.allocated_bytes(f"{d}/mem.bin") / 1048576, 1),
            "upper_mib": round(memchain.allocated_bytes(f"{d}/upper.ext4") / 1048576, 1)}
    json.dump(meta, open(f"{TPL_DIR}/{key}.json", "w")); TPL_SNAPS[key] = meta
    audit(op="template_snapshot", **meta); return meta


def destroy(sb, reason="api"):
    with sb.lock:
        rc = sb.vm.shutdown(timeout=5) if sb.vm.status == "running" else sb.vm.kill()
        detach_net(sb.vm); volume_detach(sb.id)
        with REG_LOCK:
            REG.pop(sb.id, None)
            for k in [k for k, v in IDEMP.items() if v[1].get("id") == sb.id]: IDEMP.pop(k)   # replay must not resurrect a dead id
        sb.vm.cleanup(log=audit)
        audit(op="destroy", sandbox=sb.id, reason=reason, rc=rc)
    if STORE and reason not in ("drain", "pool_shrink", "pool_overfull", "pool_unhealthy") and sb.spec.get("durable"):
        threading.Thread(target=drop_bundles, args=(sb.id,), daemon=True).start()


def drop_bundles(sid):
    """A destroyed sandbox's published pauses are history nobody can resume: remove them (chunks go at the next GC)."""
    try:
        names = [b for b in imgstore.list_bundles(STORE) if b.startswith(f"sb-{sid}-")]
        for b in names: imgstore.delete_bundle(STORE, b)
        if names: audit(op="bundles_dropped", sandbox=sid, bundles=names)
    except Exception as e:  # noqa: BLE001
        audit(op="bundles_drop_error", sandbox=sid, error=repr(e)[:200])


def recover():
    """After a restart: paused sandboxes (snapshot on disk) come back as paused; anything else is an orphan."""
    roots = [(d, f"{STATE}/vms/{d}") for d in os.listdir(f"{STATE}/vms")]
    if os.path.isdir(f"{JAIL_BASE}/firecracker"):
        roots += [(d, f"{JAIL_BASE}/firecracker/{d}/root") for d in os.listdir(f"{JAIL_BASE}/firecracker")]
    for d, path in sorted(roots):
        try:
            meta = json.load(open(f"{path}/spec.json"))
            if not os.path.exists(f"{path}/paused/snapshot.bin"): raise FileNotFoundError("no paused snapshot")
            spec = meta["spec"]; t = TEMPLATES[spec["template"]]
            vm = VM(d, t["kernel"], t["base"], None, spec["vcpu"], spec["mem"], f"{STATE}/vms", **vm_kwargs(spec, d))
            vm.status = "paused"; attach_net(vm, d, spec)
            if spec.get("volumes"): volume_attach(spec, d)                   # the bookkeeping is in memory; rebuild it from the sandboxes that came back
            sb = Sandbox(d, spec, vm); sb.created = meta.get("created", sb.created)
            with REG_LOCK: REG[d] = sb
            audit(op="recover", sandbox=d, status="paused")
        except Exception as e:  # noqa: BLE001
            for sub in ("images", "layers"): VM._unmount_all(f"{path}/{sub}")
            try: os.rmdir(f"/sys/fs/cgroup/{CG_PARENT}/{d}")
            except OSError: pass
            victim = os.path.dirname(path) if path.startswith(JAIL_BASE) else path
            if os.path.basename(victim) == d: shutil.rmtree(victim, ignore_errors=True)
            import traceback; audit(op="orphan_removed", sandbox=d, reason=repr(e), tb=traceback.format_exc()[-600:])


# ---------------- publishing to the store: a paused sandbox or a template snapshot becomes a bundle any node can import ----------------
def _bundle_files(vm):
    """Files that make up a paused sandbox: memory layers (chain), device state, writable disk; and each checkpoint's device state, writable-layer copy
    and memory chain (D34 补: checkpoints travel with the bundle, so a migrated or taken-over sandbox can still roll back)."""
    chain = json.load(open(f"{vm.dir}/paused/chain.json"))
    files = {**{f"mem/{i:02d}": p for i, p in enumerate(chain)}, "snapshot.bin": f"{vm.dir}/paused/snapshot.bin", "upper.ext4": f"{vm.dir}/upper.ext4"}
    d = f"{vm.dir}/ckpt"
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        try: cchain = json.load(open(f"{d}/{name}/chain.json"))
        except (OSError, ValueError): continue
        if not (os.path.exists(f"{d}/{name}/snapshot.bin") and os.path.exists(f"{d}/{name}/upper.ext4")): continue
        files[f"ckpt/{name}/snapshot.bin"] = f"{d}/{name}/snapshot.bin"; files[f"ckpt/{name}/upper.ext4"] = f"{d}/{name}/upper.ext4"
        for i, p in enumerate(cchain): files[f"ckpt/{name}/mem/{i:02d}"] = p
    return chain, files


def _content_id(path):
    """sha1 over the allocated extents (offset + data) of a sparse file: names shared layers by content."""
    h = hashlib.sha1(str(os.path.getsize(path)).encode())
    with open(path, "rb") as f:
        for off, ln in memchain.data_extents(path):
            h.update(off.to_bytes(8, "little")); f.seek(off); left = ln
            while left:
                b = f.read(min(left, 1 << 20)); h.update(b); left -= len(b)
    return h.hexdigest()[:16]


def export_sandbox(sb):
    """Paused sandbox -> bundle in the store (chunks dedup'd: a template layer shared by many sandboxes is uploaded once)."""
    if sb.vm.status != "paused": raise RuntimeError("export needs a paused sandbox")
    t0 = time.monotonic(); chain, files = _bundle_files(sb.vm); name = f"sb-{sb.id}-{int(time.time())}"; stats = {}; images = {}; by_path = {}
    for rel, path in files.items():
        if path in by_path: images[rel] = by_path[path]; continue                   # a memory layer the main chain and a checkpoint share: uploaded once
        img = f"{name}--{rel.replace('/', '_')}"
        if "mem/" in rel and not path.startswith(f"{sb.vm.dir}/mem/"):   # a shared (template/parent) layer: name by content so it dedups
            img = "layer-" + _content_id(path)
        _, st = imgstore.push_remote(STORE, img, path); images[rel] = img; stats[rel] = st; by_path[path] = img
    bundle = {"name": name, "kind": "sandbox", "id": sb.id, "node": NODE_NAME, "created": time.time(), "spec": sb.spec,
              "sandbox_created": sb.created, "images": images, "chain_len": len(chain), "stats": stats}
    imgstore.put_bundle(STORE, name, bundle); sb.spec["bundle"] = name; save_spec(sb)
    sent = round(sum(x["sent_mib"] for x in stats.values()), 1); s = round(time.monotonic() - t0, 3)
    audit(op="export", sandbox=sb.id, bundle=name, sent_mib=sent, s=s); return {**bundle, "sent_mib": sent, "s": s}


def import_sandbox(name):
    b = imgstore.get_bundle(STORE, name)
    if not b or b.get("kind") != "sandbox": raise KeyError(f"no sandbox bundle {name}")
    sid = b["id"]
    if sid in REG: raise RuntimeError(f"sandbox {sid} already on this node")
    spec = new_spec({k: v for k, v in b["spec"].items() if k != "bundle"}); spec["bundle"] = name
    err = admit(spec)
    if err: raise PermissionError(err)
    t0 = time.monotonic(); t = TEMPLATES[spec["template"]]
    vm = VM(sid, t["kernel"], t["base"], None, spec["vcpu"], spec["mem"], f"{STATE}/vms", **vm_kwargs(spec, sid))
    try:
        os.makedirs(f"{vm.dir}/paused", exist_ok=True); os.makedirs(f"{vm.dir}/mem", exist_ok=True)
        for d in ("paused", "mem"): vm._own(f"{vm.dir}/{d}")
        chain = []; stats = {}; pulled = {}; cchains = {}
        for rel, img in sorted(b["images"].items()):              # ckpt/* sorts first: a layer a checkpoint shares with the main chain is pulled once
            if rel.startswith("mem/"):
                if img in pulled: chain.append(pulled[img]); continue
                dst = f"{vm.dir}/mem/{vm._stamp()}-{rel[4:]}.bin"; chain.append(dst)
            elif rel == "snapshot.bin": dst = f"{vm.dir}/paused/snapshot.bin"
            elif rel.startswith("ckpt/"):
                _, cname, *rest = rel.split("/"); cdir = f"{vm.dir}/ckpt/{cname}"
                if not os.path.isdir(cdir): os.makedirs(cdir); vm._own(cdir)
                if rest[0] == "mem":
                    if img in pulled: cchains.setdefault(cname, {})[rest[1]] = pulled[img]; continue
                    dst = f"{vm.dir}/mem/{vm._stamp()}-ck-{cname}-{rest[1]}.bin"; cchains.setdefault(cname, {})[rest[1]] = dst
                else: dst = f"{cdir}/{rest[0]}"
            else: dst = f"{vm.dir}/{rel}"
            stats[rel] = imgstore.pull_remote(STORE, img, dst); vm._own(dst); os.chmod(dst, 0o640); pulled[img] = dst
        for cname, m in cchains.items(): json.dump([m[k] for k in sorted(m)], open(f"{vm.dir}/ckpt/{cname}/chain.json", "w"))
        spec["checkpoints"] = [c for c in (b["spec"].get("checkpoints") or []) if os.path.exists(f"{vm.dir}/ckpt/{c.get('name')}/snapshot.bin")]
        json.dump(chain, open(f"{vm.dir}/paused/chain.json", "w")); vm.mem_chain = chain; vm.status = "paused"
        attach_net(vm, sid, spec); sb = Sandbox(sid, spec, vm); sb.created = b.get("sandbox_created", sb.created); sb.boot_mode = "import"
        save_spec(sb)
        with REG_LOCK: REG[sid] = sb
    except Exception:
        vm.cleanup(log=audit); raise
    got = round(sum(x["fetched_mib"] for x in stats.values()), 1); s = round(time.monotonic() - t0, 3)
    audit(op="import", sandbox=sid, bundle=name, fetched_mib=got, s=s, source_node=b.get("node")); return sb, {"fetched_mib": got, "s": s}


def export_template(key):
    meta = TPL_SNAPS[key]; d = meta["dir"]; t0 = time.monotonic(); name = f"tpl-{key}"; images = {}; stats = {}
    for rel in ("mem.bin", "snapshot.bin", "upper.ext4"):
        img = f"{name}--{rel}"; _, stats[rel] = imgstore.push_remote(STORE, img, f"{d}/{rel}"); images[rel] = img
    bundle = {"name": name, "kind": "template", "key": key, "node": NODE_NAME, "created": time.time(), "meta": meta, "images": images}
    imgstore.put_bundle(STORE, name, bundle); s = round(time.monotonic() - t0, 3)
    audit(op="template_export", key=key, bundle=name, s=s); return {**bundle, "s": s, "sent_mib": round(sum(x["sent_mib"] for x in stats.values()), 1)}


def import_template(name):
    b = imgstore.get_bundle(STORE, name)
    if not b or b.get("kind") != "template": raise KeyError(f"no template bundle {name}")
    key = b["key"]; spec = new_spec({k: b["meta"][k] for k in ("template", "vcpu", "mem", "pmem_base", "balloon", "layers")})
    if tpl_key(spec) != key: raise RuntimeError("template bundle was built against different images (key mismatch)")
    t0 = time.monotonic(); d = f"{TPL_DIR}/{key}"; shutil.rmtree(d, ignore_errors=True); os.makedirs(d); got = 0
    for rel, img in b["images"].items():
        st = imgstore.pull_remote(STORE, img, f"{d}/{rel}"); got += st["fetched_mib"]
        os.chmod(f"{d}/{rel}", 0o644 if rel == "upper.ext4" else 0o640)
        if JAIL and rel != "upper.ext4": os.chown(f"{d}/{rel}", 0, JAIL_GID)
    json.dump([f"{d}/mem.bin"], open(f"{d}/chain.json", "w"))
    meta = {**b["meta"], "dir": d, "imported_from": b.get("node")}; json.dump(meta, open(f"{TPL_DIR}/{key}.json", "w")); TPL_SNAPS[key] = meta
    s = round(time.monotonic() - t0, 3); audit(op="template_import", key=key, bundle=name, fetched_mib=round(got, 1), s=s)
    return {**meta, "fetched_mib": round(got, 1), "s": s}


def do_pause(sb, reason):
    """Pause to disk; durable sandboxes are then published so another node can take them over."""
    t = sb.vm.pause_to_disk(); audit(op=reason, sandbox=sb.id, s=round(t, 3))
    sb.spec["paused_at"] = time.time(); save_spec(sb)                               # the retention clock (003: a paused sandbox is kept retain_paused_s, then deleted)
    if sb.spec.get("durable") and STORE:
        try: export_sandbox(sb)
        except Exception as e: audit(op="export_error", sandbox=sb.id, error=repr(e)[:200])   # noqa: BLE001
    return t


# ---------------- drain (SIGTERM): running persistent sandboxes are paused to disk, the rest destroyed ----------------
def drain():
    DRAINING[0] = True; t0 = time.monotonic(); audit(op="drain_start", sandboxes=len(REG))
    def one(sb):
        try:
            with sb.lock:
                if sb.vm.status == "running" and sb.spec["class"] == "persistent":
                    do_pause(sb, "drain_pause")
                elif sb.vm.status != "paused": destroy(sb, "drain")
        except Exception as e:  # noqa: BLE001
            audit(op="drain_error", sandbox=sb.id, error=repr(e))
    pooled = [s for p in POOL.values() for s in p["ready"]]
    ths = [threading.Thread(target=one, args=(sb,)) for sb in list(REG.values())] + [threading.Thread(target=destroy, args=(s, "drain")) for s in pooled]
    [t.start() for t in ths]; [t.join() for t in ths]
    audit(op="drain_done", s=round(time.monotonic() - t0, 3)); os._exit(0)


# ---------------- fence (D54): a node the control plane cannot reach stops its own sandboxes ----------------
# The control plane cannot power a node off, so "node down" may really be "network cut, VMs still running". Evacuating then would run a sandbox twice.
# The node therefore fences itself: no authenticated contact for FENCE_AFTER_S -> pause persistent sandboxes (a durable one publishes its bundle if the store is reachable),
# destroy the rest. The control plane only auto-evacuates after FENCE_AFTER_S plus a margin (AUTO_EVACUATE_AFTER_S). 0 = off (default).
FENCE_AFTER_S = float(os.environ.get("FENCE_AFTER_S", "0"))
LAST_CP = [time.time()]; FENCED = [False]

def fence_all():
    def one(sb):
        try:
            with sb.lock:
                if sb.vm.status == "running" and sb.spec["class"] == "persistent": do_pause(sb, "fence_pause")
                elif sb.vm.status == "running": destroy(sb, "fence")
        except Exception as e:  # noqa: BLE001
            audit(op="fence_error", sandbox=sb.id, error=repr(e))
    ths = [threading.Thread(target=one, args=(sb,)) for sb in list(REG.values())]
    [t.start() for t in ths]; [t.join() for t in ths]

def fence_loop():
    while True:
        time.sleep(max(1.0, min(5.0, FENCE_AFTER_S / 4)))
        quiet = time.time() - LAST_CP[0]
        if not FENCED[0] and quiet > FENCE_AFTER_S:
            FENCED[0] = True; audit(op="fence_start", quiet_s=round(quiet), sandboxes=len(REG)); t0 = time.monotonic(); fence_all(); audit(op="fence_done", s=round(time.monotonic() - t0, 3))
        elif FENCED[0] and quiet <= FENCE_AFTER_S:
            FENCED[0] = False; audit(op="fence_lifted")        # paused sandboxes stay paused: the control plane decides what to do with them (resume, or they were already evacuated)


# ---------------- TTL loop ----------------
def ttl_loop():
    while True:
        time.sleep(1)
        now = time.time()
        for sb in list(REG.values()):
            try:
                with sb.lock:
                    st = sb.vm.status; idle = now - sb.last_used; age = now - sb.created
                    if sb.spec["ttl_max_s"] and age > sb.spec["ttl_max_s"]:
                        destroy(sb, "ttl_max"); continue
                    if st.startswith("failed"):
                        destroy(sb, st); continue
                    if st == "paused" and sb.spec.get("retain_paused_s") and sb.spec.get("paused_at") and now - sb.spec["paused_at"] > sb.spec["retain_paused_s"]:
                        destroy(sb, "retain_expired"); continue      # kept this long after its pause; a durable one was published at pause and can be imported again
                    if st == "running" and sb.spec["ttl_idle_s"] and idle > sb.spec["ttl_idle_s"]:
                        if sb.spec["class"] == "ephemeral": destroy(sb, "ttl_idle")
                        else:
                            do_pause(sb, "auto_pause")
            except Exception as e:  # noqa: BLE001
                import traceback; audit(op="ttl_error", sandbox=sb.id, error=repr(e), tb=traceback.format_exc()[-600:])


# ---------------- HTTP ----------------
# ---- port exposure (docs/002 沙箱侧 8 PortExposure; D39): /sandboxes/<id>/ports/<port>/<path> is reverse-proxied to the guest's ip:port over the tap.
# The node is the only thing that can reach a guest address; the control plane forwards here, so a service an agent started in a sandbox is reachable
# through the same API and bearer as everything else. Plain HTTP as whole responses; an Upgrade (WebSocket) request becomes a byte tunnel after the handshake.
PORT_RE = re.compile(r"^/sandboxes/([^/]+)/ports/(\d+)(/.*)?$")
HOP_HEADERS = ("host", "authorization", "connection", "transfer-encoding", "keep-alive", "proxy-authorization", "te", "upgrade")

def proxy_port(h, sid, port, sub, method, raw, qs):
    import http.client            # (also used by _stream_port_response below)
    sb = get_sb(sid)
    with sb.lock: sb.ensure_running(); sb.touch(); ip = sb.vm.net.ip if sb.vm.net else None
    if not ip: return h._send(503, {"error": "sandbox has no network"})
    port = int(port)
    if not 1 <= port <= 65535: return h._send(400, {"error": "bad port"})
    hdr = {k: v for k, v in h.headers.items() if k.lower() not in HOP_HEADERS}; hdr["Host"] = f"{ip}:{port}"; hdr["Connection"] = "close"
    path = (sub or "/") + (f"?{qs}" if qs else "")
    if h.headers.get("Upgrade", "").lower() == "websocket" and "upgrade" in h.headers.get("Connection", "").lower():
        return _tunnel_upgrade(h, sid, ip, port, path, method)
    t0 = time.monotonic()
    try:
        c = http.client.HTTPConnection(ip, port, timeout=60); c.request(method, path, body=raw or None, headers=hdr); r = c.getresponse()
    except (OSError, http.client.HTTPException) as e:
        audit(op="port", sandbox=sid, port=port, path=path[:120], error=repr(e)[:120]); return h._send(502, {"error": f"sandbox port {port}: {e}"})
    if r.getheader("Content-Length") is None and r.status not in (204, 304):
        return _stream_port_response(h, r, c, sid, port, method, path, t0)          # a streamed answer (NDJSON, SSE, chunked): pass it on as it arrives, not when it ends
    try: data = r.read()
    except (OSError, http.client.HTTPException) as e:
        audit(op="port", sandbox=sid, port=port, path=path[:120], error=repr(e)[:120]); return h._send(502, {"error": f"sandbox port {port}: {e}"})
    h.send_response(r.status)
    for k, v in r.getheaders():
        if k.lower() not in ("connection", "transfer-encoding", "content-length", "keep-alive"): h.send_header(k, v)
    h.send_header("Content-Length", str(len(data))); h.end_headers(); h.wfile.write(data)
    audit(op="port", sandbox=sid, port=port, method=method, path=path[:120], status=r.status, bytes=len(data), s=round(time.monotonic() - t0, 3))

def _stream_port_response(h, r, c, sid, port, method, path, t0):
    """Relay a response that has no Content-Length chunk by chunk (Transfer-Encoding: chunked towards the caller), so a long-running stream such as code-interpreter's
    /execute reaches the client while it runs. The read timeout of the upstream connection is lifted: such a stream may be quiet for a long time."""
    n = 0
    try:
        h.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in ("connection", "transfer-encoding", "content-length", "keep-alive"): h.send_header(k, v)
        h.send_header("Transfer-Encoding", "chunked"); h.end_headers()
        c.sock.settimeout(None)
        while True:
            d = r.read1(65536)
            if not d: break
            n += len(d); h.wfile.write(f"{len(d):x}\r\n".encode() + d + b"\r\n")
        h.wfile.write(b"0\r\n\r\n")
    except (OSError, ValueError, http.client.HTTPException) as e:                # the caller went away or the service did: nothing more to say to either
        h.close_connection = True; audit(op="port", sandbox=sid, port=port, path=path[:120], error=repr(e)[:120], bytes=n)
        return
    finally:
        try: c.close()
        except OSError: pass
    audit(op="port", sandbox=sid, port=port, method=method, path=path[:120], status=r.status, bytes=n, streamed=True, s=round(time.monotonic() - t0, 3))

def _tunnel_upgrade(h, sid, ip, port, path, method):
    """Relay the WebSocket handshake to the guest and, on 101, hand the two sockets to each other until one side closes (the handler reads unbuffered,
    so bytes the client sent right after its handshake are still on the socket, not lost in a buffer)."""
    t0 = time.monotonic()
    try: up = socket.create_connection((ip, port), timeout=15)
    except OSError as e:
        audit(op="port", sandbox=sid, port=port, path=path[:120], error=repr(e)[:120]); return h._send(502, {"error": f"sandbox port {port}: {e}"})
    hdr = "".join(f"{k}: {v}\r\n" for k, v in h.headers.items() if k.lower() not in ("host", "authorization", "proxy-authorization"))
    up.sendall(f"{method} {path} HTTP/1.1\r\nHost: {ip}:{port}\r\n{hdr}\r\n".encode()); up.settimeout(30)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            d = up.recv(65536)
            if not d: break
            buf += d
    except OSError as e:
        up.close(); audit(op="port", sandbox=sid, port=port, path=path[:120], error=repr(e)[:120]); return h._send(502, {"error": f"sandbox port {port}: {e}"})
    head, _, rest = buf.partition(b"\r\n\r\n"); status = head.split(b" ")[1:2]
    h.close_connection = True
    if status != [b"101"]:                                   # the guest declined the upgrade: pass its answer through as is and stop
        h.connection.sendall(buf); up.close(); audit(op="port_upgrade", sandbox=sid, port=port, path=path[:120], status=status[0].decode(errors="replace") if status else None); return
    h.connection.sendall(head + b"\r\n\r\n" + rest); up.settimeout(None)
    audit(op="port_upgrade", sandbox=sid, port=port, path=path[:120], status=101, s=round(time.monotonic() - t0, 3))
    egress_proxy._pipe(h.connection, up, idle=3600)          # closes both

# ---- E2B envd relay (docs/research/e2b-compat-2026-11.md §5): /sandboxes/<id>/envd/<path> is relayed, byte for byte, to the guest's envd listener on vsock
# port 49983 (sandboxd answers HTTP/1.1 there; one request per connection, Connection: close). The control plane has already authenticated the caller and parsed
# what it audits; the node only resumes a paused sandbox, counts it as activity, and pipes both directions (streamed Connect responses flow as they are produced).
ENVD_RE = re.compile(r"^/sandboxes/([^/]+)/envd(/.*)?$")
ENVD_VSOCK_PORT = 49983
ENVD_SKIP = ("host", "authorization", "x-envd-authorization", "connection", "proxy-authorization", "keep-alive", "te", "upgrade")

def proxy_envd(h, sid, sub, method, qs):
    sb = get_sb(sid)
    with sb.lock: sb.ensure_running(); sb.touch(); vdir = sb.vm.dir
    path = (sub or "/") + (f"?{qs}" if qs else "")
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); up.settimeout(15)
    try:
        up.connect(f"{vdir}/v.sock"); up.sendall(f"CONNECT {ENVD_VSOCK_PORT}\n".encode())
        ack = b""
        while not ack.endswith(b"\n") and len(ack) < 64:
            c = up.recv(1)
            if not c: break
            ack += c
        if not ack.startswith(b"OK"): raise OSError(f"guest has no envd listener ({ack!r}): rebuild the base image with this sandboxd")
    except OSError as e:
        up.close(); audit(op="envd", sandbox=sid, path=sub or "/", error=repr(e)[:160])
        return h._send(503, {"code": "unavailable", "message": f"sandbox has no envd: {e}"})
    hdrs = "".join(f"{k}: {v}\r\n" for k, v in h.headers.items() if k.lower() not in ENVD_SKIP)
    user_auth = h.headers.get("X-Envd-Authorization")        # the SDK's own Authorization (Basic <user>:) cannot share the header the node bearer uses
    if user_auth: hdrs += f"Authorization: {user_auth}\r\n"
    up.sendall(f"{method} {path} HTTP/1.1\r\nHost: envd\r\n{hdrs}Connection: close\r\n\r\n".encode()); up.settimeout(None)
    h.close_connection = True
    audit(op="envd", sandbox=sid, method=method, path=(sub or "/")[:120])
    egress_proxy._pipe(h.connection, up, idle=3600)           # closes both

ROUTES = []
def route(method, pattern):
    def deco(fn): ROUTES.append((method, re.compile("^" + pattern + "$"), fn)); return fn
    return deco


def get_sb(sid):
    sb = REG.get(sid)
    if not sb: raise KeyError(f"no sandbox {sid}")
    return sb


def node_mem():
    try: return int(open("/sys/fs/cgroup/memory.current").read()) // 1048576
    except OSError: return None

@route("GET", "/health")
def h_health(h, m, body): return 200, {"ok": True, **usage(), "node_mem_mib": node_mem(), "layers": len(LAYERS), "jail": JAIL, "uffd": UFFD_OK, "uffd_note": UFFD_WHY, "node": NODE_NAME, "store": STORE, "fence_after_s": FENCE_AFTER_S, "fenced": FENCED[0], "cp_quiet_s": round(time.time() - LAST_CP[0], 1),
                                       "template_snapshots": list(TPL_SNAPS), "pools": {k: len(p["ready"]) for k, p in POOL.items()}, "default_template": DEFAULT_TEMPLATE, "templates": list(TEMPLATES)}

@route("GET", "/sandboxes")
def h_list(h, m, body): return 200, {"sandboxes": [s.info() for s in REG.values()]}

@route("POST", "/sandboxes/([^/]+)/commit")
def h_commit(h, m, body):
    sb = get_sb(m.group(1))
    try: meta = commit_template(sb, str(body.get("name") or ""), body.get("note") or "")
    except FileExistsError as e: return 409, {"error": f"template {e} exists"}
    return 201, {k: v for k, v in meta.items() if k != "dir"}

@route("POST", "/layers/from-archive")
def h_layer_archive(h, m, body):
    if not isinstance(body, (bytes, bytearray)) or not body: return 400, {"error": "upload the image archive as the body (docker save / OCI layout tar); ?name="}
    try: return 201, layer_from_archive(bytes(body), h.query.get("name") or "archive")
    except subprocess.CalledProcessError as e: return 500, {"error": "unpack failed: " + (e.stderr or b"").decode(errors="replace")[-200:]}

@route("POST", "/layers/from-oci")
def h_layer_oci(h, m, body):
    image = str(body.get("image") or "")
    if not image or " " in image: return 400, {"error": "image required, e.g. python:3.12-slim or ghcr.io/org/app:tag"}
    bid = str(body.get("build") or "")[:40] or None
    try: return 201, layer_from_oci(image, str(body.get("name") or image.rsplit("/", 1)[-1].split(":")[0].split("@")[0]), bid)
    except (urllib.error.HTTPError, urllib.error.URLError) as e: blog(bid, f"failed: registry: {e}"); return 502, {"error": f"registry: {e}"}
    except subprocess.CalledProcessError as e: blog(bid, "failed: unpacking the layers"); return 500, {"error": "unpack failed: " + (e.stderr or b"").decode(errors="replace")[-200:]}
    except Exception as e: blog(bid, f"failed: {e}"); raise

@route("GET", "/builds/log/([^/]+)")
def h_build_log(h, m, body):
    b = BUILD_LOGS.get(m.group(1))
    try: since = int(h.query.get("since") or 0)
    except ValueError: since = 0
    if not b: return 404, {"error": "no log for this build (it ran on another node, or it is older than an hour)"}
    return 200, {"build": m.group(1), "lines": b["lines"][since:], "next": len(b["lines"])}

@route("GET", "/volumes")
def h_vol_list(h, m, body): return 200, {"volumes": [volume_info(v) for v in VOLUMES.values()]}

@route("POST", "/volumes")
def h_vol_create(h, m, body):
    try: return 201, volume_info(volume_create(body))
    except FileExistsError as e: return 409, {"error": f"volume {e} exists"}
    except subprocess.CalledProcessError as e: return 500, {"error": "mkfs failed: " + (e.stderr or b"").decode(errors="replace")[-200:]}

@route("POST", "/volumes/from-archive")
def h_vol_archive(h, m, body):
    """body = a tar; ?name=&project=&size_mib=&id=  -> a volume whose filesystem holds the tar's tree (the way a harness directory becomes a read-only volume)."""
    if not isinstance(body, (bytes, bytearray)) or not body: return 400, {"error": "upload a tar as the body"}
    q = h.query
    try: return 201, volume_info(volume_create({"name": q.get("name"), "project": q.get("project"), "size_mib": int(q.get("size_mib") or 64), "id": q.get("id")}, content=bytes(body)))
    except FileExistsError as e: return 409, {"error": f"volume {e} exists"}
    except subprocess.CalledProcessError as e: return 500, {"error": "mkfs failed: " + (e.stderr or b"").decode(errors="replace")[-200:]}

@route("GET", "/volumes/([^/]+)")
def h_vol_get(h, m, body):
    v = VOLUMES.get(m.group(1))
    return (200, volume_info(v)) if v else (404, {"error": "no volume"})

@route("GET", "/volumes/([^/]+)/export")
def h_vol_export(h, m, body):
    """One volume as a GNU sparse tar (<id>.ext4 + <id>.json) for another node's /volumes/import; refused while a writer holds it (D35 补: volumes move with a migration)."""
    with VOL_LOCK:
        v = VOLUMES.get(m.group(1))
        if not v: return 404, {"error": "no volume"}
        if "rw" in v["attached"].values() and any(REG[s].vm.status == "running" for s in v["attached"] if s in REG): return 409, {"error": "a running writer holds it"}
    desc = f"{STATE}/volumes/{v['id']}.json"
    if not os.path.exists(desc): _vol_save(v)
    data = subprocess.run(["tar", "-C", f"{STATE}/volumes", "-cS", f"{v['id']}.ext4", f"{v['id']}.json"], check=True, capture_output=True).stdout
    audit(op="volume_export", volume=v["id"], bytes=len(data)); return 200, data

@route("POST", "/volumes/import")
def h_vol_import(h, m, body):
    """The body is a tar from /volumes/<id>/export; registered here under the same id (already present: 409)."""
    import tempfile
    if not isinstance(body, (bytes, bytearray)) or not body: return 400, {"error": "upload the exported volume tar as the body"}
    with tempfile.TemporaryDirectory(dir=f"{STATE}/volumes") as tmp:
        subprocess.run(["tar", "-C", tmp, "-xS", "-f", "-"], input=bytes(body), check=True, capture_output=True)
        descs = [f for f in os.listdir(tmp) if f.endswith(".json")]
        if len(descs) != 1: return 400, {"error": "the tar must hold one volume description"}
        meta = json.load(open(f"{tmp}/{descs[0]}")); vid = str(meta.get("id") or "")
        if not re.match(r"^[a-z0-9][a-z0-9_.-]{0,47}$", vid) or not os.path.exists(f"{tmp}/{vid}.ext4"): return 400, {"error": "the tar must hold <id>.ext4 and <id>.json of one volume"}
        with VOL_LOCK:
            if vid in VOLUMES: return 409, {"error": f"volume {vid} exists here"}
            path = f"{STATE}/volumes/{vid}.ext4"; os.replace(f"{tmp}/{vid}.ext4", path); os.chmod(path, 0o644)
            v = {**meta, "path": path, "attached": {}, "imported": time.time()}; VOLUMES[vid] = v; _vol_save(v)
    audit(op="volume_import", volume=vid, bytes=os.path.getsize(path)); return 201, volume_info(v)

@route("DELETE", "/volumes/([^/]+)")
def h_vol_delete(h, m, body):
    with VOL_LOCK:
        v = VOLUMES.get(m.group(1))
        if not v: return 404, {"error": "no volume"}
        if v["attached"]: return 409, {"error": f"attached to {sorted(v['attached'])}"}
        VOLUMES.pop(v["id"]); 
        for p in (v["path"], f"{STATE}/volumes/{v['id']}.json"):
            try: os.unlink(p)
            except FileNotFoundError: pass
    audit(op="volume_delete", volume=v["id"]); return 200, {"deleted": v["id"]}

@route("POST", "/sandboxes")
def h_create(h, m, body):
    key = h.headers.get("Idempotency-Key")
    if key and key in IDEMP: return IDEMP[key]
    try: sb = create(new_spec(body))
    except PermissionError as e: return (503 if DRAINING[0] else 429), {"error": str(e), **usage()}
    resp = (201, sb.info())
    if key:
        if len(IDEMP) >= MAX_IDEMP:
            for k in list(IDEMP)[: MAX_IDEMP // 10]: IDEMP.pop(k, None)
        IDEMP[key] = resp
    return resp

@route("GET", "/sandboxes/([^/]+)")
def h_get(h, m, body): return 200, get_sb(m.group(1)).info()

@route("DELETE", "/sandboxes/([^/]+)")
def h_delete(h, m, body): destroy(get_sb(m.group(1))); return 200, {"deleted": m.group(1)}

@route("POST", "/sandboxes/([^/]+)/pause")
def h_pause(h, m, body):
    sb = get_sb(m.group(1))
    with sb.lock:
        if sb.vm.status != "running": return 409, {"error": f"status {sb.vm.status}"}
        do_pause(sb, "pause")
    return 200, sb.info()

@route("POST", "/sandboxes/([^/]+)/resume")
def h_resume(h, m, body):
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); sb.touch()
    return 200, sb.info()

# ---------------- checkpoints: in-place rollback points (docs/002 沙箱侧 5 状态; D34) ----------------
CKPT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")
def _ckpt_list(sb): return sb.spec.setdefault("checkpoints", [])

@route("GET", "/sandboxes/([^/]+)/checkpoints")
def h_ckpt_list(h, m, body): return 200, {"checkpoints": _ckpt_list(get_sb(m.group(1)))}

@route("POST", "/sandboxes/([^/]+)/checkpoints")
def h_ckpt_create(h, m, body):
    sb = get_sb(m.group(1)); name = str(body.get("name") or f"c{len(_ckpt_list(sb)) + 1}-{uuid.uuid4().hex[:4]}")
    if not CKPT_RE.match(name): return 400, {"error": "checkpoint name: lowercase letters, digits, _ . -, up to 40 chars"}
    if any(c["name"] == name for c in _ckpt_list(sb)): return 409, {"error": f"checkpoint {name} exists"}
    with sb.lock:
        sb.ensure_running(); sb.touch(); t = sb.vm.checkpoint(name)
        entry = {"name": name, "note": str(body.get("note") or "")[:200], "created": time.time(), "paused_s": round(t, 3),
                 "chain_len": len(sb.vm.mem_chain), "upper_mib": round(memchain.allocated_bytes(f"{sb.vm.dir}/ckpt/{name}/upper.ext4") / 1048576, 1)}
        _ckpt_list(sb).append(entry); save_spec(sb)
    audit(op="checkpoint", sandbox=sb.id, name=name, s=round(t, 3)); return 201, entry

@route("POST", "/sandboxes/([^/]+)/rollback")
def h_rollback(h, m, body):
    sb = get_sb(m.group(1)); name = str(body.get("checkpoint") or "")
    if not any(c["name"] == name for c in _ckpt_list(sb)): return 404, {"error": f"no checkpoint {name}"}
    with sb.lock:
        t = sb.vm.rollback(name); sb.touch(); sb.boot_mode = "rollback"
        try: sb.vm.guest.exec(f"hostname {sb.id}", user="root")      # the same sandbox, but the clock and hostname fixups a restore skips
        except Exception: pass                                           # noqa: BLE001
        save_spec(sb)
    ph = getattr(sb.vm, "restore_phases", None)
    audit(op="rollback", sandbox=sb.id, checkpoint=name, s=round(t, 3), phases=ph); return 200, {**sb.info(), "rollback_s": round(t, 3), "checkpoint": name, "phases": ph}

@route("DELETE", "/sandboxes/([^/]+)/checkpoints/([^/]+)")
def h_ckpt_delete(h, m, body):
    sb = get_sb(m.group(1)); name = m.group(2)
    if not any(c["name"] == name for c in _ckpt_list(sb)): return 404, {"error": f"no checkpoint {name}"}
    with sb.lock:
        sb.vm.checkpoint_drop(name); sb.spec["checkpoints"] = [c for c in _ckpt_list(sb) if c["name"] != name]; save_spec(sb)
    audit(op="checkpoint_drop", sandbox=sb.id, name=name); return 200, {"deleted": name, "checkpoints": _ckpt_list(sb)}

def _exec_env(sb, body):
    """The sandbox's own environment (spec.env: E2B envVars, D40 补) under the request's env; empty -> nothing passed."""
    env = {**(sb.spec.get("env") or {}), **(body.get("env") or {})}
    return {"env": env} if env else {}

def oom_note(uptime_text, dmesg_text, within_s):
    """The kernel's last "Out of memory: Killed process N (name)" line from the last within_s seconds of the guest's life, or None. Pure so it can be tested."""
    try: up = float(uptime_text.split()[0])
    except (ValueError, IndexError): return None
    found = None
    for ln in dmesg_text.splitlines():
        mm = re.match(r"^\[\s*(\d+\.\d+)\]\s+Out of memory: Killed process (\d+) \(([^)]*)\)", ln)
        if mm and up - float(mm.group(1)) <= within_s: found = {"pid": int(mm.group(2)), "process": mm.group(3)}
    return found


def _oom_check(sb, within_s):
    g = sb.vm.new_conn()
    try:
        r = g.exec("cat /proc/uptime; echo ---; dmesg 2>/dev/null | grep 'Out of memory' | tail -5", user="root", timeout=10)
    finally: g.close()
    up, _, dm = r.get("stdout", "").partition("---")
    return oom_note(up, dm, within_s)


@route("POST", "/sandboxes/([^/]+)/exec")
def h_exec(h, m, body):
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); sb.touch(); g = sb.vm.new_conn()
    t0 = time.monotonic()
    try: r = g.exec(body["cmd"], **{k: body[k] for k in ("user", "cwd", "timeout", "stdin", "landlock") if k in body}, **_exec_env(sb, body))
    finally: g.close()
    if r.get("exit") in (137, -1, -9):      # SIGKILL (137 through a shell, -1 when the guest runtime reaps a signalled child itself): when the kernel's OOM killer did it, say so (D79), or the caller only sees 137 and has to find dmesg
        try:
            o = _oom_check(sb, time.monotonic() - t0 + 0.5)      # only a kill during this very command: an earlier command's OOM seconds ago must not be pinned on it
            if o: r = {**r, "oom_killed": True, "oom": o, "hint": f"{o['process']} was killed by the kernel: out of memory in a {sb.spec.get('mem')} MiB sandbox; create it with more mem"}
        except Exception: pass     # the hint is a courtesy; the exec result stands without it
    audit(op="exec", sandbox=sb.id, user=body.get("user", "agent"), cmd=body["cmd"][:200], exit=r.get("exit"), s=round(time.monotonic() - t0, 3))
    return 200, r

@route("POST", "/sandboxes/([^/]+)/export")
def h_export(h, m, body):
    if not STORE: return 400, {"error": "no STORE configured"}
    sb = get_sb(m.group(1))
    with sb.lock:
        if sb.vm.status == "running": do_pause(sb, "pause")
        return 200, export_sandbox(sb)

@route("POST", "/sandboxes/import")
def h_import(h, m, body):
    if not STORE: return 400, {"error": "no STORE configured"}
    if DRAINING[0]: return 503, {"error": "node is draining"}
    try: sb, st = import_sandbox(body["bundle"])
    except PermissionError as e: return 429, {"error": str(e), **usage()}
    return 201, {**sb.info(), "import": st}

@route("POST", "/templates/([^/]+)/export")
def h_tpl_export(h, m, body):
    if m.group(1) not in TPL_SNAPS: raise KeyError(f"no template snapshot {m.group(1)}")
    return 200, export_template(m.group(1))

@route("POST", "/templates/import")
def h_tpl_import(h, m, body): return 201, import_template(body["bundle"])

@route("GET", "/bundles")
def h_bundles(h, m, body): return 200, {"bundles": imgstore.list_bundles(STORE) if STORE else []}

@route("PUT", "/sandboxes/([^/]+)/network")
def h_net(h, m, body):
    sb = get_sb(m.group(1))
    with sb.lock:
        net = sb.spec["network"]
        if "allow_hosts" in body: net["allow_hosts"] = list(body["allow_hosts"])
        if "allow_direct" in body: net["allow_direct"] = list(body["allow_direct"]); sb.vm.net.set_direct(net["allow_direct"])
        if "mode" in body:
            if body["mode"] not in egress_proxy.MODES: return 400, {"error": "mode must be closed|allowlist|audited"}
            net["mode"] = body["mode"]
        if "rules" in body:
            try: net["rules"] = egress_proxy.validate_rules(body["rules"])
            except ValueError as e: return 400, {"error": str(e)}
        if "limits" in body:
            try: net["limits"] = egress_proxy.validate_limits(body["limits"])
            except ValueError as e: return 400, {"error": str(e)}
        _apply_policy(sb.vm, sb.id, net); save_spec(sb)
    audit(op="network_update", sandbox=sb.id, network=net); return 200, {"network": net}

@route("POST", "/sandboxes/([^/]+)/ttl")
def h_ttl(h, m, body):
    """Change the idle / max TTL of a running sandbox (E2B's timeout / refreshes map here)."""
    sb = get_sb(m.group(1))
    with sb.lock:
        for k in ("ttl_idle_s", "ttl_max_s", "retain_paused_s"):
            if k in body:
                v = int(body[k])
                if v < 0 or v > 86400 * 365: return 400, {"error": f"{k} out of range"}
                sb.spec[k] = v
        sb.touch(); save_spec(sb)
    audit(op="ttl", sandbox=sb.id, ttl_idle_s=sb.spec.get("ttl_idle_s"), ttl_max_s=sb.spec.get("ttl_max_s"), retain_paused_s=sb.spec.get("retain_paused_s")); return 200, sb.info()

@route("GET", "/sandboxes/([^/]+)/egress")
def h_egress(h, m, body):
    sb = get_sb(m.group(1)); return 200, {"sandbox": sb.id, "decisions": egress_proxy.recent(sb.id)}

# ---------------- live metrics (D57): a ring of 5-second samples per sandbox ----------------
# The cgroup the jailer made for the VM already counts CPU and memory; sampling it is a couple of file reads per sandbox. Points live in memory only
# (an hour at 5 s): a restart empties them, a paused sandbox simply has a gap. The control plane forwards GET /sandboxes/<id>/metrics untouched.
METRICS_EVERY = float(os.environ.get("METRICS_EVERY_S", "5")); METRICS_KEEP = int(os.environ.get("METRICS_POINTS", "720"))
METRICS = {}; _CPU_PREV = {}

def cpu_usec_of(sid):
    cg = read_cgroup(f"/sys/fs/cgroup/{CG_PARENT}/{sid}")
    return cg[0] if cg else None

def read_cgroup(d):
    """(cpu_usec, mem_bytes) of a cgroup v2 directory, or None when it is not readable."""
    try:
        cpu = 0
        with open(f"{d}/cpu.stat") as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k == "usage_usec": cpu = int(v)
        with open(f"{d}/memory.current") as f: mem = int(f.read())
        return cpu, mem
    except (OSError, ValueError): return None

def metrics_point(prev, cur, now, egress=None):
    """One sample: cpu as % of one core since `prev` (None for the first), memory in MiB, cumulative egress bytes."""
    cpu_usec, mem = cur
    pct = None
    if prev and now > prev[0] and cpu_usec >= prev[1]: pct = round((cpu_usec - prev[1]) / ((now - prev[0]) * 1e6) * 100, 1)
    pt = {"t": round(now, 1), "cpu_pct": pct, "mem_mib": round(mem / 1048576, 1)}
    if egress: pt["egress_out"], pt["egress_in"] = egress.get("out", 0), egress.get("in", 0)
    return pt

def metrics_tick():
    now = time.time()
    for sid, sb in list(REG.items()):
        if sb.vm.status != "running" or not sb.vm.jail: _CPU_PREV.pop(sid, None); continue
        cur = read_cgroup(f"/sys/fs/cgroup/{CG_PARENT}/{sid}")
        if not cur: continue
        try: eg = egress_proxy.traffic(sb.vm.net.ip) if sb.vm.net else None
        except Exception: eg = None  # noqa: BLE001
        METRICS.setdefault(sid, collections.deque(maxlen=METRICS_KEEP)).append(metrics_point(_CPU_PREV.get(sid), cur, now, eg))
        _CPU_PREV[sid] = (now, cur[0])
    for sid in [k for k in METRICS if k not in REG]: METRICS.pop(sid, None); _CPU_PREV.pop(sid, None)

def metrics_loop():
    while True:
        time.sleep(METRICS_EVERY)
        try: metrics_tick()
        except Exception as e: audit(op="metrics_error", error=repr(e))  # noqa: BLE001

@route("GET", "/sandboxes/([^/]+)/metrics")
def h_metrics(h, m, body):
    sb = get_sb(m.group(1))
    try: since = float(h.query.get("since") or 0)
    except ValueError: since = 0.0
    return 200, {"sandbox": sb.id, "interval_s": METRICS_EVERY, "points": [p for p in list(METRICS.get(sb.id, ())) if p["t"] > since]}


# ---- the node's credential vault (docs/002 沙箱侧 6 Credential): names are listed, values never returned ----
CRED_FILE = os.environ.get("EGRESS_CREDENTIALS_FILE", f"{STATE}/credentials.json")
egress_proxy.load_credentials(CRED_FILE)

@route("GET", "/egress/credentials")
def h_cred_list(h, m, body): return 200, {"credentials": egress_proxy.credential_names()}

@route("PUT", "/egress/credentials/([^/]+)")
def h_cred_put(h, m, body):
    try: egress_proxy.set_credential(m.group(1), body.get("header"), body.get("value"), path=CRED_FILE)
    except ValueError as e: return 400, {"error": str(e)}
    audit(op="credential_set", name=m.group(1), header=body.get("header")); return 200, {"credentials": egress_proxy.credential_names()}

@route("DELETE", "/egress/credentials/([^/]+)")
def h_cred_del(h, m, body):
    egress_proxy.drop_credential(m.group(1), path=CRED_FILE); audit(op="credential_drop", name=m.group(1)); return 200, {"credentials": egress_proxy.credential_names()}

@route("POST", "/sandboxes/([^/]+)/exec/stream")
def h_exec_stream(h, m, body):
    """Chunked NDJSON: {"event":"out"|"err","data":...} lines, then the final result object."""
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); sb.touch(); g = sb.vm.new_conn()
    h.begin_stream()
    try:
        r = g.exec(body["cmd"], stream=True, on_event=lambda ev: h.chunk({"event": ev["event"], "data": ev["data"]}),
                   **{k: body[k] for k in ("user", "cwd", "timeout", "landlock") if k in body}, **_exec_env(sb, body))
    finally: g.close()
    h.chunk({"done": True, "exit": r.get("exit"), "timeout": r.get("timeout"), "elapsed_s": r.get("elapsed_s")}); h.end_stream()
    audit(op="exec", sandbox=sb.id, user=body.get("user", "agent"), cmd=body["cmd"][:200], exit=r.get("exit"), stream=True)
    return None

@route("POST", "/sandboxes/([^/]+)/sessions")
def h_sess_open(h, m, body):
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); sb.touch(); g = sb.vm.new_conn()
    try: r = g.call("session_open", **{k: body[k] for k in ("user", "pty") if k in body})
    finally: g.close()
    audit(op="session_open", sandbox=sb.id, session=r["session"], pty=r["pty"]); return 201, r

@route("POST", "/sandboxes/([^/]+)/sessions/([^/]+)/exec")
def h_sess_exec(h, m, body):
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); sb.touch(); g = sb.vm.new_conn()
    try: r = g.session_exec(m.group(2), body["cmd"], timeout=body.get("timeout", 60))
    finally: g.close()
    audit(op="session_exec", sandbox=sb.id, session=m.group(2), cmd=body["cmd"][:200], exit=r.get("exit")); return 200, r

@route("POST", "/sandboxes/([^/]+)/sessions/([^/]+)/write")
def h_sess_write(h, m, body):
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); sb.touch(); g = sb.vm.new_conn()
    try: return 200, g.session_write(m.group(2), base64.b64decode(body["data_b64"]))
    finally: g.close()

@route("POST", "/sandboxes/([^/]+)/sessions/([^/]+)/read")
def h_sess_read(h, m, body):
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); sb.touch(); g = sb.vm.new_conn()
    try: data = g.session_read(m.group(2), wait=body.get("wait", 0.2))
    finally: g.close()
    return 200, {"data_b64": base64.b64encode(data).decode(), "bytes": len(data)}

@route("DELETE", "/sandboxes/([^/]+)/sessions/([^/]+)")
def h_sess_close(h, m, body):
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); g = sb.vm.new_conn()
    try: return 200, g.session_close(m.group(2))
    finally: g.close()

@route("PUT", "/sandboxes/([^/]+)/files")
def h_put_file(h, m, body):          # raw body; ?path=
    sb = get_sb(m.group(1)); path = h.query.get("path")
    with sb.lock: sb.ensure_running(); sb.touch(); r = sb.vm.guest.write_file(path, body)
    audit(op="write_file", sandbox=sb.id, path=path, bytes=r["bytes"]); return 200, r

@route("GET", "/sandboxes/([^/]+)/events")
def h_events(h, m, body):           # kernel ABI event log (agent-runtime), ?since=<seq>&max=
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); sb.touch(); r = sb.vm.guest.call("abi_events", since=int(h.query.get("since", "0")), max=int(h.query.get("max", "1000")))
    return 200, {"events": r.get("events", []), "last_seq": r.get("last_seq", 0)}

@route("PUT", "/sandboxes/([^/]+)/budget")
def h_budget(h, m, body):           # local budget limits enforced by the kernel ABI (the proxy's limits stay authoritative)
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); r = sb.vm.guest.call("budget_set", max_tokens=int(body.get("max_tokens", 0)), max_usd=float(body.get("max_usd", 0)))
    audit(op="budget_set", sandbox=sb.id, **{k: body.get(k) for k in ("max_tokens", "max_usd")}); return 200, r

@route("PUT", "/sandboxes/([^/]+)/ticket")
def h_ticket(h, m, body):           # the Ticket the harness works on (kernel ABI ticket_get); host side of the intake layer
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); sb.touch(); r = sb.vm.guest.call("ticket_set", ticket=body.get("ticket") or body)
    audit(op="ticket_set", sandbox=sb.id, ticket=(body.get("ticket") or body).get("id")); return 200, r

@route("POST", "/sandboxes/([^/]+)/inbox")
def h_inbox(h, m, body):            # one Input for the harness (kernel ABI inbox): {kind, text, actor?, thread?, ...}
    sb = get_sb(m.group(1))
    with sb.lock: sb.ensure_running(); sb.touch(); r = sb.vm.guest.call("abi_input", **{k: v for k, v in body.items() if k not in ("id", "op")})
    audit(op="abi_input", sandbox=sb.id, kind=body.get("kind")); return 201, r

@route("GET", "/sandboxes/([^/]+)/diff")
def h_diff(h, m, body):             # unified diff of a git checkout inside the sandbox: ?cwd=/work/repo&base=main (docs/008 diff 面板)
    sb = get_sb(m.group(1)); cwd = h.query.get("cwd", "/work/repo"); base = h.query.get("base", "main")
    if not re.fullmatch(r"[A-Za-z0-9_./~^-]{1,80}", base) or not cwd.startswith("/"): return 400, {"error": "bad base or cwd"}
    G = f"git -c safe.directory='*' -C {shlex.quote(cwd)}"; B = shlex.quote(base)
    cmd = f"M=$({G} merge-base {B} HEAD 2>/dev/null || echo {B}); {G} diff --stat \"$M\"; echo __DIFF__; {G} diff \"$M\"; echo __STATUS__; {G} status --porcelain | head -200"   # committed + working tree against the merge base
    with sb.lock: sb.ensure_running(); sb.touch(); r = sb.vm.guest.exec(cmd, user="root", timeout=60)
    out = r.get("stdout", ""); stat, _, rest = out.partition("__DIFF__\n"); diff, _, status = rest.partition("__STATUS__\n")
    return 200, {"exit": r.get("exit"), "stat": stat[-4000:], "diff": diff[-400000:], "status": status[-8000:], "stderr": r.get("stderr", "")[-500:], "base": base, "cwd": cwd}

def read_whole(guest, path, chunk=4 << 20, limit=1 << 30):
    """A whole file out of a guest. The guest's read_file returns at most `max_bytes` (1 MiB when not asked for more), so a file is read in chunks until one comes back short:
    a workspace tar of a real repository is a few MiB and used to arrive truncated at exactly 1 MiB, silently (the evaluator then failed to unpack it, or verified half a tree)."""
    out, off = [], 0
    while off < limit:
        d = guest.read_file(path, offset=off, max_bytes=chunk); out.append(d); off += len(d)
        if len(d) < chunk: break
    return b"".join(out)


@route("GET", "/sandboxes/([^/]+)/files")
def h_get_file(h, m, body):
    sb = get_sb(m.group(1)); path = h.query.get("path")
    with sb.lock: sb.ensure_running(); sb.touch(); data = read_whole(sb.vm.guest, path)
    return 200, data                  # bytes -> raw response

@route("POST", "/sandboxes/([^/]+)/fork")
def h_fork(h, m, body):
    sb = get_sb(m.group(1)); n = int(body.get("n", 1)); cls = body.get("class", "ephemeral")
    spec = {**sb.spec, "class": cls, "ttl_idle_s": int(body.get("ttl_idle_s", 600)), "ttl_max_s": int(body.get("ttl_max_s", 0)),
            "qos": body.get("qos", "be" if cls == "ephemeral" else sb.spec.get("qos", "normal"))}
    err = admit(spec, n)
    if err: return 429, {"error": err, **usage()}
    names = [f"{sb.id}-f{uuid.uuid4().hex[:4]}" for _ in range(n)]; ck = body.get("checkpoint")
    with sb.lock:
        if ck and ck not in [c["name"] for c in _ckpt_list(sb)]: return 404, {"error": f"no checkpoint {ck}"}
        sb.ensure_running(); sb.touch(); t0 = time.monotonic(); qos = spec["qos"] if cls != "ephemeral" else "be"
        if ck: clones = sb.vm.fork_from_checkpoint(ck, names, qos=qos, on_clone=lambda c: attach_net(c, c.name, spec))     # from a point in the past; the parent is not touched
        else: clones = sb.vm.fork(names, qos=qos, on_clone=lambda c: attach_net(c, c.name, spec))
    out = []
    for c in clones:
        c.guest.exec(f"hostname {c.name}", user="root")
        csb = Sandbox(c.name, spec, c); csb.boot_mode, csb.tpl_key = "fork", sb.tpl_key; save_spec(csb)
        with REG_LOCK: REG[c.name] = csb
        out.append(csb.info())
    audit(op="fork", sandbox=sb.id, clones=names, checkpoint=ck, s=round(time.monotonic() - t0, 3), phases=[getattr(c, "restore_phases", None) for c in clones])
    return 201, {"parent": sb.id, "clones": out, "checkpoint": ck, "s": round(time.monotonic() - t0, 3)}

@route("GET", "/lazy/stats")
def h_lazy_stats(h, m, body):
    st = lazy_stats()
    if st is None: return 404, {"error": "lazy loading not enabled"}
    sizes = {}
    for n in os.listdir(LAZY_MNT):
        if not n.startswith("."): sizes[n] = os.path.getsize(f"{LAZY_MNT}/{n}")
    return 200, {**st, "image_sizes": sizes}

@route("POST", "/lazy/prefetch")
def h_lazy_prefetch(h, m, body):
    """Warm the local cache with an image's boot profile (chunk indices) by reading those ranges."""
    import urllib.request
    name = body["image"]; t0 = time.monotonic()
    man = json.loads(urllib.request.urlopen(f"{LAZY_STORE}/images/{name}.json").read())
    idx = body.get("chunks") or man.get("profile", []); cs = man["chunk_size"]
    def rd(i):
        with open(f"{LAZY_MNT}/{name}", "rb") as f: f.seek(i * cs); f.read(cs)
    ths = []
    for k in range(0, len(idx), 8):            # 8 parallel fetchers
        th = threading.Thread(target=lambda ks=idx[k:k + 8]: [rd(i) for i in ks]); th.start(); ths.append(th)
    [t.join() for t in ths]
    audit(op="lazy_prefetch", image=name, chunks=len(idx), s=round(time.monotonic() - t0, 3))
    return 200, {"image": name, "chunks": len(idx), "s": round(time.monotonic() - t0, 3)}

@route("GET", "/lazy/profile/([^/]+)")
def h_lazy_profile(h, m, body):
    try: return 200, {"image": m.group(1), "chunks": json.load(open(f"{LAZY_MNT}/.profile/{m.group(1)}"))}
    except OSError: return 404, {"error": "no such image or lazy loading off"}

@route("POST", "/templates/snapshot")
def h_tpl_build(h, m, body):
    spec = new_spec({**body, "class": "ephemeral"}); key = tpl_key(spec)
    if key in TPL_SNAPS and not body.get("rebuild"): return 200, TPL_SNAPS[key]
    if DRAINING[0]: return 503, {"error": "node is draining"}
    err = admit(spec)
    if err: return 429, {"error": err, **usage()}
    return 201, build_template_snapshot(spec)

@route("PUT", "/templates/pool")
def h_pool_set(h, m, body):
    if DRAINING[0]: return 503, {"error": "node is draining"}
    want = int(body.get("ready", 1)); spec = new_spec({k: v for k, v in body.items() if k != "ready"})
    if want < 0 or want > MAX_VMS: raise ValueError("ready out of range")
    return 200, pool_set(spec, want)

@route("GET", "/templates/pool")
def h_pool_list(h, m, body): return 200, {"pools": [pool_info(k) for k in list(POOL)]}

@route("DELETE", "/templates/pool/([^/]+)")
def h_pool_drop(h, m, body): pool_drop(m.group(1)); return 200, {"deleted": m.group(1)}

@route("GET", "/templates")
def h_tpl_list(h, m, body): return 200, {"templates": list(TEMPLATES), "snapshots": list(TPL_SNAPS.values())}

@route("DELETE", "/templates/([^/]+)")
def h_tpl_delete(h, m, body):
    meta = TPL_SNAPS.pop(m.group(1), None)
    if not meta: raise KeyError(f"no template snapshot {m.group(1)}")
    if m.group(1) in POOL: pool_drop(m.group(1), "template_deleted")
    if STORE and body.get("drop_bundle"): imgstore.delete_bundle(STORE, f"tpl-{m.group(1)}")
    shutil.rmtree(meta["dir"], ignore_errors=True)       # sandboxes started from it hold hardlinks, they keep running
    try: os.unlink(f"{TPL_DIR}/{meta['key']}.json")
    except OSError: pass
    audit(op="template_snapshot_delete", key=meta["key"]); return 200, {"deleted": meta["key"]}

@route("GET", "/layers")
def h_layers(h, m, body): return 200, {"layers": list(LAYERS.values())}

@route("GET", "/layers/([^/]+)/export")
def h_layer_export(h, m, body):
    """One layer as a tar (GNU sparse: the ext4 image's holes stay holes) with its description, for another node's /layers/import (D37 补: layers travel)."""
    lid = m.group(1); meta = LAYERS.get(lid)
    if not meta: return 404, {"error": f"no layer {lid}"}
    d = os.path.dirname(meta["path"]); img = os.path.basename(meta["path"])
    desc = f"{STATE}/layers/{lid}.json"
    if not os.path.exists(desc): json.dump(meta, open(desc, "w"))
    data = subprocess.run(["tar", "-C", d, "-cS", "--transform", f"s|^.*/||", img, desc] if d != f"{STATE}/layers" else ["tar", "-C", d, "-cS", img, f"{lid}.json"],
                          check=True, capture_output=True).stdout
    audit(op="layer_export", layer=lid, bytes=len(data)); return 200, data

@route("POST", "/layers/import")
def h_layer_import(h, m, body):
    """The body is a tar from /layers/<id>/export; the layer is registered here under the same id (already present: 200, nothing done)."""
    import tarfile, tempfile, io
    if not isinstance(body, (bytes, bytearray)) or not body: return 400, {"error": "upload the exported layer tar as the body"}
    with tempfile.TemporaryDirectory(dir=f"{STATE}/layers") as tmp:
        subprocess.run(["tar", "-C", tmp, "-xS", "-f", "-"], input=bytes(body), check=True, capture_output=True)
        descs = [f for f in os.listdir(tmp) if f.endswith(".json")]
        if len(descs) != 1: return 400, {"error": "the tar must hold one layer description"}
        meta = json.load(open(f"{tmp}/{descs[0]}")); lid = str(meta.get("id") or "")
        if not re.match(r"^L-[0-9a-f]{8}$", lid) or not os.path.exists(f"{tmp}/{lid}.ext4"): return 400, {"error": "the tar must hold <id>.ext4 and <id>.json of one layer"}
        if lid in LAYERS: return 200, {**{k: v for k, v in LAYERS[lid].items() if k != "path"}, "present": True}
        img = f"{STATE}/layers/{lid}.ext4"; os.replace(f"{tmp}/{lid}.ext4", img)
        meta["path"] = img; meta["imported"] = time.time(); json.dump(meta, open(f"{STATE}/layers/{lid}.json", "w")); LAYERS[lid] = meta
    audit(op="layer_import", layer=lid, bytes=os.path.getsize(img)); return 201, {k: v for k, v in meta.items() if k != "path"}

@route("POST", "/sandboxes/([^/]+)/pack")
def h_pack(h, m, body):
    """pack_diff: turn this sandbox's writable layer into a new read-only layer (DSec §6.1)."""
    import subprocess, tempfile
    sb = get_sb(m.group(1)); name = body.get("name", "layer"); lid = f"L-{uuid.uuid4().hex[:8]}"
    with sb.lock:
        sb.ensure_running(); sb.touch(); t0 = time.monotonic(); g = sb.vm.guest
        try: pk = g.call("pack", user="root")
        except Exception as e:
            if "No space left" in str(e):     # the guest writes the tar into /run, a tmpfs of half its RAM: tell the caller what to change instead of passing the tar error on
                raise ValueError(f"the layer is too big to pack in this sandbox: the guest stages the tar in memory (about half of {sb.spec.get('mem')} MiB) and, on a runtime from D76, "
                                 f"in its disk ({sb.spec.get('disk')} MiB) when that is not enough; give it more disk (about twice the layer), or more mem on an older base") from e
            raise
        size = pk["bytes"]; t_tar = round(time.monotonic() - t0, 3)
        with tempfile.TemporaryDirectory(dir=f"{STATE}/layers") as tmp:
            tar = f"{tmp}/pack.tar"
            with open(tar, "wb") as f:
                off = 0
                while off < size:
                    data = g.read_file(pk["path"], offset=off, max_bytes=1 << 20); f.write(data); off += len(data)
                    if not data: break
            g.exec(f"rm -f {pk['path']}", user="root"); t_xfer = round(time.monotonic() - t0 - t_tar, 3)
            tree = f"{tmp}/tree"; os.makedirs(tree)
            subprocess.run(["tar", "--xattrs", "--xattrs-include=*", "--numeric-owner", "-C", tree, "-xf", tar], capture_output=True)
            used = int(subprocess.check_output(["du", "-sb", tree]).split()[0])
            img = f"{STATE}/layers/{lid}.ext4"; subprocess.check_call(["truncate", "-s", str(max(used * 2, 16 << 20) + (8 << 20)), img])
            subprocess.check_call(["mkfs.ext4", "-q", "-F", "-L", f"sbx-layer-{lid}", "-d", tree, img])
    meta = {"id": lid, "name": name, "path": img, "bytes": os.path.getsize(img), "tar_bytes": size, "from": sb.id,
            "created": time.time(), "s": round(time.monotonic() - t0, 3), "s_tar": t_tar, "s_xfer": t_xfer,
            "tar_warnings": pk.get("warnings", "")[-200:]}
    LAYERS[lid] = meta; json.dump(meta, open(f"{STATE}/layers/{lid}.json", "w"))
    audit(op="pack", sandbox=sb.id, layer=lid, bytes=meta["bytes"], s=meta["s"]); return 201, meta

@route("GET", "/audit")
def h_audit(h, m, body):
    n = int(h.query.get("n", "50"))
    with open(f"{STATE}/audit.jsonl", "rb") as f: lines = f.read().splitlines()[-n:]
    return 200, {"events": [json.loads(l) for l in lines]}


def make_server(port, cert=None, key=None):
    """The node's HTTP server; with TLS_CERT / TLS_KEY it speaks HTTPS (D62: the control plane <-> node hop carries bearer tokens and sandbox contents)."""
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.minimum_version = ssl.TLSVersion.TLSv1_2; ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    return srv


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rbufsize = 0                                     # unbuffered reads: an upgraded connection (ports) hands the raw socket on with nothing held back
    def _body(self, n):
        """Exactly n bytes of body: an unbuffered rfile returns what one recv gave, so a big upload (a layer archive) needs the loop."""
        parts = []; left = n
        while left > 0:
            d = self.rfile.read(left)
            if not d: break
            parts.append(d); left -= len(d)
        return b"".join(parts)
    def log_message(self, *a): pass
    def _dispatch(self, method):
        path, _, qs = self.path.partition("?")
        self.query = dict(urllib.parse.parse_qsl(qs, keep_blank_values=True))        # decoded: ?cwd=%2Fwork%2Frepo works
        if self.headers.get("Authorization") == f"Bearer {NODE_TOKEN}": LAST_CP[0] = time.time()   # the control plane's health poll carries the token: that is "it can still reach me"
        if path not in PUBLIC_ROUTES and self.headers.get("Authorization") != f"Bearer {NODE_TOKEN}":
            n = int(self.headers.get("Content-Length") or 0); self._body(n)
            return self._send(401, {"error": "missing or invalid bearer token"})
        em = ENVD_RE.match(path)
        if em:                                           # relayed unread: the request body (Content-Length or chunked) is still on the socket
            try: return proxy_envd(self, em.group(1), em.group(2), method, qs)
            except KeyError as e: return self._send(404, {"error": str(e)})
        n = int(self.headers.get("Content-Length") or 0); raw = self._body(n)
        pm = PORT_RE.match(path)
        if pm:
            try: return proxy_port(self, pm.group(1), pm.group(2), pm.group(3), method, raw, qs)
            except KeyError as e: return self._send(404, {"error": str(e)})
        for meth, rx, fn in ROUTES:
            mm = rx.match(path)
            if meth == method and mm:
                try:
                    body = raw if (method == "PUT" and path.endswith("/files")) or path in ("/layers/from-archive", "/layers/import", "/volumes/from-archive", "/volumes/import") else (json.loads(raw) if raw else {})
                    res = fn(self, mm, body)
                    if res is None: return          # handler streamed its own response
                    code, out = res
                except KeyError as e: code, out = 404, {"error": str(e)}
                except (ValueError, RuntimeError) as e: code, out = 400, {"error": str(e)}
                except Exception as e:  # noqa: BLE001
                    code, out = 500, {"error": f"{type(e).__name__}: {e}"}; audit(op="error", path=path, error=out["error"])
                return self._send(code, out)
        self._send(404, {"error": "no route"})
    def begin_stream(self):
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked"); self.end_headers(); self._streaming = True
    def chunk(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n"); self.wfile.flush()
    def end_stream(self):
        self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
    def _send(self, code, out):
        data = out if isinstance(out, bytes) else json.dumps(out, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/octet-stream" if isinstance(out, bytes) else "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def do_PUT(self): self._dispatch("PUT")
    def do_DELETE(self): self._dispatch("DELETE")
    def do_PATCH(self): self._dispatch("PATCH")
    def do_HEAD(self): self._dispatch("HEAD")


def main():
    if LAZY_STORE: start_lazyfs()
    if JAIL: setup_cgroups()
    netmgr.ensure_base_rules()
    egress_proxy.start(netmgr.PROXY_PORT, lambda ev: audit(**ev))
    recover()
    signal.signal(signal.SIGTERM, lambda *a: threading.Thread(target=drain, daemon=True).start())
    threading.Thread(target=ttl_loop, daemon=True).start()
    if FENCE_AFTER_S > 0: threading.Thread(target=fence_loop, daemon=True).start()
    threading.Thread(target=metrics_loop, daemon=True).start()
    threading.Thread(target=pool_fill_loop, daemon=True).start()
    def warm():
        for entry in TEMPLATE_POOL:
            try: pool_set(new_spec({k: v for k, v in entry.items() if k != "ready"}), int(entry.get("ready", 1)))
            except Exception as e: audit(op="pool_warm_error", entry=entry, error=repr(e)[:200])   # noqa: BLE001
    if TEMPLATE_POOL: threading.Thread(target=warm, daemon=True).start()
    audit(op="node_start", port=PORT, max_vms=MAX_VMS, mem_budget_mib=MEM_BUDGET, jail=JAIL, lazy_store=LAZY_STORE, uffd=UFFD_OK, uffd_note=UFFD_WHY, template_snapshots=list(TPL_SNAPS))
    print(f"nodeagent listening on :{PORT}  state={STATE}  images={IMAGES}  auth=bearer({STATE}/node-token)", flush=True)
    make_server(PORT, os.environ.get("TLS_CERT"), os.environ.get("TLS_KEY")).serve_forever()


if __name__ == "__main__":
    main()
