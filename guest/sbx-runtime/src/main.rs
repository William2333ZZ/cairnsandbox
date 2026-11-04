//! sandboxd v1 in Rust: AF_VSOCK listener (host CID only), newline-delimited JSON.
//! request : {"id","op",...}  response: {"id","ok",...}  streamed: {"id","event":"out"|"err","data"} during exec/session_exec.
mod abi;
mod landlock;
use base64::{engine::general_purpose::STANDARD as B64, Engine};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::io::{AsRawFd, FromRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const PORT: u32 = 5000;
const HOST_CID: u32 = 2;
const SHELL: &str = "/usr/local/lib/sandboxd/bash";
const DEFAULT_USER: &str = "agent";
const MAX_OUT: usize = 1 << 20;
const NET_FILE: &str = "/run/sandboxd/net.json";

type R = Result<Map<String, Value>, String>;
fn err<T>(kind: &str, msg: impl std::fmt::Display) -> Result<T, String> { Err(format!("{kind}: {msg}")) }
fn obj(v: Value) -> Map<String, Value> { v.as_object().cloned().unwrap_or_default() }

// ---------------- users / env ----------------
fn user(name: &str) -> Result<(u32, u32, String), String> {
    let pw = fs::read_to_string("/etc/passwd").map_err(|e| format!("OSError: {e}"))?;
    for line in pw.lines() {
        let f: Vec<&str> = line.split(':').collect();
        if f.len() >= 6 && f[0] == name { return Ok((f[2].parse().unwrap_or(65534), f[3].parse().unwrap_or(65534), f[5].to_string())); }
    }
    err("KeyError", format!("getpwnam(): name not found: '{name}'"))
}

fn net_env() -> Vec<(String, String)> {
    let Ok(s) = fs::read_to_string(NET_FILE) else { return vec![] };
    let Ok(v) = serde_json::from_str::<Value>(&s) else { return vec![] };
    match v.get("proxy").and_then(|p| p.as_str()) {
        Some(px) if !px.is_empty() => ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"].iter().map(|k| (k.to_string(), px.to_string()))
            .chain(std::iter::once(("no_proxy".into(), "localhost,127.0.0.1".into()))).collect(),
        _ => vec![],
    }
}

fn base_env(name: &str, home: &str) -> Vec<(String, String)> {
    let mut e = vec![("HOME", home), ("USER", name), ("LOGNAME", name), ("SHELL", SHELL), ("TERM", "xterm"),
                     ("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"), ("LANG", "C.UTF-8")]
        .into_iter().map(|(k, v)| (k.to_string(), v.to_string())).collect::<Vec<_>>();
    e.extend(net_env()); e
}

/// Drop to uid/gid in the child (after setsid) — mirrors subprocess(user=, group=, start_new_session=True).
/// With a Landlock policy the confinement is applied first (still root, so O_PATH on any dir works), then privileges go.
fn drop_privs(uid: u32, gid: u32, tty: bool) -> std::io::Result<()> { drop_privs_ll(uid, gid, tty, None) }
fn drop_privs_ll(uid: u32, gid: u32, tty: bool, ll: Option<&landlock::Policy>) -> std::io::Result<()> {
    unsafe {
        if libc::setsid() < 0 { return Err(std::io::Error::last_os_error()); }
        if tty { libc::ioctl(0, libc::TIOCSCTTY as _, 0); }
        if let Some(p) = ll { if let Err(e) = landlock::restrict(p) { return Err(std::io::Error::new(std::io::ErrorKind::Other, format!("landlock: {e}"))); } }
        if libc::setgroups(0, std::ptr::null()) < 0 { return Err(std::io::Error::last_os_error()); }
        if libc::setgid(gid) < 0 { return Err(std::io::Error::last_os_error()); }
        if libc::setuid(uid) < 0 { return Err(std::io::Error::last_os_error()); }
    }
    Ok(())
}

// ---------------- capped buffer ----------------
struct Capped { buf: Vec<u8>, cap: usize, truncated: bool }
impl Capped {
    fn new() -> Self { Capped { buf: Vec::new(), cap: MAX_OUT, truncated: false } }
    fn add<'a>(&mut self, b: &'a [u8]) -> &'a [u8] {
        let room = self.cap.saturating_sub(self.buf.len());
        if room == 0 { self.truncated = true; return &b[..0]; }
        let n = b.len().min(room); self.buf.extend_from_slice(&b[..n]);
        if b.len() > room { self.truncated = true; }
        &b[..n]
    }
    fn text(&self) -> String { String::from_utf8_lossy(&self.buf).into_owned() }
}

