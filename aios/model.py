"""OpenAI-compatible chat model over the stdlib (no litellm). One tool: bash. Cost comes from the model proxy's
X-Cost-USD / X-Tokens-* headers (the proxy meters outside the sandbox); a plain upstream yields cost 0."""
import json, logging, time, urllib.error, urllib.request
from dataclasses import dataclass, field
from jinja2 import StrictUndefined, Template

log = logging.getLogger("aios.model")

BASH_TOOL = {"type": "function", "function": {"name": "bash", "description": "Execute a bash command",
             "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command to execute"}},
                            "required": ["command"]}}}


class FormatError(Exception):
    def __init__(self, message): self.message = message; super().__init__("format error")


@dataclass
class ModelConfig:
    model_name: str
    base_url: str
    api_key: str = ""
    model_kwargs: dict = field(default_factory=dict)       # temperature, max_tokens, ...
    timeout_s: int = 180
    retries: int = 4
    format_error_template: str = "{{ error }}"
    compact_history: bool = False
    compact_batch: int = 1
    observation_template: str = ("{% if output.exception_info %}<exception>{{output.exception_info}}</exception>\n{% endif %}"
                                 "<returncode>{{output.returncode}}</returncode>\n<output>\n{{output.output}}</output>")


KEEP_FULL = 8            # the most recent messages are sent whole
OLD_OUTPUT_CHARS = 1500  # an older command's output longer than this is sent as its head and tail


def compact_history(messages, keep=KEEP_FULL, limit=OLD_OUTPUT_CHARS, batch=1):
    """Each model call re-sends the whole conversation, so a long job pays for every old `cat` again at every step (on a real repository a job spent 1.7M input tokens in 35 steps
    and stopped at its cost limit). Old tool outputs shrink to their head and tail with a note that they can be run again; the task, the model's own words and calls, and the
    latest `keep` messages stay as they were. Returns a new list; the trajectory the harness records is untouched."""
    out = []; cut = max(0, len(messages) - keep)
    cut -= cut % max(1, batch)          # batch > 1: the boundary moves in steps, so what is sent stays identical for `batch` messages and the provider's prefix cache keeps hitting (a sliding boundary rewrites the prefix every step)
    for i, m in enumerate(messages):
        c = m.get("content")
        if i < cut and m.get("role") == "tool" and isinstance(c, str) and len(c) > limit:
            head, tail = c[: limit * 2 // 3], c[-limit // 3:]
            m = {**m, "content": f"{head}\n[... {len(c) - len(head) - len(tail)} characters of this earlier output are left out to keep the conversation short; run the command again (with head, tail or grep) if you need them ...]\n{tail}"}
        out.append(m)
    return out


class Model:
    def __init__(self, **kw): self.config = ModelConfig(**kw); self.stats = {"calls": 0, "cost": 0.0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0}

    def _post(self, payload: dict):
        req = urllib.request.Request(self.config.base_url.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.config.api_key or 'none'}"})
        delay = 2.0
        for attempt in range(self.config.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout_s) as r:
                    return json.loads(r.read()), {k.lower(): v for k, v in r.headers.items()}
            except urllib.error.HTTPError as e:
                body = e.read()[:500].decode(errors="replace")
                if e.code in (400, 401, 402, 403, 404, 413, 422) or attempt == self.config.retries:
                    raise RuntimeError(f"model HTTP {e.code}: {body}") from None
                log.warning("model HTTP %s, retry %d: %s", e.code, attempt + 1, body[:120])
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                if attempt == self.config.retries: raise RuntimeError(f"model unreachable: {e}") from None
                log.warning("model error %r, retry %d", e, attempt + 1)
            time.sleep(delay); delay = min(delay * 2, 30)

    def query(self, messages: list[dict]) -> dict:
        prepared = [{k: v for k, v in m.items() if k != "extra"} for m in messages]
        if self.config.compact_history: prepared = compact_history(prepared, batch=self.config.compact_batch)           # off by default: the first candidate that turned it on was not accepted (docs/006 对话要送多少)
        resp, hdr = self._post({"model": self.config.model_name, "messages": prepared, "tools": [BASH_TOOL], **self.config.model_kwargs})
        usage = resp.get("usage") or {}
        cost = {"cost": float(hdr.get("x-cost-usd", 0) or 0), "tokens_in": int(hdr.get("x-tokens-in") or usage.get("prompt_tokens", 0) or 0),
                "tokens_out": int(hdr.get("x-tokens-out") or usage.get("completion_tokens", 0) or 0),
                "tokens_cached": int(hdr.get("x-tokens-cached") or usage.get("prompt_cache_hit_tokens", 0) or (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)}
        self.stats["calls"] += 1
        for k in cost: self.stats[k] += cost[k]
        choice = (resp.get("choices") or [{}])[0]; msg = dict(choice.get("message") or {}); finish = choice.get("finish_reason")
        try: actions = self._parse_actions(msg.get("tool_calls") or [], finish)
        except FormatError as e:
            e.message["extra"].update(cost); e.message["extra"]["response"] = resp; raise
        msg.setdefault("content", msg.get("content") or "")
        msg["extra"] = {"actions": actions, "response": resp, **cost, "timestamp": time.time()}
        return msg

    def _parse_actions(self, tool_calls, finish_reason):
        def err(text, has):
            content = Template(self.config.format_error_template, undefined=StrictUndefined).render(error=text, actions=[], has_tool_calls=has, finish_reason=finish_reason)
            return FormatError({"role": "user", "content": content, "extra": {"interrupt_type": "FormatError"}})
        if not tool_calls: raise err("No tool calls found in the response. Every response MUST include at least one tool call.", False)
        actions = []
        for tc in tool_calls:
            fn = tc.get("function") or {}; e = ""
            try: args = json.loads(fn.get("arguments") or "{}")
            except ValueError as ex: args, e = {}, f"Error parsing tool call arguments: {ex}."
            if fn.get("name") != "bash": e += f"Unknown tool '{fn.get('name')}'."
            if not isinstance(args, dict) or "command" not in args: e += "Missing 'command' argument in bash tool call."
            if e: raise err(e.strip(), True)
            actions.append({"command": args["command"], "tool_call_id": tc.get("id")})
        return actions

    def format_message(self, **kw) -> dict: return kw

    def format_observation_messages(self, message: dict, outputs: list[dict], template_vars: dict | None = None) -> list[dict]:
        actions = message.get("extra", {}).get("actions", [])
        outputs = outputs + [{"output": "", "returncode": -1, "exception_info": "action was not executed"}] * (len(actions) - len(outputs))
        out = []
        for a, o in zip(actions, outputs):
            content = Template(self.config.observation_template, undefined=StrictUndefined).render(output=o, **(template_vars or {}))
            out.append({"role": "tool", "tool_call_id": a.get("tool_call_id"), "content": content,
                        "extra": {"raw_output": o.get("output", ""), "returncode": o.get("returncode"), "timestamp": time.time(), "exception_info": o.get("exception_info")}})
        return out

    def get_template_vars(self) -> dict: return {k: v for k, v in self.config.__dict__.items() if k != "api_key"}

    def serialize(self) -> dict: return {"info": {"config": {"model": self.get_template_vars(), "model_type": "aios.model.Model"}, "model_stats": dict(self.stats)}}
