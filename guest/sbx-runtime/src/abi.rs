//! Cairn kernel ABI v1 (docs/003-guest.md): a unix socket the harness talks to. The runtime is the only
//! side with authority; the harness can report, ask and be refused.
//!   socket  /run/aios/abi.sock (0666; peer uid checked with SO_PEERCRED: root, aios, agent)
//!   wire    NDJSON  {"id","op",...} -> {"id","ok",...}
//!   ops     hello | event {type,data} | budget_get | budget_charge {tokens_in,tokens_out,cost_usd} | checkpoint {note}
//!           report {state,text?,attention?,deliverable?} | ticket_get | inbox {since}
//!   log     /run/aios/events.jsonl (root 0600, append-only from the harness's point of view; the host reads it via sandboxd)
//!   host    ticket_set {ticket} and abi_input {kind,text,...} arrive over vsock (sandboxd ops) and land in /run/aios/{ticket.json,inbox.jsonl}
use serde_json::{json, Map, Value};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::io::AsRawFd;
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

pub const ABI_VERSION: u32 = 2;
pub const SOCK: &str = "/run/aios/abi.sock";
pub const EVENTS: &str = "/run/aios/events.jsonl";
pub const TICKET: &str = "/run/aios/ticket.json";
pub const INBOX: &str = "/run/aios/inbox.jsonl";
pub const REPORT_STATES: &[&str] = &["progress", "waiting", "ready", "blocked", "failed", "dropped"];
const CAPS: &[&str] = &["event", "budget", "checkpoint", "report", "ticket", "inbox"];

#[derive(Default, Clone, serde::Serialize)]
pub struct Budget { pub tokens_in: u64, pub tokens_out: u64, pub cost_usd: f64, pub calls: u64,
                    pub max_tokens: u64, pub max_usd: f64 }     // 0 = unlimited (the authoritative limit is the host proxy)

pub struct Abi { pub budget: Mutex<Budget>, pub seq: Mutex<u64>, log: Mutex<Option<fs::File>>, pub inbox_n: Mutex<u64> }

impl Abi {
    pub fn new() -> Arc<Abi> {
        fs::create_dir_all("/run/aios").ok();
        let log = OpenOptions::new().create(true).append(true).open(EVENTS).ok();
        if let Ok(md) = fs::metadata(EVENTS) { let mut p = md.permissions(); p.set_mode(0o600); fs::set_permissions(EVENTS, p).ok(); }
        Arc::new(Abi { budget: Mutex::new(Budget::default()), seq: Mutex::new(0), log: Mutex::new(log), inbox_n: Mutex::new(0) })
    }
    pub fn record(&self, uid: u32, typ: &str, data: Value) -> u64 {
        let seq = { let mut s = self.seq.lock().unwrap(); *s += 1; *s };
        let ts = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
        let line = json!({"seq": seq, "ts": ts, "uid": uid, "type": typ, "data": data});
        if let Some(f) = &mut *self.log.lock().unwrap() { let _ = writeln!(f, "{}", line); }
        seq
    }
    /// Events since `since` (host side, via sandboxd's abi_events op).
    pub fn events(&self, since: u64, max: usize) -> Vec<Value> {
        let Ok(f) = fs::File::open(EVENTS) else { return vec![] };
        BufReader::new(f).lines().filter_map(|l| l.ok()).filter_map(|l| serde_json::from_str::<Value>(&l).ok())
            .filter(|v| v.get("seq").and_then(|s| s.as_u64()).unwrap_or(0) > since).take(max).collect()
    }
    pub fn set_limits(&self, max_tokens: u64, max_usd: f64) { let mut b = self.budget.lock().unwrap(); b.max_tokens = max_tokens; b.max_usd = max_usd; }
    /// Host side: the Ticket the harness works on (root-owned, world-readable; the harness reads it through ticket_get).
    pub fn set_ticket(&self, ticket: &Value) -> Result<(), String> {
        fs::write(TICKET, serde_json::to_vec_pretty(ticket).map_err(|e| e.to_string())?).map_err(|e| format!("OSError: {e}"))?;
        fs::set_permissions(TICKET, fs::Permissions::from_mode(0o644)).ok();
        self.record(0, "kernel", json!({"ticket_set": ticket.get("id").cloned().unwrap_or(Value::Null)})); Ok(())
    }
    /// Host side: one Input appended to the inbox (fan-out of a Trigger, a policy change, a note). Returns its number.
    pub fn push_input(&self, mut input: Map<String, Value>) -> Result<u64, String> {
        let n = { let mut g = self.inbox_n.lock().unwrap(); *g += 1; *g };
        let ts = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
        input.insert("n".into(), json!(n)); input.insert("ts".into(), json!(ts));
        let mut f = OpenOptions::new().create(true).append(true).open(INBOX).map_err(|e| format!("OSError: {e}"))?;
        fs::set_permissions(INBOX, fs::Permissions::from_mode(0o644)).ok();
        writeln!(f, "{}", Value::Object(input.clone())).map_err(|e| format!("OSError: {e}"))?;
        self.record(0, "input", json!({"n": n, "kind": input.get("kind").cloned().unwrap_or(Value::Null)})); Ok(n)
    }
    fn inbox(&self, since: u64) -> Vec<Value> {
        let Ok(f) = fs::File::open(INBOX) else { return vec![] };
        BufReader::new(f).lines().filter_map(|l| l.ok()).filter_map(|l| serde_json::from_str::<Value>(&l).ok())
            .filter(|v| v.get("n").and_then(|s| s.as_u64()).unwrap_or(0) > since).collect()
    }

