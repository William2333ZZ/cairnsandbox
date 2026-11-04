# Team prompt sections (dsh-superagent 004): the words a team writes to shape how the agent works, kept apart from the rules the host enforces.
# Three sections the team can write; every save is an immutable version; a section left alone follows the built-in text. What the host enforces (the gate, Mandates, approvals, the way to ask or hand off,
# the review) is not here and no text here can change it: this is advice the agent reads, never a permission.
import hashlib
from config import *

SECTIONS = {
    "communication": ("沟通", "Do not report progress: the user hears from you only when you are done, stuck, or need a decision."),
    "scope": ("范围", ""),
    "delivery": ("交付", ""),
}
LIMIT_ONE, LIMIT_ALL = 3000, 8000


def _h(text): return hashlib.sha256((text or "").encode()).hexdigest()[:8]


class PromptsMixin:
    def _prompt_store(self): return self.store.kv_get("prompts", {}) or {}

    def prompt_overrides(self):
        """{section: text} of the sections a team has written (the built-in text is not repeated): what goes into every ticket."""
        out = {}
        for name, versions in self._prompt_store().items():
            if name in SECTIONS and versions and versions[-1].get("text") is not None: out[name] = versions[-1]["text"]
        return out

    def prompts_view(self):
        st = self._prompt_store(); sections = {}
        for name, (label, default) in SECTIONS.items():
            vs = st.get(name) or []; cur = vs[-1] if vs else None
            sections[name] = {"label": label, "default": default, "text": cur["text"] if cur else default, "overridden": bool(cur and cur.get("text") is not None), "version": len(vs),
                              "default_updated": bool(cur and cur.get("default_hash") != _h(default)), "history": [{"n": i + 1, "by": v.get("by"), "at": v.get("at"), "size": len(v.get("text") or ""), "reset": v.get("text") is None} for i, v in enumerate(vs)][-20:]}
        return {"sections": sections, "limit": LIMIT_ONE, "limit_all": LIMIT_ALL}

    def prompts_save(self, name, text, base=None, actor="panel"):
        """Save a section as a new version. `base` is the version the editor started from: a save on top of someone else's is refused, the text is not lost to the caller."""
        if name not in SECTIONS: raise ValueError(f"unknown section {name!r}; known: {', '.join(SECTIONS)}")
        if not isinstance(text, str): raise ValueError("text must be a string")
        text = text.strip()
        if len(text) > LIMIT_ONE: raise ValueError(f"too long: {len(text)} characters, the limit is {LIMIT_ONE}")
        with self.lock:
            st = self._prompt_store(); vs = list(st.get(name) or [])
            if base is not None and int(base) != len(vs): raise ValueError(f"conflict: {name} is at version {len(vs)}, you started from {base}")
            default = SECTIONS[name][1]
            if text == default.strip() or (not text and not default):                                   # the same as the built-in text is no override
                if not vs or vs[-1].get("text") is None: return self.prompts_view()
                vs.append({"text": None, "by": actor, "at": now(), "default_hash": _h(default)})
            else:
                total = sum(len(v) for k, v in self.prompt_overrides().items() if k != name) + len(text)
                if total > LIMIT_ALL: raise ValueError(f"all sections together may be {LIMIT_ALL} characters; this would be {total}")
                vs.append({"text": text, "by": actor, "at": now(), "default_hash": _h(default)})
            st[name] = vs[-50:]; self.store.kv_set("prompts", st)
        return self.prompts_view()

    def prompts_reset(self, name, actor="panel"):
        if name not in SECTIONS: raise ValueError(f"unknown section {name!r}")
        return self.prompts_save(name, SECTIONS[name][1], actor=actor)
