# SQLite store: tables, JSON columns, migrations (docs/011 状态目录).
import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *

# ----------------------------------------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers(id TEXT PRIMARY KEY, source TEXT, actor TEXT, human INTEGER, addressed INTEGER, scope TEXT, text TEXT,
  url TEXT, threads TEXT, channel TEXT, meta TEXT, disposition TEXT, created REAL);
CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, source TEXT, title TEXT, subscriptions TEXT, eval TEXT, closed TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, thread TEXT, triggers TEXT, outcome TEXT, strength TEXT, basis TEXT, reason TEXT,
  matched_rules TEXT, would_accept INTEGER, policy_version TEXT, by_whom TEXT, ticket TEXT, created REAL);
CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, short TEXT, title TEXT, thread TEXT, source TEXT, mandate TEXT, channels TEXT, inputs TEXT,
  state TEXT, ended_by TEXT, outcome TEXT, workspace_layer TEXT, branch TEXT, pr TEXT, attention TEXT, last_report TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY, ticket TEXT, state TEXT, sandbox TEXT, node TEXT, version TEXT, layers TEXT,
  reports TEXT, events TEXT, agent_exit INTEGER, cost REAL, tokens_in INTEGER, tokens_out INTEGER, elapsed_s REAL, error TEXT, log TEXT,
  recovery INTEGER, created REAL, started REAL, ended REAL, traj TEXT);
CREATE TABLE IF NOT EXISTS policy_versions(id TEXT PRIMARY KEY, policy TEXT, origin TEXT, created REAL);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, channel TEXT, ticket TEXT, purpose TEXT, text TEXT, state TEXT, attempts INTEGER, error TEXT, created REAL, sent REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, ticket TEXT, kind TEXT, action TEXT, draft TEXT, state TEXT, actor TEXT, created REAL, decided REAL, decided_by TEXT, result TEXT, error TEXT, review TEXT);
CREATE TABLE IF NOT EXISTS memory(id TEXT PRIMARY KEY, layer TEXT, text TEXT, ticket TEXT, repo TEXT, source TEXT, created REAL, updated REAL, chk TEXT);
CREATE TABLE IF NOT EXISTS corrections(id TEXT PRIMARY KEY, ticket TEXT, kind TEXT, text TEXT, context TEXT, rule TEXT, state TEXT, asked_before INTEGER, repo TEXT, version TEXT, task TEXT, created REAL);
CREATE TABLE IF NOT EXISTS conversation(id TEXT PRIMARY KEY, ts REAL, kind TEXT, say TEXT, actor TEXT, text TEXT, ticket TEXT, origin TEXT, dest TEXT, delivered INTEGER, meta TEXT, created REAL);
"""
JSON_COLS = {"threads", "channel", "meta", "subscriptions", "eval", "closed", "triggers", "matched_rules", "mandate", "channels", "inputs", "pr",
             "context", "correlated", "attention", "last_report", "layers", "reports", "events", "policy", "traj", "origin", "dest", "action", "result", "task", "review", "chk"}

MIGRATIONS = ["ALTER TABLE assignments ADD COLUMN traj TEXT", "ALTER TABLE tickets ADD COLUMN task TEXT", "ALTER TABLE approvals ADD COLUMN review TEXT", "ALTER TABLE memory ADD COLUMN chk TEXT", "ALTER TABLE assignments ADD COLUMN tokens_cached INTEGER", "ALTER TABLE threads ADD COLUMN context TEXT", "ALTER TABLE threads ADD COLUMN correlated TEXT", "ALTER TABLE assignments ADD COLUMN home TEXT", "ALTER TABLE assignments ADD COLUMN home_volume TEXT"]


class Store:
    """One SQLite file; every row belongs to a user (docs/009 鉴权与隔离): rows are inserted with this store's user and read
    only with it, and kv keys are prefixed with it. One deployment serves user "default" today; more users are more stores."""
    def __init__(self, path, user="default"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None); self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock(); self.user = user
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL"); self.db.executescript(SCHEMA)
            self.tables = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for m in MIGRATIONS + [f"ALTER TABLE {t} ADD COLUMN user TEXT DEFAULT 'default'" for t in self.tables if t != "kv"]:
                try: self.db.execute(m)
                except sqlite3.OperationalError: pass
            self.cols = {t: [r[1] for r in self.db.execute(f"PRAGMA table_info({t})")] for t in self.tables}
            if user == "default":                                                      # keys written before users existed belong to the default user
                for (k,) in self.db.execute("SELECT k FROM kv WHERE k NOT LIKE '%:%'").fetchall():
                    self.db.execute("UPDATE kv SET k=? WHERE k=?", (f"default:{k}", k))

    def _scoped(self, table): return "user" in self.cols.get(table, ())

    @staticmethod
    def _enc(d): return {k: (json.dumps(v) if k in JSON_COLS else v) for k, v in d.items()}

    @staticmethod
    def _dec(row):
        if row is None: return None
        d = dict(row)
        for k in JSON_COLS:
            if k in d and isinstance(d[k], str):
                try: d[k] = json.loads(d[k])
                except ValueError: pass                                                 # a plain string written before the column became JSON (policy_versions.origin)
        for k in ("human", "addressed", "would_accept", "recovery"):
            if k in d and d[k] is not None: d[k] = bool(d[k])
        return d

    def insert(self, table, d):
        if self._scoped(table) and "user" not in d: d = {**d, "user": self.user}
        d = self._enc(d)
        with self.lock: self.db.execute(f"INSERT INTO {table}({','.join(d)}) VALUES({','.join('?' * len(d))})", list(d.values()))

    def update(self, table, rid, d):
        d = self._enc(d)
        with self.lock: self.db.execute(f"UPDATE {table} SET {','.join(k + '=?' for k in d)} WHERE id=?", list(d.values()) + [rid])

    def get(self, table, rid):
        q, a = (f"SELECT * FROM {table} WHERE id=? AND user=?", (rid, self.user)) if self._scoped(table) else (f"SELECT * FROM {table} WHERE id=?", (rid,))
        with self.lock: return self._dec(self.db.execute(q, a).fetchone())

    def all(self, table, where="", args=(), order="created"):
        if self._scoped(table):                                                         # the user's rows only, whatever the caller asked
            cond = where.strip()[len("WHERE"):].strip() if where.strip().upper().startswith("WHERE") else ""
            where = "WHERE user=?" + (f" AND ({cond})" if cond else ""); args = (self.user,) + tuple(args)
        with self.lock: return [self._dec(r) for r in self.db.execute(f"SELECT * FROM {table} {where} ORDER BY {order}", args)]

    def kv_get(self, k, default=None):
        with self.lock: r = self.db.execute("SELECT v FROM kv WHERE k=?", (f"{self.user}:{k}",)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, k, v):
        with self.lock: self.db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (f"{self.user}:{k}", json.dumps(v)))
