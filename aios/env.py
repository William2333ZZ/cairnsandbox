"""Local bash environment: every action runs in a fresh subshell with a timeout; the whole process group is killed on
timeout. Adapted from mini-swe-agent environments/local.py (MIT)."""
import os, platform, signal, subprocess
from dataclasses import dataclass, field

SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


class Submitted(Exception):
    def __init__(self, submission): self.submission = submission; super().__init__("submitted")


@dataclass
class EnvConfig:
    cwd: str = ""
    env: dict = field(default_factory=dict)
    timeout: int = 60


class LocalEnvironment:
    def __init__(self, **kw): self.config = EnvConfig(**kw)

    def execute(self, action: dict, cwd: str = "", timeout: int | None = None) -> dict:
        command = action.get("command", ""); cwd = cwd or self.config.cwd or os.getcwd()
        try:
            out, rc = _run(command, cwd, os.environ | self.config.env, timeout or self.config.timeout)
            output = {"output": out, "returncode": rc, "exception_info": ""}
        except Exception as e:  # noqa: BLE001
            raw = getattr(e, "output", None) or ""
            output = {"output": raw if isinstance(raw, str) else raw.decode("utf-8", "replace"), "returncode": -1,
                      "exception_info": f"An error occurred while executing the command: {e}",
                      "extra": {"exception_type": type(e).__name__}}
        lines = output["output"].lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == SUBMIT_MARKER and output["returncode"] == 0: raise Submitted("".join(lines[1:]))
        return output

    def get_template_vars(self) -> dict:
        u = platform.uname(); return {**self.config.__dict__, "system": u.system, "release": u.release, "version": u.version, "machine": u.machine}

    def serialize(self) -> dict: return {"info": {"config": {"environment": dict(self.config.__dict__), "environment_type": "aios.env.LocalEnvironment"}}}


def _run(command, cwd, env, timeout):
    env = {k: v for k, v in env.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH", "PYTHONDONTWRITEBYTECODE")}   # the harness's own interpreter settings stay out of the task's shell
    p = subprocess.Popen(command, shell=True, text=True, cwd=cwd, env=env, encoding="utf-8", errors="replace",
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try: out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL); out, _ = p.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=out)
    return out, p.returncode
