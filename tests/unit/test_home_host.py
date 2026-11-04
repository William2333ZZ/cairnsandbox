"""The host's side of the personal harness directory (intake/home.py, docs/004, D29): one directory per user under the state dir, shipped read-only into every sandbox at
/work/home, named by the same digest the runtime computes; the person edits it file by file and the next assignment carries the change; nothing ships when it is empty."""
import io, json, os, sys, tarfile
import pytest
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from test_intake_engine import engine, wait, I  # noqa: E402
from aios import home as H  # noqa: E402


def run_one(e):
    e.receive(I.workbench_trigger({"text": "please fix"})); e.pass_once()
    assert wait(e, lambda: e.store.all("tickets") and e.store.all("tickets")[-1]["state"] == "ended")
    return e.store.all("tickets")[-1]


def test_an_empty_directory_ships_nothing_and_the_ticket_says_so():
    e = engine(); assert e.home_view()["digest"] == "" and e.home_tar() is None
    run_one(e)
    assert ("put", "/tmp/home.tar") not in e.cp.calls and e.cp.last_ticket["home"] == ""
    a = e.store.all("assignments")[-1]; assert a["home"] == ""


def test_the_directory_is_shipped_read_only_named_by_the_runtimes_digest_and_a_persons_edit_rides_the_next_assignment():
    e = engine()
    v = e.home_write("rules/10-ci.md", "Never touch the CI files.\n")
    e.home_write("memory.md", "<!-- notes -->\nThe runner is pytest.\n"); e.home_write("skills/release/SKILL.md", "---\nname: release\ndescription: cut a release\n---\n1. tag\n")
    assert [f["path"] for f in e.home_view()["files"]] == ["memory.md", "rules/10-ci.md", "skills/release/SKILL.md"] and v["in_sandbox"] == "/work/home"
    assert e.home_digest() == H.Home(e.home_dir()).digest()                                     # host and runtime agree on the version
    run_one(e)
    assert ("put", "/tmp/home.tar") in e.cp.calls
    tar_exec = [c for c in e.cp.calls if c[0] == "exec" and c[1] == "root" and c[2].startswith("rm -rf /work/home")]; assert tar_exec, e.cp.calls
    assert e.cp.last_ticket["home"] == e.home_digest() and e.store.all("assignments")[-1]["home"] == e.home_digest()
    tk = e.store.all("tickets")[-1]; assert e.ticket_view(tk["id"])["assignments"][0]["home"] == e.home_digest()
    with tarfile.open(fileobj=io.BytesIO(e.home_tar())) as tf:                                   # what travels: only the files the runtime reads, owned by root, not writable
        names = sorted(m.name for m in tf.getmembers()); assert names == ["memory.md", "rules/10-ci.md", "skills/release/SKILL.md"]
        assert all(m.uid == 0 and m.mode == 0o644 for m in tf.getmembers())
    d0 = e.home_digest(); e.home_write("rules/20-tests.md", "Run the narrowest test first.\n"); assert e.home_digest() != d0
    run_one(e); assert e.cp.last_ticket["home"] == e.home_digest() != d0
    hist = [json.loads(l) for l in open(os.path.join(e.home_dir(), "history.jsonl"))]; assert [h["op"] for h in hist] == ["write"] * 4 and hist[-1]["digest"] == e.home_digest()


def test_tasks_and_proposals_stay_on_the_host():
    e = engine(); e.home_write("rules/01.md", "Keep.")
    root = e.home_dir(); os.makedirs(os.path.join(root, "tasks", "repo", "t1")); open(os.path.join(root, "tasks", "repo", "t1", "prompt.md"), "w").write("x")
    os.makedirs(os.path.join(root, "proposals", "p1")); open(os.path.join(root, "proposals", "p1", "memory.md"), "w").write("machine")
    with tarfile.open(fileobj=io.BytesIO(e.home_tar())) as tf: assert [m.name for m in tf.getmembers()] == ["rules/01.md"]
    assert e.home_view()["proposals"] == ["p1"] and e.home_digest() == H.Home(root).digest()


