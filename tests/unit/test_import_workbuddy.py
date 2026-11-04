"""WorkBuddy importer tests: workspace size filtering and import behavior."""
import json
import os
import sys
import tarfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "eval"))
import import_workbuddy as iw
if iw.tomllib is None:  # pragma: no cover
    pytest.skip("needs tomllib (Python 3.11+) to parse task.toml", allow_module_level=True)


@pytest.mark.parametrize(
    "size, expected",
    [
        (iw.WORKSPACE_MAX - 1, False),
        (iw.WORKSPACE_MAX, False),
        (iw.WORKSPACE_MAX + 1, True),
    ],
)
def test_workspace_too_large_uses_workspace_max(size, expected):
    """The default WORKSPACE_MAX boundary is inclusive for retaining a workspace."""
    assert iw.workspace_too_large(size) is expected


def test_workspace_too_large_accepts_an_explicit_limit():
    assert iw.workspace_too_large(11, limit=10) is True
    assert iw.workspace_too_large(10, limit=10) is False


def _write_task(src, name, workspace_content):
    """Create the minimum WorkBuddy task layout needed by the importer."""
    task = os.path.join(src, "tasks", name)
    os.makedirs(os.path.join(task, "tests"))
    os.makedirs(os.path.join(task, "environment"))
    with open(os.path.join(task, "tests", "verifier.toml"), "w", encoding="utf-8") as f:
        f.write('family = "script_verifier"\n')
    with open(os.path.join(task, "tests", "verifier.py"), "w", encoding="utf-8") as f:
        f.write("print('verifier')\n")
    with open(os.path.join(task, "tests", "gold.patch"), "w", encoding="utf-8") as f:
        f.write(
            "diff --git a/reference.txt b/reference.txt\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
            "--- /dev/null\n"
            "+++ b/reference.txt\n"
            "@@ -0,0 +1 @@\n"
            "+gold\n"
        )
    with open(os.path.join(task, "environment", "Dockerfile"), "w", encoding="utf-8") as f:
        f.write("FROM python:3.12-slim\n")
    with open(os.path.join(task, "task.toml"), "w", encoding="utf-8") as f:
        f.write('[metadata]\ndifficulty = "medium"\ncategory = "test"\n')
    with open(os.path.join(task, "instruction.md"), "w", encoding="utf-8") as f:
        f.write("Make the test pass.\n")
    payload = os.path.join(task, "payload.txt")
    with open(payload, "wb") as f:
        f.write(workspace_content)
    with tarfile.open(os.path.join(task, "environment", "workspace.tar.gz"), "w:gz") as tf:
        tf.add(payload, arcname="payload.txt")


def test_import_skips_over_limit_workspaces_and_keeps_smaller_ones(tmp_path, monkeypatch):
    """A task over the size threshold is omitted; one at/below it is imported."""
    src = tmp_path / "src"
    out = tmp_path / "out"
    _write_task(src, "large", b"x" * 20)
    _write_task(src, "small", b"tiny\n")

    # Keep the fixture small while exercising the importer's real skip/keep path.
    monkeypatch.setattr(iw, "workspace_too_large", lambda size, limit=iw.WORKSPACE_MAX: size > 10)

    assert iw.main([str(src), str(out)]) == 0

    taskset = json.loads((out / "taskset.json").read_text(encoding="utf-8"))
    assert taskset["tasks"] == ["wb-small"]
    assert not (out / "wb-large").exists()
    assert (out / "wb-small" / "init" / "payload.txt").read_text(encoding="utf-8") == "tiny\n"
    assert (out / "wb-small" / "reference" / "reference.txt").read_text(encoding="utf-8") == "gold\n"
