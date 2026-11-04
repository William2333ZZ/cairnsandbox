"""An MCP server (stdio, JSON-RPC 2.0) that gives any MCP-speaking agent (Claude Code, Codex, Cursor, OpenClaw) the sandbox cloud as tools (D45):
create a sandbox, run commands, read / write files, checkpoint / rollback / fork, pause / resume / kill, list. Zero dependencies; the protocol
surface implemented is what tool use needs: initialize, tools/list, tools/call, ping, notifications/initialized.

    python3 -m cairnbox.mcp                       # CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT from the environment
    claude mcp add aios -- python3 -m cairnbox.mcp
"""
import json, sys, traceback

from .client import Client, ApiError

PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "sandbox_create", "description": "Create a sandbox (microVM). Returns its id and node. mem in MiB; cls persistent|ephemeral; network e.g. {\"allow_hosts\": [\"pypi.org\"]}; env is NAME->value put under every command.",
     "inputSchema": {"type": "object", "properties": {"template": {"type": "string"}, "mem": {"type": "integer"}, "cls": {"type": "string", "enum": ["persistent", "ephemeral"]}, "network": {"type": "object"}, "env": {"type": "object"}, "ttl_idle_s": {"type": "integer"}, "template_key": {"type": "string"}, "layers": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "sandbox_run", "description": "Run a shell command in a sandbox and wait. Returns exit code, stdout, stderr.",
     "inputSchema": {"type": "object", "required": ["sandbox", "cmd"], "properties": {"sandbox": {"type": "string"}, "cmd": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}, "user": {"type": "string", "enum": ["agent", "root"]}, "env": {"type": "object"}}}},
    {"name": "sandbox_write_file", "description": "Write a whole file (text) at an absolute path inside the sandbox.",
     "inputSchema": {"type": "object", "required": ["sandbox", "path", "text"], "properties": {"sandbox": {"type": "string"}, "path": {"type": "string"}, "text": {"type": "string"}}}},
    {"name": "sandbox_read_file", "description": "Read a file (text, UTF-8) at an absolute path inside the sandbox.",
     "inputSchema": {"type": "object", "required": ["sandbox", "path"], "properties": {"sandbox": {"type": "string"}, "path": {"type": "string"}}}},
    {"name": "sandbox_checkpoint", "description": "Take a named checkpoint of a running sandbox (memory + disk) to roll back to or fork from later.",
     "inputSchema": {"type": "object", "required": ["sandbox"], "properties": {"sandbox": {"type": "string"}, "name": {"type": "string"}, "note": {"type": "string"}}}},
    {"name": "sandbox_rollback", "description": "Roll a sandbox back in place to a checkpoint (same id, network and files as at that moment).",
     "inputSchema": {"type": "object", "required": ["sandbox", "checkpoint"], "properties": {"sandbox": {"type": "string"}, "checkpoint": {"type": "string"}}}},
    {"name": "sandbox_fork", "description": "Clone a sandbox (copy-on-write) from now or from a checkpoint; returns the clone ids.",
     "inputSchema": {"type": "object", "required": ["sandbox"], "properties": {"sandbox": {"type": "string"}, "n": {"type": "integer"}, "checkpoint": {"type": "string"}, "cls": {"type": "string"}}}},
    {"name": "sandbox_pause", "description": "Pause a sandbox to disk (resumes transparently on the next command).", "inputSchema": {"type": "object", "required": ["sandbox"], "properties": {"sandbox": {"type": "string"}}}},
    {"name": "sandbox_resume", "description": "Resume a paused sandbox.", "inputSchema": {"type": "object", "required": ["sandbox"], "properties": {"sandbox": {"type": "string"}}}},
    {"name": "sandbox_kill", "description": "Delete a sandbox.", "inputSchema": {"type": "object", "required": ["sandbox"], "properties": {"sandbox": {"type": "string"}}}},
    {"name": "sandbox_list", "description": "List the sandboxes this token can see (id, status, node, class).", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "sandbox_info", "description": "Everything the control plane knows about one sandbox (status, checkpoints, volumes, network, usage).", "inputSchema": {"type": "object", "required": ["sandbox"], "properties": {"sandbox": {"type": "string"}}}},
]