fn read_fd(fd: RawFd, buf: &mut [u8]) -> isize { unsafe { libc::read(fd, buf.as_mut_ptr() as *mut _, buf.len()) } }
fn poll_fds(fds: &[RawFd], ms: i32) -> Vec<RawFd> {
    let mut pfds: Vec<libc::pollfd> = fds.iter().map(|&fd| libc::pollfd { fd, events: libc::POLLIN, revents: 0 }).collect();
    let n = unsafe { libc::poll(pfds.as_mut_ptr(), pfds.len() as _, ms) };
    if n <= 0 { return vec![]; }
    pfds.iter().filter(|p| p.revents != 0).map(|p| p.fd).collect()
}
fn killpg(pid: i32) { unsafe { libc::kill(-pid, libc::SIGKILL); } }

// ---------------- sessions ----------------
struct Session {
    id: String, pty: bool, alive: Arc<AtomicBool>, pid: i32,
    rx: Mutex<mpsc::Receiver<Option<Vec<u8>>>>, pending: Mutex<Vec<u8>>,
    writer: Mutex<SessWriter>,
}
enum SessWriter { Pty(RawFd), Pipe(std::process::ChildStdin) }

impl Session {
    fn open(name: &str, use_pty: bool) -> Result<Arc<Session>, String> {
        let (uid, gid, home) = user(name)?;
        let env = base_env(name, &home); let id = short_id();
        let (tx, rx) = mpsc::channel::<Option<Vec<u8>>>(); let alive = Arc::new(AtomicBool::new(true));
        let (pid, rfd, writer) = if use_pty {
            let (mut m, mut s) = (0 as RawFd, 0 as RawFd);
            if unsafe { libc::openpty(&mut m, &mut s, std::ptr::null_mut(), std::ptr::null(), std::ptr::null()) } != 0 { return err("OSError", "openpty"); }
            let (s1, s2, s3) = unsafe { (libc::dup(s), libc::dup(s), s) };
            let mut c = Command::new(SHELL); c.arg("-i").current_dir(&home).env_clear().envs(env.iter().map(|(k, v)| (k, v)));
            c.stdin(unsafe { Stdio::from_raw_fd(s1) }).stdout(unsafe { Stdio::from_raw_fd(s2) }).stderr(unsafe { Stdio::from_raw_fd(s3) });
            unsafe { c.pre_exec(move || drop_privs(uid, gid, true)); }
            let child = c.spawn().map_err(|e| format!("OSError: {e}"))?;
            (child.id() as i32, m, SessWriter::Pty(m))
        } else {
            let mut c = Command::new(SHELL); c.args(["--norc", "--noprofile"]).current_dir(&home).env_clear().envs(env.iter().map(|(k, v)| (k, v)));
            c.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
            unsafe { c.pre_exec(move || drop_privs(uid, gid, false)); }
            let mut child = c.spawn().map_err(|e| format!("OSError: {e}"))?;
            // merge stderr into stdout like stderr=STDOUT: dup2 is not available post-spawn, so read both fds in the reader
            let out = child.stdout.take().unwrap(); let errf = child.stderr.take().unwrap(); let stdin = child.stdin.take().unwrap();
            let (ofd, efd) = (out.as_raw_fd(), errf.as_raw_fd()); std::mem::forget(out); std::mem::forget(errf);
            let tx2 = tx.clone(); let al = alive.clone();
            thread::spawn(move || {   // stderr reader
                let mut b = [0u8; 65536];
                loop { let n = read_fd(efd, &mut b); if n <= 0 { break; } let _ = tx2.send(Some(b[..n as usize].to_vec())); }
                let _ = al.load(Ordering::Relaxed);
            });
            (child.id() as i32, ofd, SessWriter::Pipe(stdin))
        };
        let al = alive.clone();
        thread::spawn(move || {
            let mut b = [0u8; 65536];
            loop { let n = read_fd(rfd, &mut b); if n <= 0 { break; } let _ = tx.send(Some(b[..n as usize].to_vec())); }
            al.store(false, Ordering::Relaxed); let _ = tx.send(None);
            unsafe { let mut st = 0; libc::waitpid(pid, &mut st, libc::WNOHANG); }
        });
        Ok(Arc::new(Session { id, pty: use_pty, alive, pid, rx: Mutex::new(rx), pending: Mutex::new(Vec::new()), writer: Mutex::new(writer) }))
    }
    fn write(&self, data: &[u8]) -> Result<(), String> {
        match &mut *self.writer.lock().unwrap() {
            SessWriter::Pty(fd) => { if unsafe { libc::write(*fd, data.as_ptr() as *const _, data.len()) } < 0 { return err("OSError", "write pty"); } }
            SessWriter::Pipe(w) => { w.write_all(data).and_then(|_| w.flush()).map_err(|e| format!("OSError: {e}"))?; }
        }
        Ok(())
    }
    /// Next chunk: pending bytes first, then the channel with a timeout. Ok(None) = EOF, Err(()) = timeout.
    fn next(&self, timeout: Duration) -> Result<Option<Vec<u8>>, ()> {
        { let mut p = self.pending.lock().unwrap(); if !p.is_empty() { return Ok(Some(std::mem::take(&mut *p))); } }
        match self.rx.lock().unwrap().recv_timeout(timeout) { Ok(v) => Ok(v), Err(mpsc::RecvTimeoutError::Timeout) => Err(()), Err(_) => Ok(None) }
    }
    fn kill(&self) {
        self.alive.store(false, Ordering::Relaxed); killpg(self.pid);
        unsafe { let mut st = 0; for _ in 0..20 { if libc::waitpid(self.pid, &mut st, libc::WNOHANG) != 0 { break; } thread::sleep(Duration::from_millis(100)); } }
        if let SessWriter::Pty(fd) = &*self.writer.lock().unwrap() { unsafe { libc::close(*fd); } }
    }
}

