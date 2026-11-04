# Things a person says to their agent about the agent itself (docs/001 第 1、2 阶段: "没有设置页 ... 都能在对话里改" and "接入了什么,在对话里问它就能得到清单").
# Deterministic phrases, handled by the host and never turned into a job: a budget change, "what are you connected to". Only a person the policy lists, only when
# the message is addressed to the agent; the answer is a note in the conversation, so it is also the record of what changed.
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from config import *

AMOUNT = r"\$?\s*(\d+(?:\.\d+)?)\s*(?:美元|刀|usd|dollars?)?"
DAILY_RE = re.compile(r"(?:每天|每日|一天)(?:的)?(?:自己发起的|自动的)?(?:预算|上限)\s*(?:改成|改为|设为|设成|调到|调成|是|=)\s*" + AMOUNT + "|daily\\s+(?:budget|limit)\\s*(?:to|=|is)\\s*" + AMOUNT, re.I)
JOB_RE = re.compile(r"(?:每件事|每个任务|单个任务|每次)(?:的)?预算\s*(?:改成|改为|设为|设成|调到|调成|是|=)\s*" + AMOUNT + "|per[- ](?:job|task)\\s+budget\\s*(?:to|=|is)\\s*" + AMOUNT, re.I)
ABOUT_RE = re.compile(r"^\s*(?:@[\w.-]+\s*)?(?:你)?(?:现在)?(?:接了|连了|接入了|连着)哪些|^\s*(?:@[\w.-]+\s*)?(?:你)?(?:现在)?(?:的)?(?:状态|设置)(?:是什么|怎么样|如何)?\s*[?？]?\s*$|what\s+are\s+you\s+(?:connected\s+to|set\s+up\s+with)|^\s*status\s*\??\s*$", re.I)


def _amount(m):
    return float(next(g for g in m.groups() if g))


class CommandsMixin:
    def command(self, trigger):
        """-> a reply text (and the thing is done) when the message is one of the phrases above from a person who may say it, else None."""
        text = (trigger.get("text") or "").strip()
        if not (trigger.get("human") and trigger.get("addressed") and trigger.get("actor") != "system" and self.trusted(trigger)) or len(text) > 300: return None
        m = DAILY_RE.search(text)
        if m:
            v = _amount(m)
            if not 0.1 <= v <= 1000: return f"每日上限要在 0.1 到 1000 美元之间,{v:g} 不行。"
            old = self.policy.p["defaults"].get("daily_budget_usd"); self.policy.update({"defaults": {"daily_budget_usd": v}}, origin="person")
            return f"已改:它自己发起的事(CI、例行、评测)每天合计最多花 ${v:g}(原来 ${(old or 0):g})。你开口要的事不受这个限制。今天已花 ${self.spent_today():.2f}。"
        m = JOB_RE.search(text)
        if m:
            v = _amount(m)
            if not 0.05 <= v <= 100: return f"每件事的预算要在 0.05 到 100 美元之间,{v:g} 不行。"
            old = self.policy.p["defaults"].get("budget_usd"); self.policy.update({"defaults": {"budget_usd": v}}, origin="person")
            return f"已改:之后每件事最多花 ${v:g}(原来 ${(old or 0):g});在做的事不受影响。"
        if ABOUT_RE.search(text): return self.about_me()
        return None

    def about_me(self):
        """The inventory: where it is connected, what it does by itself, what it may spend, which version it runs, who else is on the team."""
        cons = [f"{c['title']}" + (f"({c.get('repo')})" if c.get("repo") else "") + (" 已暂停" if c.get("paused") else "") + ("" if c.get("enabled") or c["kind"] == "workbench" else " 只读/未配置") for c in self.connectors_view().get("connectors", [])]
        rts = [f"{r.get('text', '')[:40]}({'每天 ' + r['daily_at'] if r.get('daily_at') else '每 ' + str(int((r.get('every_s') or 0) // 60)) + ' 分钟'}{'' if r.get('enabled', True) else ',已停'})" for r in self.routines()]
        d = self.policy.p["defaults"]; v = self.agent_version(); rules = len(self.memory_view()["rules"])
        team = [a["name"] for a in (self.engines.agents(self.cfg.get("owner") or self.user) if self.engines else []) if a["name"] != (self.cfg.get("agent") or "main")]
        ask = self.ask_view()["total"]
        return ("我现在的样子:\n" f"- 接入:{'、'.join(cons) or '只有工作台'}\n" f"- 例行:{'; '.join(rts) or '没有'}\n"
                f"- 预算:每件事 ${(d.get('budget_usd') or 0):g},它自己发起的事每天 ${(d.get('daily_budget_usd') or 0):g}(今天已花 ${self.spent_today():.2f})\n"
                f"- 运行系统:{(v or {}).get('label') or '部署默认'}{'(已钉住)' if self.store.kv_get('agent.pinned', False) else ''}\n"
                f"- 你教过的规则:{rules} 条\n" + (f"- 其他 agent:{'、'.join('@' + n for n in team)}\n" if team else "") +
                f"- 该问就问:有用 {ask['useful']}、不该问 {ask['unnecessary']}、该问没问 {ask['missed']}")