class Server:
    """Protocol handling without I/O, so it can be driven by tests; `serve_stdio()` is the loop."""
    def __init__(self, client=None): self.c = client or Client(); self.initialized = False

    def handle(self, msg):
        """One JSON-RPC message in, a response dict (or None for notifications) out."""
        mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
        if method == "initialize":
            return self._ok(mid, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}, "serverInfo": {"name": "cairnbox", "version": "0.1.0"},
                                  "instructions": "Sandboxes are microVMs: create one, run commands and write files in it, checkpoint before risky steps and roll back; kill it when done (idle ones pause by themselves)."})
        if method == "notifications/initialized": self.initialized = True; return None
        if method == "ping": return self._ok(mid, {})
        if method == "tools/list": return self._ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            try: return self._ok(mid, {"content": [{"type": "text", "text": self.call(name, args)}], "isError": False})
            except ApiError as e: return self._ok(mid, {"content": [{"type": "text", "text": f"{e.status}: {e.message}"}], "isError": True})
            except Exception as e:  # noqa: BLE001
                return self._ok(mid, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})
        if mid is None: return None
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}

    def _ok(self, mid, result): return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call(self, name, a):
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None: raise ValueError(f"unknown tool {name}")
        missing = [k for k in tool["inputSchema"].get("required", []) if k not in a]
        if missing: raise ValueError(f"missing required argument(s): {', '.join(missing)}")      # said in words, not as a KeyError
        c = self.c
        sb = (lambda: c.sandboxes.get(a["sandbox"]))
        if name == "sandbox_create":
            s = c.sandboxes.create(**{k: a[k] for k in ("template", "mem", "cls", "network", "env", "ttl_idle_s", "template_key", "layers") if k in a})
            return json.dumps({"id": s.id, "status": s.status, "node": s.node})
        if name == "sandbox_run":
            r = sb().commands.run(a["cmd"], cwd=a.get("cwd"), timeout=a.get("timeout"), user=a.get("user"), env=a.get("env"))
            return json.dumps({"exit": r.exit, "stdout": r.stdout, "stderr": r.stderr, "timed_out": r.timed_out})
        if name == "sandbox_write_file": return json.dumps({"bytes": sb().files.write(a["path"], a["text"])})
        if name == "sandbox_read_file": return sb().files.read_text(a["path"])
        if name == "sandbox_checkpoint": return json.dumps(sb().checkpoints.create(a.get("name"), a.get("note")))
        if name == "sandbox_rollback": r = sb().checkpoints.rollback(a["checkpoint"]); return json.dumps({"id": r.get("id"), "status": r.get("status"), "rollback_s": r.get("rollback_s")})
        if name == "sandbox_fork": return json.dumps([x.id for x in sb().fork(n=a.get("n", 1), checkpoint=a.get("checkpoint"), cls=a.get("cls", "ephemeral"))])
        if name == "sandbox_pause": return json.dumps({"status": sb().pause().status})
        if name == "sandbox_resume": return json.dumps({"status": sb().resume().status})
        if name == "sandbox_kill": return json.dumps(sb().kill())
        if name == "sandbox_list": return json.dumps([{"id": s.id, "status": s.status, "node": s.node, "class": s.info.get("class")} for s in c.sandboxes.list()])
        if name == "sandbox_info": return json.dumps(sb().info)
        raise ValueError(f"unknown tool {name}")

    def serve_stdio(self, inp=sys.stdin, out=sys.stdout):
        """Newline-delimited JSON-RPC on stdio (what MCP clients spawn)."""
        for line in inp:
            line = line.strip()
            if not line: continue
            try: msg = json.loads(line)
            except ValueError: continue
            try: resp = self.handle(msg)
            except Exception:  # noqa: BLE001
                resp = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": traceback.format_exc()[-400:]}}
            if resp is not None: out.write(json.dumps(resp) + "\n"); out.flush()


def main():
    Server().serve_stdio()


if __name__ == "__main__":
    main()
