"""scripts/lib/verify.sh: the download checks bootstrap.sh and the kernel recipe rely on (file:// URLs, so no network)."""
import hashlib, os, subprocess, tempfile

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def sh(script, **env):
    return subprocess.run(["bash", "-c", f". {ROOT}/scripts/lib/verify.sh; {script}"], capture_output=True, text=True, env={**os.environ, **env})


def setup(content=b"the payload", name="thing.tgz", wrong=False):
    d = tempfile.mkdtemp(); open(f"{d}/{name}", "wb").write(content)
    digest = hashlib.sha256(b"something else" if wrong else content).hexdigest()
    open(f"{d}/SUMS", "w").write(f"deadbeef  other.tgz\n{digest}  {name}\n"); return d, digest


def test_a_matching_download_is_kept():
    d, _ = setup(); out = f"{d}/out"
    r = sh(f"fetch_verified file://{d}/thing.tgz file://{d}/SUMS thing.tgz {out}"); assert r.returncode == 0, r.stderr
    assert open(out, "rb").read() == b"the payload"


def test_a_corrupted_download_is_refused_and_removed():
    d, _ = setup(wrong=True); out = f"{d}/out"
    r = sh(f"fetch_verified file://{d}/thing.tgz file://{d}/SUMS thing.tgz {out}"); assert r.returncode != 0 and "mismatch" in r.stderr
    assert not os.path.exists(out), "an unverified file must not be left behind"


def test_a_pinned_digest_wins_over_the_published_sums():
    d, digest = setup(wrong=True); out = f"{d}/out"           # the published file disagrees, the pin is right
    good = hashlib.sha256(b"the payload").hexdigest()
    assert sh(f"fetch_verified file://{d}/thing.tgz file://{d}/SUMS thing.tgz {out} {good}").returncode == 0
    assert sh(f"fetch_verified file://{d}/thing.tgz file://{d}/SUMS thing.tgz {out}2 {'0' * 64}").returncode != 0


def test_a_name_missing_from_the_sums_file_is_an_error():
    d, _ = setup(); r = sh(f"fetch_verified file://{d}/thing.tgz file://{d}/SUMS nothere.tgz {d}/out")
    assert r.returncode != 0 and "no checksum" in r.stderr and not os.path.exists(f"{d}/out")


def test_sums_lines_with_a_star_for_binary_mode_are_understood():
    d = tempfile.mkdtemp(); open(f"{d}/S", "w").write("abc123 *file.bin\n")
    assert sh(f"expected_from_sums {d}/S file.bin").stdout.strip() == "abc123"


def test_the_scripts_parse():
    for f in ("scripts/bootstrap.sh", "scripts/lib/verify.sh", "recipes/kernel-6.18/build.sh"):
        assert subprocess.run(["bash", "-n", f"{ROOT}/{f}"]).returncode == 0, f