def test_edits_are_fenced_to_what_the_runtime_reads_and_a_broken_config_is_refused_before_it_lands():
    e = engine()
    for bad in ("../x.md", "rules/../../etc/passwd", "tasks/r/x", "proposals/p/memory.md", "history.jsonl", "", "rules/", "rules/a b.md"):
        with pytest.raises(ValueError): e.home_write(bad, "x")
    with pytest.raises(ValueError): e.home_write("config.json", "{broken")
    assert e.home_files() == [] and not os.path.exists(os.path.join(e.home_dir(), "config.json"))
    e.home_write("config.json", json.dumps({"agnet": {}})); assert e.home_problems() == ["config.json: unknown top-level key 'agnet' (agent, environment, model)"]
    e.home_write("prompts/instance.md", "no task"); assert "prompts/instance.md must contain {{task}}" in e.home_view()["problems"]
    assert e.home_read("prompts/instance.md")["text"] == "no task"
    with pytest.raises(KeyError): e.home_read("rules/none.md")
    with pytest.raises(KeyError): e.home_delete("rules/none.md")
    e.home_delete("prompts/instance.md"); assert e.home_problems() == ["config.json: unknown top-level key 'agnet' (agent, environment, model)"]
    assert H.Home(e.home_dir()).check() == e.home_problems()                                     # the host's early check names the same problems the runtime would stop on


def test_one_directory_per_user_shared_by_their_agents(tmp_path):
    import tempfile
    d = tempfile.mkdtemp(); cfg = I.cfg_from_env(); cfg["state"] = d; cfg["layers"] = ["L-a"]; cfg["merge_s"] = 0
    from test_intake_engine import FakeCP, FakeGitea
    a = I.Engine({**cfg, "user": "alice", "owner": "alice", "agent": "main"}, cp=FakeCP(), proxy=None, gitea=FakeGitea())
    fix = I.Engine({**cfg, "user": "alice/fix", "owner": "alice", "agent": "fix"}, cp=FakeCP(), proxy=None, gitea=FakeGitea())
    bob = I.Engine({**cfg, "user": "bob", "owner": "bob", "agent": "main"}, cp=FakeCP(), proxy=None, gitea=FakeGitea())
    a.home_write("rules/01.md", "Alice's rule.")
    assert fix.home_dir() == a.home_dir() and fix.home_digest() == a.home_digest() != "" and bob.home_digest() == "" and bob.home_dir() != a.home_dir()


def test_volume_mode_makes_one_read_only_volume_per_digest_and_drops_old_ones():
    e = engine(); e.cfg["home_volume"] = True; e.cp.vols = []
    e.home_write("rules/01.md", "Rule one.")
    run_one(e)
    spec = [c[1] for c in e.cp.calls if c[0] == "create"][-1]
    assert spec["volumes"] == [{"id": "vol-1", "mount": "/work/home", "ro": True}] and ("put", "/tmp/home.tar") not in e.cp.calls     # the volume replaces the tar
    assert e.store.all("assignments")[-1]["home_volume"] == "vol-1" and e.cp.last_ticket["home"] == e.home_digest()
    names = [c[1] for c in e.cp.calls if c[0] == "volume_from_archive"]; assert names == [f"home-{e.home_digest()[2:]}"]
    run_one(e)
    assert [c[1] for c in e.cp.calls if c[0] == "volume_from_archive"] == names                                                  # same digest: reused
    e.home_write("rules/02.md", "Rule two.")
    run_one(e)
    created = [c[1] for c in e.cp.calls if c[0] == "volume_from_archive"]; assert len(created) == 2 and created[1] == f"home-{e.home_digest()[2:]}"
    assert ("volume_delete", "vol-1") in e.cp.calls and [v["id"] for v in e.cp.vols] == ["vol-2"]                                 # the old version is gone once nothing holds it
    e.cfg["home_volume"] = False
    run_one(e); assert ("put", "/tmp/home.tar") in e.cp.calls                                                                      # tar mode is unchanged


def test_the_service_names_its_project_when_configured():
    e = engine(); run_one(e)
    assert "project" not in [c[1] for c in e.cp.calls if c[0] == "create"][-1]                    # no INTAKE_PROJECT: the token's default project
    e.cfg["project"] = "acme"; run_one(e)
    assert [c[1] for c in e.cp.calls if c[0] == "create"][-1]["project"] == "acme"                 # D33 补: usage and quotas land on this Project