type Sessions = Arc<Mutex<HashMap<String, Arc<Session>>>>;

fn short_id() -> String {
    let mut b = [0u8; 4]; fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut b)).ok();
    b.iter().map(|x| format!("{x:02x}")).collect()
}

// ---------------- ops ----------------
struct Ctx<'a> { sessions: &'a Sessions, emit: &'a dyn Fn(Value), boot: Instant, abi: &'a Arc<abi::Abi> }

fn s_of<'a>(req: &'a Value, k: &str) -> Option<&'a str> { req.get(k).and_then(|v| v.as_str()) }
fn f_of(req: &Value, k: &str, d: f64) -> f64 { req.get(k).and_then(|v| v.as_f64()).unwrap_or(d) }
fn get_session(ctx: &Ctx, req: &Value) -> Result<Arc<Session>, String> {
    let id = s_of(req, "session").ok_or("KeyError: 'session'")?;
    match ctx.sessions.lock().unwrap().get(id) { Some(s) if s.alive.load(Ordering::Relaxed) => Ok(s.clone()), _ => err("KeyError", "no such live session") }
}

fn op_ping(ctx: &Ctx, _req: &Value) -> R { Ok(obj(json!({"uptime_s": ctx.boot.elapsed().as_secs_f64(), "pid": std::process::id()}))) }

fn op_health(ctx: &Ctx, _req: &Value) -> R {
    let mut la = [0f64; 3]; unsafe { libc::getloadavg(la.as_mut_ptr(), 3); }
    let mut mem = HashMap::new();
    for line in fs::read_to_string("/proc/meminfo").unwrap_or_default().lines() {
        if let Some((k, v)) = line.split_once(':') { mem.insert(k.to_string(), v.split_whitespace().next().and_then(|x| x.parse::<u64>().ok()).unwrap_or(0)); }
    }
    let root_fs = Command::new("findmnt").args(["-n", "-o", "FSTYPE", "/"]).output().map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default();
    Ok(obj(json!({"uptime_s": ctx.boot.elapsed().as_secs_f64(), "load1": la[0], "mem_total_kb": mem.get("MemTotal").copied().unwrap_or(0),
                  "mem_avail_kb": mem.get("MemAvailable").copied().unwrap_or(0), "sessions": ctx.sessions.lock().unwrap().len(), "root_fs": root_fs})))
}

fn op_exec(ctx: &Ctx, req: &Value) -> R {
    let name = s_of(req, "user").unwrap_or(DEFAULT_USER); let (uid, gid, home) = user(name)?;
    let cmd = s_of(req, "cmd").ok_or("KeyError: 'cmd'")?; let timeout = f_of(req, "timeout", 60.0); let stream = req.get("stream").and_then(|v| v.as_bool()).unwrap_or(false);
    let cwd = s_of(req, "cwd").filter(|c| !c.is_empty()).map(|c| c.to_string()).unwrap_or(home.clone());
    let mut env = base_env(name, &home);
    if let Some(extra) = req.get("env").and_then(|v| v.as_object()) { for (k, v) in extra { env.push((k.clone(), v.as_str().map(|s| s.to_string()).unwrap_or_else(|| v.to_string()))); } }
    let t0 = Instant::now();
    let policy = landlock_policy(req, &cwd, &home);
    let mut c = Command::new(SHELL); c.arg("-c").arg(cmd).current_dir(&cwd).env_clear().envs(env.iter().map(|(k, v)| (k, v)));
    c.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    unsafe { c.pre_exec(move || drop_privs_ll(uid, gid, false, policy.as_ref())); }
    let mut child = c.spawn().map_err(|e| format!("OSError: {e}"))?;
    { let mut sin = child.stdin.take().unwrap(); if let Some(d) = s_of(req, "stdin") { let _ = sin.write_all(d.as_bytes()); } }   // dropped: EOF
    let out = child.stdout.take().unwrap(); let errf = child.stderr.take().unwrap();
    let (ofd, efd) = (out.as_raw_fd(), errf.as_raw_fd());
    let (mut cap_o, mut cap_e) = (Capped::new(), Capped::new()); let mut open = vec![ofd, efd]; let mut timed_out = false; let mut buf = vec![0u8; 65536];
    while !open.is_empty() {
        let left = timeout - t0.elapsed().as_secs_f64();
        if left <= 0.0 { timed_out = true; killpg(child.id() as i32); break; }
        for fd in poll_fds(&open, (left.min(0.5) * 1000.0) as i32) {
            let n = read_fd(fd, &mut buf);
            if n <= 0 { open.retain(|&x| x != fd); continue; }
            let (name, cap) = if fd == ofd { ("out", &mut cap_o) } else { ("err", &mut cap_e) };
            let kept = cap.add(&buf[..n as usize]);
            if stream && !kept.is_empty() { (ctx.emit)(json!({"event": name, "data": String::from_utf8_lossy(kept)})); }
        }
    }
    let status = child.wait().map_err(|e| format!("OSError: {e}"))?; drop(out); drop(errf);
    let code = if timed_out { Value::Null } else { json!(status.code().unwrap_or(-1)) };
    Ok(obj(json!({"exit": code, "timeout": timed_out, "stdout": cap_o.text(), "stderr": cap_e.text(), "truncated": cap_o.truncated || cap_e.truncated, "elapsed_s": t0.elapsed().as_secs_f64()})))
}

