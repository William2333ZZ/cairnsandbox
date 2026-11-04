import os, sys, tempfile
os.environ["STATE_DIR"] = tempfile.mkdtemp(); os.environ["IMAGES_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "node"))
import pytest
import nodeagent as na


def test_defaults_persistent():
    s = na.new_spec({})
    assert s["class"] == "persistent" and s["qos"] == "normal" and s["ttl_idle_s"] == 1800
    assert s["network"] == {"mode": "closed", "allow_hosts": [], "allow_direct": [], "rules": [], "limits": {"requests_per_min": 0, "max_bytes": 0}}      # D36: closed when nothing is allowed, no L7 rules, no limits
    assert s["retain_paused_s"] == 0                                                                   # 003: a paused sandbox is kept until told otherwise


def test_env_is_a_small_dict_of_names_to_strings():
    assert na.new_spec({"env": {"FOO": "bar", "_X1": ""}})["env"] == {"FOO": "bar", "_X1": ""} and na.new_spec({})["env"] == {}
    for bad in ({"env": ["A=1"]}, {"env": {"1A": "x"}}, {"env": {"A": 1}}, {"env": {"A-B": "x"}}):
        with pytest.raises(ValueError): na.new_spec(bad)


def test_network_limits_and_retention_are_validated():
    s = na.new_spec({"network": {"limits": {"requests_per_min": 30}}, "retain_paused_s": 3600})
    assert s["network"]["limits"] == {"requests_per_min": 30, "max_bytes": 0} and s["retain_paused_s"] == 3600
    for bad in ({"network": {"limits": {"max_bytes": -1}}}, {"network": {"limits": {"x": 1}}}, {"retain_paused_s": -5}):
        with pytest.raises(ValueError): na.new_spec(bad)


def test_ephemeral_is_best_effort_with_short_ttl():
    s = na.new_spec({"class": "ephemeral"})
    assert s["qos"] == "be" and s["ttl_idle_s"] == 600


def test_rejects_bad_inputs():
    with pytest.raises(ValueError): na.new_spec({"template": "nope"})
    with pytest.raises(ValueError): na.new_spec({"class": "forever"})
    with pytest.raises(ValueError): na.new_spec({"qos": "turbo"})
    with pytest.raises(ValueError): na.new_spec({"layers": ["L-missing"]})


def test_admission_budgets():
    na.REG.clear()
    assert na.admit(na.new_spec({"mem": 512})) is None
    assert "memory budget" in na.admit(na.new_spec({"mem": na.MEM_BUDGET + 1}))
    assert "vcpu budget" in na.admit(na.new_spec({"vcpu": 10**6}))


def test_node_token_is_generated_and_persisted():
    p = f"{na.STATE}/node-token"
    assert os.path.exists(p) and oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert len(na.NODE_TOKEN) == 64 and na._node_token() == na.NODE_TOKEN


def test_boot_mode_validated_and_defaults_to_auto():
    assert na.new_spec({})["boot"] == "auto" and na.new_spec({"boot": "cold"})["boot"] == "cold"
    with pytest.raises(ValueError): na.new_spec({"boot": "warm"})


def test_template_key_depends_on_guest_config_only():
    a, b = na.new_spec({"mem": 512}), na.new_spec({"mem": 512, "class": "ephemeral", "ttl_idle_s": 5, "qos": "ls"})
    assert na.tpl_key(a) == na.tpl_key(b)                     # class/ttl/qos are not part of the guest image+devices
    assert na.tpl_key(a) != na.tpl_key(na.new_spec({"mem": 1024}))
    assert na.tpl_key(a) != na.tpl_key(na.new_spec({"mem": 512, "balloon": False}))


def test_create_refused_while_draining():
    na.DRAINING[0] = True
    try:
        with pytest.raises(PermissionError): na.create(na.new_spec({}))
    finally: na.DRAINING[0] = False


def test_disk_size_in_spec_and_template_key():
    import nodeagent
    s = nodeagent.new_spec({"disk": 2048}); assert s["disk"] == 2048
    assert nodeagent.new_spec({})["disk"] == 512
    try: nodeagent.new_spec({"disk": 64}); assert False
    except ValueError: pass
    assert nodeagent.tpl_key(nodeagent.new_spec({})) != nodeagent.tpl_key(nodeagent.new_spec({"disk": 2048}))


def test_a_file_bigger_than_one_read_comes_out_whole():
    """The guest returns at most max_bytes per read; a 5 MiB workspace tar used to arrive as its first 1 MiB."""
    class G:
        def __init__(self, data): self.data = data; self.calls = 0
        def read_file(self, path, offset=0, max_bytes=1 << 20): self.calls += 1; return self.data[offset: offset + max_bytes]
    for n in (0, 10, (4 << 20) - 1, 4 << 20, (4 << 20) + 1, 11 << 20):
        data = os.urandom(n); g = G(data); assert na.read_whole(g, "/tmp/x") == data, n
    g = G(b"x" * (9 << 20)); na.read_whole(g, "/tmp/x"); assert g.calls == 3


def test_labels_are_a_small_dict_of_strings_kept_and_reported():
    s = na.new_spec({"labels": {"project": "acme", "meta.purpose": "ci"}})
    assert s["labels"] == {"project": "acme", "meta.purpose": "ci"} and na.new_spec({})["labels"] == {}
    for bad in ({"labels": ["a"]}, {"labels": {"a": 1}}, {"labels": {"": "x"}}, {"labels": {"k": "v" * 2000}}, {"labels": {f"k{i}": "v" for i in range(65)}}):
        with pytest.raises(ValueError): na.new_spec(bad)


class _Sb:
    def __init__(self, sid, cls, status):
        import threading, types
        self.id, self.lock = sid, threading.RLock(); self.vm = types.SimpleNamespace(status=status); self.spec = {"class": cls}


def test_a_fenced_node_pauses_persistent_sandboxes_and_destroys_the_rest(monkeypatch):
    """D54: the control plane cannot power a node off, so a node it cannot reach stops its own sandboxes before anyone takes them over."""
    reg = {"p": _Sb("p", "persistent", "running"), "e": _Sb("e", "ephemeral", "running"), "z": _Sb("z", "persistent", "paused")}
    seen = []
    monkeypatch.setattr(na, "REG", reg)
    monkeypatch.setattr(na, "do_pause", lambda sb, why: seen.append(("pause", sb.id, why)))
    monkeypatch.setattr(na, "destroy", lambda sb, why="api": seen.append(("destroy", sb.id, why)))
    na.fence_all()
    assert sorted(seen) == [("destroy", "e", "fence"), ("pause", "p", "fence_pause")]      # the already-paused one is left alone


def test_cgroup_samples_become_cpu_percent_and_memory(tmp_path):
    (tmp_path / "cpu.stat").write_text("usage_usec 2000000\nuser_usec 1500000\nsystem_usec 500000\n"); (tmp_path / "memory.current").write_text(str(256 * 1048576))
    cur = na.read_cgroup(str(tmp_path)); assert cur == (2_000_000, 256 * 1048576)
    first = na.metrics_point(None, cur, 100.0); assert first["cpu_pct"] is None and first["mem_mib"] == 256.0
    nxt = na.metrics_point((100.0, 1_000_000), cur, 105.0, {"out": 10, "in": 20})
    assert nxt["cpu_pct"] == 20.0 and nxt["egress_out"] == 10 and nxt["egress_in"] == 20      # 1 CPU-second in 5 s = 20 % of one core
    assert na.read_cgroup(str(tmp_path / "nope")) is None


def test_the_node_serves_https_when_given_a_certificate(tmp_path):
    """D62: the control plane <-> node hop can be TLS; a client that trusts the certificate gets /health, one that does not is refused."""
    import shutil, ssl, subprocess, threading, urllib.error, urllib.request
    if not shutil.which("openssl"): pytest.skip("no openssl")
    crt, key = str(tmp_path / "n.crt"), str(tmp_path / "n.key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                    "-keyout", key, "-out", crt], check=True, capture_output=True)
    srv = na.make_server(0, crt, key); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ctx = ssl.create_default_context(cafile=crt)
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/health", context=ctx, timeout=10) as r: assert r.status == 200
        with pytest.raises(urllib.error.URLError): urllib.request.urlopen(f"https://127.0.0.1:{port}/health", context=ssl.create_default_context(), timeout=10)
    finally: srv.shutdown(); srv.server_close()


def test_a_named_template_fills_the_guest_config_the_caller_left_out():
    na.TPL_SNAPS["dev-x"] = {"key": "dev-x", "template": na.DEFAULT_TEMPLATE, "vcpu": 4, "mem": 3072, "disk": 4096, "layers": [], "pmem_base": False, "balloon": True}
    try:
        s = na.new_spec({"template_key": "dev-x"})
        assert (s["vcpu"], s["mem"], s["disk"], s["pmem_base"], s["template_key"]) == (4, 3072, 4096, False, "dev-x")
        assert na.new_spec({"template_key": "dev-x", "mem": 512})["mem"] == 512        # what the caller does say still wins (and resolve_template_key then refuses a mismatch)
        assert na.new_spec({"template_key": "0123456789abcdef"})["mem"] == 512          # a hex placement hint is not a template
    finally: na.TPL_SNAPS.pop("dev-x", None)


def test_oom_note_finds_a_recent_kill_and_ignores_old_ones():
    dm = ("[   50.000001] Out of memory: Killed process 11 (old) total-vm:1kB\n"
          "[  126.829312] Out of memory: Killed process 5674 (govulncheck) total-vm:3733844kB, anon-rss:1918428kB\n")
    assert na.oom_note("130.50 99.1\n", dm, 10) == {"pid": 5674, "process": "govulncheck"}
    assert na.oom_note("300.00 1.0\n", dm, 10) is None                   # the kill was minutes ago: not this command
    assert na.oom_note("130.50 99.1\n", "", 10) is None and na.oom_note("", dm, 10) is None


def test_a_named_template_at_another_size_needs_boot_cold_and_says_so():
    na.TPL_SNAPS["dev-y"] = {"key": "dev-y", "template": na.DEFAULT_TEMPLATE, "vcpu": 2, "mem": 2048, "disk": 4096, "layers": [], "pmem_base": False, "balloon": True}
    try:
        s = na.new_spec({"template_key": "dev-y", "mem": 4096})
        assert s["mem"] == 4096 and s["disk"] == 4096 and s["vcpu"] == 2       # the rest still comes from the template
        with pytest.raises(ValueError) as e: na.resolve_template_key(s)
        assert "mem=2048" in str(e.value) and "boot" in str(e.value) and "cold" in str(e.value)
        s = na.new_spec({"template_key": "dev-y", "mem": 4096, "boot": "cold"})
        assert na.resolve_template_key(s) == na.tpl_key(s)                   # not the template's snapshot: a cold boot
    finally: na.TPL_SNAPS.pop("dev-y", None)