    fn handle(&self, uid: u32, req: &Value) -> Result<Map<String, Value>, String> {
        let op = req.get("op").and_then(|v| v.as_str()).unwrap_or("");
        let o = |v: Value| v.as_object().cloned().unwrap_or_default();
        match op {
            "hello" => Ok(o(json!({"abi": ABI_VERSION, "caps": CAPS, "uid": uid, "budget": &*self.budget.lock().unwrap()}))),
            "event" => {
                let typ = req.get("type").and_then(|v| v.as_str()).unwrap_or("note");
                if typ == "kernel" { return Err("reserved event type".into()); }
                let seq = self.record(uid, typ, req.get("data").cloned().unwrap_or(Value::Null));
                Ok(o(json!({"seq": seq})))
            }
            "budget_get" => Ok(o(json!({"budget": &*self.budget.lock().unwrap()}))),
            "budget_charge" => {
                let f = |k: &str| req.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0);
                let mut b = self.budget.lock().unwrap();
                b.tokens_in += f("tokens_in") as u64; b.tokens_out += f("tokens_out") as u64; b.cost_usd += f("cost_usd"); b.calls += 1;
                let over = (b.max_tokens > 0 && b.tokens_in + b.tokens_out >= b.max_tokens) || (b.max_usd > 0.0 && b.cost_usd >= b.max_usd);
                let snap = b.clone(); drop(b);
                if over { self.record(uid, "kernel", json!({"budget_exceeded": true})); }
                Ok(o(json!({"budget": snap, "over": over})))
            }
            "report" => {
                let state = req.get("state").and_then(|v| v.as_str()).unwrap_or("");
                if !REPORT_STATES.contains(&state) { return Err(format!("bad report state '{state}'")); }
                let mut data = Map::new(); data.insert("state".into(), json!(state));
                for k in ["text", "attention", "deliverable"] { if let Some(v) = req.get(k) { data.insert(k.into(), v.clone()); } }
                let seq = self.record(uid, "report", Value::Object(data));
                Ok(o(json!({"seq": seq})))
            }
            "ticket_get" => {
                let t = fs::read_to_string(TICKET).ok().and_then(|s| serde_json::from_str::<Value>(&s).ok());
                Ok(o(json!({"ticket": t, "inbox_n": *self.inbox_n.lock().unwrap()})))
            }
            "inbox" => {
                let since = req.get("since").and_then(|v| v.as_u64()).unwrap_or(0);
                let items = self.inbox(since); let last = items.last().and_then(|e| e.get("n")).cloned().unwrap_or(json!(since));
                Ok(o(json!({"inputs": items, "last_n": last})))
            }
            "checkpoint" => {
                let seq = self.record(uid, "checkpoint", json!({"note": req.get("note").cloned().unwrap_or(Value::Null)}));
                unsafe { libc::sync(); }
                Ok(o(json!({"seq": seq})))
            }
            other => Err(format!("unknown op '{other}'")),
        }
    }
}

fn peer_uid(s: &UnixStream) -> Option<u32> {
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() }; let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let r = unsafe { libc::getsockopt(s.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED, &mut cred as *mut _ as *mut _, &mut len) };
    if r == 0 { Some(cred.uid) } else { None }
}

pub fn serve(abi: Arc<Abi>, allowed_uids: Vec<u32>) {
    let _ = fs::remove_file(SOCK);
    let l = match UnixListener::bind(SOCK) { Ok(l) => l, Err(e) => { eprintln!("abi: bind {SOCK}: {e}"); return; } };
    fs::set_permissions(SOCK, fs::Permissions::from_mode(0o666)).ok();
    abi.record(0, "kernel", json!({"abi": ABI_VERSION, "listening": SOCK}));
    for conn in l.incoming() {
        let Ok(conn) = conn else { continue };
        let uid = match peer_uid(&conn) { Some(u) if allowed_uids.contains(&u) => u, other => { eprintln!("abi: refused peer {other:?}"); continue; } };
        let abi = abi.clone();
        thread::spawn(move || {
            let mut w = match conn.try_clone() { Ok(w) => w, Err(_) => return };
            for line in BufReader::new(conn).lines() {
                let Ok(line) = line else { break };
                if line.trim().is_empty() { continue; }
                let resp = match serde_json::from_str::<Value>(&line) {
                    Err(e) => json!({"id": null, "ok": false, "error": format!("bad json: {e}")}),
                    Ok(req) => { let id = req.get("id").cloned().unwrap_or(Value::Null);
                        match abi.handle(uid, &req) { Ok(mut m) => { m.insert("id".into(), id); m.insert("ok".into(), json!(true)); Value::Object(m) }
                                                      Err(e) => json!({"id": id, "ok": false, "error": e}) } }
                };
                if w.write_all(format!("{}\n", resp).as_bytes()).is_err() { break; }
            }
        });
    }
}