/// `landlock: true` = default harness confinement (read everything, write only cwd/tmp/home, TCP connect only to the
/// egress proxy port 3128); `landlock: {"ro":[..],"rw":[..],"connect_ports":[..]}` = explicit policy; absent = none.
fn landlock_policy(req: &Value, cwd: &str, home: &str) -> Option<landlock::Policy> {
    let v = req.get("landlock")?;
    let strs = |k: &str, d: Vec<String>| v.get(k).and_then(|a| a.as_array()).map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect()).unwrap_or(d);
    match v {
        Value::Bool(true) => Some(landlock::Policy { ro: vec!["/".into()], rw: vec![cwd.into(), "/tmp".into(), home.into(), "/dev/null".into(), "/dev/zero".into(), "/dev/shm".into(), "/dev/pts".into(), "/dev/tty".into()], connect_ports: vec![3128] }),
        Value::Object(_) => Some(landlock::Policy { ro: strs("ro", vec!["/".into()]), rw: { let mut v = strs("rw", vec![cwd.into(), "/tmp".into()]); v.push("/dev/null".into()); v },   // shells need /dev/null
                                                   connect_ports: v.get("connect_ports").and_then(|a| a.as_array()).map(|a| a.iter().filter_map(|x| x.as_u64().map(|p| p as u16)).collect()).unwrap_or(vec![3128]) }),
        _ => None,
    }
}

fn op_abi_events(ctx: &Ctx, req: &Value) -> R {
    let since = f_of(req, "since", 0.0) as u64; let max = f_of(req, "max", 1000.0) as usize;
    let ev = ctx.abi.events(since, max); let last = ev.last().and_then(|e| e.get("seq")).cloned().unwrap_or(json!(since));
    Ok(obj(json!({"events": ev, "last_seq": last})))
}
fn op_budget_set(ctx: &Ctx, req: &Value) -> R {
    ctx.abi.set_limits(f_of(req, "max_tokens", 0.0) as u64, f_of(req, "max_usd", 0.0));
    ctx.abi.record(0, "kernel", json!({"budget_set": {"max_tokens": f_of(req, "max_tokens", 0.0), "max_usd": f_of(req, "max_usd", 0.0)}}));
    Ok(obj(json!({"budget": &*ctx.abi.budget.lock().unwrap()})))
}
fn op_ticket_set(ctx: &Ctx, req: &Value) -> R {
    let t = req.get("ticket").ok_or("KeyError: 'ticket'")?;
    ctx.abi.set_ticket(t)?; Ok(obj(json!({"ticket": t.get("id").cloned().unwrap_or(Value::Null)})))
}
fn op_abi_input(ctx: &Ctx, req: &Value) -> R {
    let mut m = obj(req.clone()); m.remove("id"); m.remove("op");
    if !m.contains_key("kind") { m.insert("kind".into(), json!("text")); }
    let n = ctx.abi.push_input(m)?; Ok(obj(json!({"n": n})))
}
fn op_abi_info(ctx: &Ctx, _req: &Value) -> R {
    Ok(obj(json!({"abi": abi::ABI_VERSION, "socket": abi::SOCK, "landlock_abi": landlock::abi_version(), "budget": &*ctx.abi.budget.lock().unwrap(), "events": *ctx.abi.seq.lock().unwrap()})))
}

fn op_session_open(ctx: &Ctx, req: &Value) -> R {
    let s = Session::open(s_of(req, "user").unwrap_or(DEFAULT_USER), req.get("pty").and_then(|v| v.as_bool()).unwrap_or(false))?;
    let id = s.id.clone(); let pty = s.pty; ctx.sessions.lock().unwrap().insert(id.clone(), s);
    Ok(obj(json!({"session": id, "pty": pty})))
}

fn find(h: &[u8], n: &[u8]) -> Option<usize> { if n.is_empty() || h.len() < n.len() { return None; } (0..=h.len() - n.len()).find(|&i| &h[i..i + n.len()] == n) }
fn rfind(h: &[u8], n: &[u8]) -> Option<usize> { if n.is_empty() || h.len() < n.len() { return None; } (0..=h.len() - n.len()).rev().find(|&i| &h[i..i + n.len()] == n) }

fn op_session_exec(ctx: &Ctx, req: &Value) -> R {
    let s = get_session(ctx, req)?;
    if s.pty { return err("ValueError", "use session_write/session_read for pty sessions"); }
    let cmd = s_of(req, "cmd").ok_or("KeyError: 'cmd'")?; let timeout = f_of(req, "timeout", 60.0); let stream = req.get("stream").and_then(|v| v.as_bool()).unwrap_or(false);
    let token = short_id(); let sent = format!("__SBX_{token}_"); let sentb = sent.as_bytes().to_vec();
    s.write(format!("{cmd}\nprintf '\\n{sent}%s__\\n' \"$?\"\n").as_bytes())?;
    let t0 = Instant::now(); let mut cap = Capped::new(); let mut buf: Vec<u8> = Vec::new();
    loop {
        let left = timeout - t0.elapsed().as_secs_f64();
        if left <= 0.0 {
            s.kill(); ctx.sessions.lock().unwrap().remove(&s.id);
            return Ok(obj(json!({"exit": null, "timeout": true, "session_closed": true, "stdout": cap.text(), "truncated": cap.truncated, "elapsed_s": t0.elapsed().as_secs_f64()})));
        }
        let chunk = match s.next(Duration::from_secs_f64(left.min(0.5))) { Err(()) => continue, Ok(None) => {
            ctx.sessions.lock().unwrap().remove(&s.id);
            return Ok(obj(json!({"exit": null, "session_closed": true, "stdout": cap.text(), "truncated": cap.truncated})));
        }, Ok(Some(b)) => b };
        buf.extend_from_slice(&chunk);
        if let Some(i) = find(&buf, &sentb) {
            let after = &buf[i + sentb.len()..];
            let digits: Vec<u8> = after.iter().take_while(|c| c.is_ascii_digit()).cloned().collect();
            if after.len() > digits.len() + 2 && &after[digits.len()..digits.len() + 3] == b"__\n" {
                let mut data = buf[..i].to_vec(); if data.ends_with(b"\n") { data.pop(); }
                let rest = after[digits.len() + 3..].to_vec();
                let kept = cap.add(&data).to_vec();
                if stream && !kept.is_empty() { (ctx.emit)(json!({"event": "out", "data": String::from_utf8_lossy(&kept)})); }
                if !rest.is_empty() { s.pending.lock().unwrap().extend_from_slice(&rest); }
                let code: i64 = String::from_utf8_lossy(&digits).parse().unwrap_or(-1);
                return Ok(obj(json!({"exit": code, "stdout": cap.text(), "truncated": cap.truncated, "elapsed_s": t0.elapsed().as_secs_f64()})));
            }
            continue;   // sentinel seen but incomplete: wait for more
        }
        let cut = rfind(&buf, b"\n__SBX_");
        let flush: Vec<u8> = match cut { None => std::mem::take(&mut buf), Some(c) => { let f = buf[..c].to_vec(); buf = buf[c..].to_vec(); f } };
        if !flush.is_empty() { let kept = cap.add(&flush).to_vec(); if stream && !kept.is_empty() { (ctx.emit)(json!({"event": "out", "data": String::from_utf8_lossy(&kept)})); } }
    }
}

fn op_session_write(ctx: &Ctx, req: &Value) -> R {
    let s = get_session(ctx, req)?; let data = B64.decode(s_of(req, "data_b64").unwrap_or("")).map_err(|e| format!("ValueError: {e}"))?;
    s.write(&data)?; Ok(obj(json!({"bytes": data.len()})))
}

fn op_session_read(ctx: &Ctx, req: &Value) -> R {
    let id = s_of(req, "session").ok_or("KeyError: 'session'")?;
    let s = ctx.sessions.lock().unwrap().get(id).cloned().ok_or("KeyError: no such session")?;
    let wait = f_of(req, "wait", 0.2); let grace = f_of(req, "grace", 0.05); let t_end = Instant::now() + Duration::from_secs_f64(wait);
    let mut data: Vec<u8> = Vec::new();
    loop {
        let to = if data.is_empty() { t_end.saturating_duration_since(Instant::now()) } else { Duration::from_secs_f64(grace) };
        match s.next(to) { Ok(Some(b)) => { data.extend_from_slice(&b); if data.len() >= MAX_OUT { break; } }, _ => break }
    }
    Ok(obj(json!({"data_b64": B64.encode(&data), "bytes": data.len(), "alive": s.alive.load(Ordering::Relaxed)})))
}

fn op_session_close(ctx: &Ctx, req: &Value) -> R {
    let id = s_of(req, "session").unwrap_or(""); let s = ctx.sessions.lock().unwrap().remove(id);
    if let Some(s) = &s { s.kill(); } Ok(obj(json!({"closed": s.is_some()})))
}

fn op_write_file(_ctx: &Ctx, req: &Value) -> R {
    let path = s_of(req, "path").ok_or("KeyError: 'path'")?; let data = B64.decode(s_of(req, "data_b64").unwrap_or("")).map_err(|e| format!("ValueError: {e}"))?;
    if let Some(p) = std::path::Path::new(path).parent() { if !p.as_os_str().is_empty() { fs::create_dir_all(p).map_err(|e| format!("OSError: {e}"))?; } }
    fs::write(path, &data).map_err(|e| format!("OSError: {e}"))?;
    if let Some(m) = s_of(req, "mode") { let mode = u32::from_str_radix(m, 8).map_err(|e| format!("ValueError: {e}"))?; fs::set_permissions(path, std::os::unix::fs::PermissionsExt::from_mode(mode)).map_err(|e| format!("OSError: {e}"))?; }
    let owner = match req.get("owner") { None => DEFAULT_USER.to_string(), Some(v) => v.as_str().unwrap_or("").to_string() };
    if !owner.is_empty() { let (uid, gid, _) = user(&owner)?; let c = std::ffi::CString::new(path).unwrap(); if unsafe { libc::chown(c.as_ptr(), uid, gid) } != 0 { return err("OSError", "chown"); } }
    Ok(obj(json!({"bytes": data.len()})))
}

fn op_read_file(_ctx: &Ctx, req: &Value) -> R {
    let path = s_of(req, "path").ok_or("KeyError: 'path'")?; let off = f_of(req, "offset", 0.0) as u64; let max = f_of(req, "max_bytes", MAX_OUT as f64) as usize;
    let mut f = fs::File::open(path).map_err(|e| format!("FileNotFoundError: {e}"))?; use std::io::Seek; f.seek(std::io::SeekFrom::Start(off)).map_err(|e| format!("OSError: {e}"))?;
    let mut data = vec![0u8; max]; let mut n = 0; while n < max { let r = f.read(&mut data[n..]).map_err(|e| format!("OSError: {e}"))?; if r == 0 { break; } n += r; } data.truncate(n);
    Ok(obj(json!({"data_b64": B64.encode(&data), "bytes": n})))
}

fn op_pack(_ctx: &Ctx, _req: &Value) -> R {
    // The tar is staged in /run (tmpfs, half of RAM: fast). A big layer does not fit there, so on "No space left" it goes to /var/tmp on the
    // root overlay instead, which is bounded by the sandbox's disk, not its memory (D76). That file lives inside the tree being packed:
    // tar does not recognise it as its own archive through the overlay, so it is excluded by name, and the node deletes it afterwards.
    let tar = |out: &str| -> Result<(i32, String), String> {
        let o = Command::new("tar").args(["--xattrs", "--xattrs-include=*", "--numeric-owner", "--exclude=./var/tmp/pack.tar", "-C", "/run/upper/upper", "-cf", out, "."]).output().map_err(|e| format!("OSError: {e}"))?;
        Ok((o.status.code().unwrap_or(-1), String::from_utf8_lossy(&o.stderr).to_string()))
    };
    let mut out = "/run/pack.tar";
    let (mut code, mut stderr) = tar(out)?;
    if code != 0 && code != 1 && stderr.contains("No space left") {
        fs::remove_file(out).ok(); fs::create_dir_all("/var/tmp").ok();
        out = "/var/tmp/pack.tar";
        (code, stderr) = tar(out)?;
    }
    if code != 0 && code != 1 { fs::remove_file(out).ok(); return err("RuntimeError", tail(&stderr, 500)); }
    let bytes = fs::metadata(out).map(|m| m.len()).unwrap_or(0);
    Ok(obj(json!({"path": out, "bytes": bytes, "warnings": tail(&stderr, 300)})))
}
fn tail(s: &str, n: usize) -> String { let l = s.len(); if l <= n { s.to_string() } else { let mut i = l - n; while !s.is_char_boundary(i) { i += 1; } s[i..].to_string() } }

fn op_list_dir(_ctx: &Ctx, req: &Value) -> R {
    let path = s_of(req, "path").ok_or("KeyError: 'path'")?; let mut out = vec![];
    for e in fs::read_dir(path).map_err(|e| format!("FileNotFoundError: {e}"))? {
        let e = e.map_err(|e| format!("OSError: {e}"))?; let md = e.metadata().map_err(|e| format!("OSError: {e}"))?;
        let mtime = md.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_secs()).unwrap_or(0);
        out.push(json!({"name": e.file_name().to_string_lossy(), "dir": md.is_dir(), "size": md.len(), "mtime": mtime}));
    }
    Ok(obj(json!({"entries": out})))
}

fn op_delete(_ctx: &Ctx, req: &Value) -> R {
    let p = s_of(req, "path").ok_or("KeyError: 'path'")?; let md = fs::symlink_metadata(p).map_err(|e| format!("FileNotFoundError: {e}"))?;
    if md.is_dir() { fs::remove_dir_all(p) } else { fs::remove_file(p) }.map_err(|e| format!("OSError: {e}"))?;
    Ok(obj(json!({"deleted": p})))
}

fn op_set_time(_ctx: &Ctx, req: &Value) -> R {
    let epoch = f_of(req, "epoch", 0.0); let ts = libc::timespec { tv_sec: epoch as i64, tv_nsec: ((epoch % 1.0) * 1e9) as i64 };
    if unsafe { libc::clock_settime(libc::CLOCK_REALTIME, &ts) } != 0 { return err("OSError", "clock_settime"); }
    Ok(obj(json!({"now": SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)})))
}

fn op_add_entropy(_ctx: &Ctx, req: &Value) -> R {
    let data = B64.decode(s_of(req, "data_b64").unwrap_or("")).map_err(|e| format!("ValueError: {e}"))?;
    fs::OpenOptions::new().write(true).open("/dev/urandom").and_then(|mut f| f.write_all(&data)).map_err(|e| format!("OSError: {e}"))?;
    Ok(obj(json!({"bytes": data.len()})))
}

fn sh(args: &[&str]) -> String { Command::new(args[0]).args(&args[1..]).output().map(|o| String::from_utf8_lossy(&o.stdout).to_string()).unwrap_or_default() }

fn op_net_config(_ctx: &Ctx, req: &Value) -> R {
    let (ip, prefix, gw) = (s_of(req, "ip").ok_or("KeyError: 'ip'")?, req.get("prefix").map(|v| v.to_string()).unwrap_or("24".into()).trim_matches('"').to_string(), s_of(req, "gw").ok_or("KeyError: 'gw'")?);
    let proxy = s_of(req, "proxy").filter(|p| !p.is_empty()).map(|p| p.to_string());
    sh(&["ip", "addr", "flush", "dev", "eth0"]); sh(&["ip", "link", "set", "eth0", "up"]);
    sh(&["ip", "addr", "add", &format!("{ip}/{prefix}"), "dev", "eth0"]); sh(&["ip", "route", "replace", "default", "via", gw, "dev", "eth0"]);
    fs::create_dir_all("/run/sandboxd").ok();
    fs::write(NET_FILE, serde_json::to_string(&json!({"ip": ip, "prefix": prefix, "gw": gw, "proxy": proxy})).unwrap()).map_err(|e| format!("OSError: {e}"))?;
    fs::write("/etc/resolv.conf", format!("nameserver {gw}\n")).ok();
    if let Some(px) = &proxy {
        fs::write("/etc/profile.d/10-sandbox-proxy.sh", format!("export http_proxy={px} https_proxy={px} HTTP_PROXY={px} HTTPS_PROXY={px} no_proxy=localhost,127.0.0.1\n")).ok();
        fs::create_dir_all("/etc/apt/apt.conf.d").ok();
        fs::write("/etc/apt/apt.conf.d/99sandbox-proxy", format!("Acquire::http::Proxy \"{px}\";\nAcquire::https::Proxy \"{px}\";\n")).ok();
    }
    Ok(obj(json!({"eth0": sh(&["ip", "-4", "-br", "addr", "show", "eth0"]).trim(), "proxy": proxy})))
}

fn op_quiesce(_ctx: &Ctx, _req: &Value) -> R {
    unsafe { libc::sync(); }
    let o = Command::new("fsfreeze").args(["--freeze", "/run/upper"]).output().map_err(|e| format!("OSError: {e}"))?;
    Ok(obj(json!({"frozen": o.status.success(), "detail": tail(String::from_utf8_lossy(&o.stderr).trim(), 120)})))
}
fn op_thaw(_ctx: &Ctx, _req: &Value) -> R {
    let o = Command::new("fsfreeze").args(["--unfreeze", "/run/upper"]).output().map_err(|e| format!("OSError: {e}"))?;
    let e = String::from_utf8_lossy(&o.stderr).to_string();
    Ok(obj(json!({"thawed": o.status.success() || e.contains("not frozen"), "detail": tail(e.trim(), 120)})))
}
fn op_shutdown(_ctx: &Ctx, _req: &Value) -> R {
    thread::spawn(|| { thread::sleep(Duration::from_millis(200)); unsafe { libc::sync(); libc::reboot(libc::LINUX_REBOOT_CMD_RESTART); } });
    Ok(obj(json!({"shutting_down": true})))
}

fn dispatch(op: &str, ctx: &Ctx, req: &Value) -> R {
    match op {
        "ping" => op_ping(ctx, req), "health" => op_health(ctx, req), "exec" => op_exec(ctx, req),
        "session_open" => op_session_open(ctx, req), "session_exec" => op_session_exec(ctx, req), "session_write" => op_session_write(ctx, req),
        "session_read" => op_session_read(ctx, req), "session_close" => op_session_close(ctx, req),
        "write_file" => op_write_file(ctx, req), "read_file" => op_read_file(ctx, req), "pack" => op_pack(ctx, req),
        "list_dir" => op_list_dir(ctx, req), "delete" => op_delete(ctx, req), "set_time" => op_set_time(ctx, req),
        "add_entropy" => op_add_entropy(ctx, req), "net_config" => op_net_config(ctx, req), "quiesce" => op_quiesce(ctx, req),
        "thaw" => op_thaw(ctx, req), "shutdown" => op_shutdown(ctx, req),
        "abi_events" => op_abi_events(ctx, req), "budget_set" => op_budget_set(ctx, req), "abi_info" => op_abi_info(ctx, req),
        "ticket_set" => op_ticket_set(ctx, req), "abi_input" => op_abi_input(ctx, req),
        other => err("KeyError", format!("'{other}'")),
    }
}

// ---------------- connection ----------------
fn serve(stream: UnixStream, sessions: Sessions, boot: Instant, abi: Arc<abi::Abi>) {
    let w = Arc::new(Mutex::new(stream.try_clone().expect("clone")));
    let send = |v: Value| { let mut g = w.lock().unwrap(); let _ = g.write_all(format!("{}\n", v).as_bytes()); };
    for line in BufReader::new(stream).lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() { continue; }
        let req: Value = match serde_json::from_str(&line) { Ok(v) => v, Err(e) => { send(json!({"id": null, "ok": false, "error": format!("JSONDecodeError: {e}")})); continue; } };
        let rid = req.get("id").cloned().unwrap_or(Value::Null);
        let rid2 = rid.clone(); let w2 = w.clone();
        let emit = move |ev: Value| { let mut m = obj(ev); m.insert("id".into(), rid2.clone()); let mut g = w2.lock().unwrap(); let _ = g.write_all(format!("{}\n", Value::Object(m)).as_bytes()); };
        let ctx = Ctx { sessions: &sessions, emit: &emit, boot, abi: &abi };
        let resp = match dispatch(s_of(&req, "op").unwrap_or(""), &ctx, &req) {
            Ok(mut m) => { m.insert("id".into(), rid); m.insert("ok".into(), json!(true)); Value::Object(m) }
            Err(e) => json!({"id": rid, "ok": false, "error": e}),
        };
        send(resp);
    }
}

fn main() {
    let boot = Instant::now(); let sessions: Sessions = Arc::new(Mutex::new(HashMap::new()));
    unsafe { libc::signal(libc::SIGPIPE, libc::SIG_IGN); }
    let abi_srv = abi::Abi::new();
    { let a = abi_srv.clone(); let uids: Vec<u32> = ["root", "aios", "agent"].iter().filter_map(|n| user(n).ok().map(|u| u.0)).collect();
      thread::spawn(move || abi::serve(a, uids)); }
    let fd = unsafe { libc::socket(libc::AF_VSOCK, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0) };
    if fd < 0 { eprintln!("sandboxd: vsock socket failed"); std::process::exit(1); }
    let mut addr: libc::sockaddr_vm = unsafe { std::mem::zeroed() }; addr.svm_family = libc::AF_VSOCK as _; addr.svm_port = PORT; addr.svm_cid = libc::VMADDR_CID_ANY;
    let len = std::mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t;
    if unsafe { libc::bind(fd, &addr as *const _ as *const libc::sockaddr, len) } < 0 || unsafe { libc::listen(fd, 64) } < 0 { eprintln!("sandboxd: bind/listen failed"); std::process::exit(1); }
    println!("sandboxd v1 (rust) listening on vsock port {PORT} (host-only); abi v{} on {}", abi::ABI_VERSION, abi::SOCK);
    loop {
        let mut peer: libc::sockaddr_vm = unsafe { std::mem::zeroed() }; let mut plen = len;
        let c = unsafe { libc::accept(fd, &mut peer as *mut _ as *mut libc::sockaddr, &mut plen) };
        if c < 0 { continue; }
        if peer.svm_cid != HOST_CID { eprintln!("sandboxd: rejected connection from cid {}", peer.svm_cid); unsafe { libc::close(c); } continue; }
        let stream = unsafe { UnixStream::from_raw_fd(c) }; let sess = sessions.clone(); let a = abi_srv.clone();
        thread::spawn(move || serve(stream, sess, boot, a));
    }
}
