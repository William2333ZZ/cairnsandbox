//! E2B envd compatibility, served from inside the guest on vsock port 49983 (host CID only, like the NDJSON port).
//!
//! What the official E2B SDKs speak to a sandbox (verified against e2b 2.63.0, docs/research/e2b-compat-2026-11.md §7):
//!   * REST: GET /health (204), GET /files?path=&username=, POST /files (multipart or application/octet-stream)
//!   * Connect protocol with the JSON codec (the SDKs use `useBinaryFormat: false`): unary = application/json, server streams =
//!     application/connect+json envelopes (flag u8, length u32 BE, JSON), a final envelope with flag 2 carries the end-of-stream status.
//!     Services: process.Process (Start, Connect, List, SendInput, SendSignal, CloseStdin, Update) and filesystem.Filesystem
//!     (Stat, MakeDir, Move, ListDir, Remove, CreateWatcher, GetWatcherEvents, RemoveWatcher).
//! HTTP/1.1 only (cleartext SDK traffic is HTTP/1.1 even with the SDK's default http_version "2"); one request per connection
//! (`Connection: close`), so the host side can relay the bytes without understanding the framing. Request bodies may be chunked.
//! Not implemented: StreamInput (client streaming needs HTTP/2), /files gzip, /init, /metrics. WatchDir and the watcher RPCs poll (no inotify).
use crate::{base_env, drop_privs, user, DEFAULT_USER};
use base64::{engine::general_purpose::STANDARD as B64, Engine};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::fs;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::io::{FromRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::Duration;

pub const PORT: u32 = 49983;
/// Written by the host at create/restore (op set_env): the sandbox's `envVars`, laid under every process envd starts.
const ENV_FILE: &str = "/run/sandboxd/env.json";
const MAX_MULTIPART: usize = 128 << 20;

// ---------------- errors ----------------
struct CErr { code: &'static str, msg: String }
fn cerr(code: &'static str, msg: impl Into<String>) -> CErr { CErr { code, msg: msg.into() } }
fn http_of(code: &str) -> u16 {
    match code { "invalid_argument" | "failed_precondition" | "out_of_range" => 400, "unauthenticated" => 401, "permission_denied" => 403, "not_found" => 404,
                 "already_exists" | "aborted" => 409, "resource_exhausted" => 429, "canceled" => 499, "unimplemented" => 501, "unavailable" => 503,
                 "deadline_exceeded" => 504, _ => 500 }
}
fn io_err(e: io::Error, what: &str) -> CErr {
    match e.kind() {
        io::ErrorKind::NotFound => cerr("not_found", format!("{what}: not found")),
        io::ErrorKind::PermissionDenied => cerr("permission_denied", format!("{what}: permission denied")),
        io::ErrorKind::AlreadyExists => cerr("already_exists", format!("{what}: already exists")),
        _ => cerr("internal", format!("{what}: {e}")),
    }
}

// ---------------- HTTP ----------------
struct Request { method: String, path: String, query: Vec<(String, String)>, headers: Vec<(String, String)> }
impl Request {
    fn h(&self, k: &str) -> Option<&str> { self.headers.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str()) }
    fn q(&self, k: &str) -> Option<&str> { self.query.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str()) }
}

fn pct_decode(s: &str) -> String {
    let b = s.as_bytes(); let mut out = Vec::with_capacity(b.len()); let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'%' if i + 2 < b.len() => {
                let h = std::str::from_utf8(&b[i + 1..i + 3]).ok().and_then(|x| u8::from_str_radix(x, 16).ok());
                if let Some(v) = h { out.push(v); i += 3; } else { out.push(b'%'); i += 1; }
            }
            b'+' => { out.push(b' '); i += 1; }
            c => { out.push(c); i += 1; }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn read_line(rd: &mut impl BufRead, limit: usize) -> io::Result<Option<String>> {
    let mut buf = Vec::new();
    let n = rd.by_ref().take(limit as u64).read_until(b'\n', &mut buf)?;
    if n == 0 { return Ok(None); }
    while buf.last() == Some(&b'\n') || buf.last() == Some(&b'\r') { buf.pop(); }
    Ok(Some(String::from_utf8_lossy(&buf).into_owned()))
}

fn read_head(rd: &mut impl BufRead) -> io::Result<Option<Request>> {
    let Some(line) = read_line(rd, 65536)? else { return Ok(None) };
    let mut it = line.split_whitespace();
    let (Some(method), Some(target)) = (it.next(), it.next()) else { return Err(io::Error::new(io::ErrorKind::InvalidData, "bad request line")) };
    let (path, qs) = target.split_once('?').unwrap_or((target, ""));
    let query = qs.split('&').filter(|p| !p.is_empty()).map(|p| { let (k, v) = p.split_once('=').unwrap_or((p, "")); (pct_decode(k), pct_decode(v)) }).collect();
    let mut headers = Vec::new();
    loop {
        let Some(l) = read_line(rd, 65536)? else { break };
        if l.is_empty() { break; }
        if let Some((k, v)) = l.split_once(':') { headers.push((k.trim().to_ascii_lowercase(), v.trim().to_string())); }
        if headers.len() > 200 { return Err(io::Error::new(io::ErrorKind::InvalidData, "too many headers")); }
    }
    Ok(Some(Request { method: method.to_string(), path: pct_decode(path), query, headers }))
}

/// The request body as a reader: Content-Length, chunked, or empty.
enum BodyKind { Len(u64), Chunked { left: usize, done: bool } }
struct Body<'a, R: BufRead> { r: &'a mut R, kind: BodyKind }
impl<'a, R: BufRead> Body<'a, R> {
    fn new(r: &'a mut R, req: &Request) -> Self {
        let chunked = req.h("transfer-encoding").map(|v| v.to_ascii_lowercase().contains("chunked")).unwrap_or(false);
        let kind = if chunked { BodyKind::Chunked { left: 0, done: false } } else { BodyKind::Len(req.h("content-length").and_then(|v| v.parse().ok()).unwrap_or(0)) };
        Body { r, kind }
    }
}
impl<'a, R: BufRead> Read for Body<'a, R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match &mut self.kind {
            BodyKind::Len(n) => { if *n == 0 { return Ok(0); } let k = (*n).min(buf.len() as u64) as usize; let got = self.r.read(&mut buf[..k])?; *n -= got as u64; Ok(got) }
            BodyKind::Chunked { left, done } => {
                if *done { return Ok(0); }
                if *left == 0 {
                    let line = read_line(self.r, 1024)?.ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "chunk size"))?;
                    let size = usize::from_str_radix(line.split(';').next().unwrap_or("0").trim(), 16).map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "bad chunk size"))?;
                    if size == 0 { loop { match read_line(self.r, 1024)? { Some(l) if !l.is_empty() => continue, _ => break } } *done = true; return Ok(0); }
                    *left = size;
                }
                let k = (*left).min(buf.len()); let got = self.r.read(&mut buf[..k])?; *left -= got;
                if *left == 0 { let _ = read_line(self.r, 8); }
                Ok(got)
            }
        }
    }
}

fn read_all(body: &mut impl Read, limit: usize) -> Result<Vec<u8>, CErr> {
    let mut v = Vec::new();
    body.take(limit as u64 + 1).read_to_end(&mut v).map_err(|e| cerr("invalid_argument", format!("reading body: {e}")))?;
    if v.len() > limit { return Err(cerr("resource_exhausted", "request body too large")); }
    Ok(v)
}

fn status_text(c: u16) -> &'static str {
    match c { 200 => "OK", 204 => "No Content", 400 => "Bad Request", 401 => "Unauthorized", 403 => "Forbidden", 404 => "Not Found", 409 => "Conflict", 415 => "Unsupported Media Type",
              429 => "Too Many Requests", 499 => "Client Closed Request", 500 => "Internal Server Error", 501 => "Not Implemented", 503 => "Service Unavailable", 504 => "Gateway Timeout", _ => "Status" }
}
fn respond(w: &mut impl Write, code: u16, ct: &str, body: &[u8]) -> io::Result<()> {
    let head = if code == 204 { format!("HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n") }
               else { format!("HTTP/1.1 {code} {}\r\nContent-Type: {ct}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", status_text(code), body.len()) };
    w.write_all(head.as_bytes())?; if code != 204 { w.write_all(body)?; } w.flush()
}
fn respond_err(w: &mut impl Write, e: &CErr) -> io::Result<()> {
    respond(w, http_of(e.code), "application/json", json!({"code": e.code, "message": e.msg}).to_string().as_bytes())
}
fn respond_json(w: &mut impl Write, v: &Value) -> io::Result<()> { respond(w, 200, "application/json", v.to_string().as_bytes()) }

// Connect server-stream: chunked HTTP body of envelopes.
fn stream_begin(w: &mut impl Write) -> io::Result<()> {
    w.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/connect+json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")?; w.flush()
}
fn stream_frame(w: &mut impl Write, flag: u8, v: &Value) -> io::Result<()> {
    let data = v.to_string(); let mut env = Vec::with_capacity(5 + data.len());
    env.push(flag); env.extend_from_slice(&(data.len() as u32).to_be_bytes()); env.extend_from_slice(data.as_bytes());
    w.write_all(format!("{:x}\r\n", env.len()).as_bytes())?; w.write_all(&env)?; w.write_all(b"\r\n")?; w.flush()
}
fn stream_end(w: &mut impl Write, err: Option<&CErr>) -> io::Result<()> {
    let end = match err { None => json!({}), Some(e) => json!({"error": {"code": e.code, "message": e.msg}}) };
    stream_frame(w, 2, &end)?; w.write_all(b"0\r\n\r\n")?; w.flush()
}

/// The first (and only) envelope of a server-streaming request.
fn first_frame(body: &mut impl Read) -> Result<Value, CErr> {
    let mut h = [0u8; 5];
    body.read_exact(&mut h).map_err(|e| cerr("invalid_argument", format!("envelope header: {e}")))?;
    if h[0] & 1 != 0 { return Err(cerr("unimplemented", "compressed envelopes are not supported")); }
    let n = u32::from_be_bytes([h[1], h[2], h[3], h[4]]) as usize;
    if n > (8 << 20) { return Err(cerr("resource_exhausted", "message too large")); }
    let mut b = vec![0u8; n]; body.read_exact(&mut b).map_err(|e| cerr("invalid_argument", format!("envelope body: {e}")))?;
    if b.is_empty() { return Ok(json!({})); }
    serde_json::from_slice(&b).map_err(|e| cerr("invalid_argument", format!("bad JSON: {e}")))
}

// ---------------- users / paths ----------------
fn req_user(req: &Request) -> String {
    if let Some(a) = req.h("authorization").and_then(|v| v.strip_prefix("Basic ")) {
        if let Ok(d) = B64.decode(a.trim()) { let s = String::from_utf8_lossy(&d).into_owned(); let u = s.split(':').next().unwrap_or("").to_string(); if !u.is_empty() { return u; } }
    }
    req.q("username").filter(|u| !u.is_empty()).map(|u| u.to_string()).unwrap_or_else(|| DEFAULT_USER.to_string())
}
/// E2B's default user is called "user"; here the unprivileged default is `agent`, so that name is an alias when no such account exists.
fn home_of(name: &str) -> Result<(u32, u32, String), CErr> {
    user(name).or_else(|e| if name == "user" { user(DEFAULT_USER) } else { Err(e) }).map_err(|_| cerr("invalid_argument", format!("user '{name}' not found")))
}
fn resolve(p: &str, home: &str) -> PathBuf {
    if p == "~" { PathBuf::from(home) } else if let Some(r) = p.strip_prefix("~/") { Path::new(home).join(r) } else if p.starts_with('/') { PathBuf::from(p) } else { Path::new(home).join(p) }
}
fn sandbox_env() -> Vec<(String, String)> {
    let Ok(s) = fs::read_to_string(ENV_FILE) else { return vec![] };
    let Ok(Value::Object(m)) = serde_json::from_str::<Value>(&s) else { return vec![] };
    m.into_iter().map(|(k, v)| (k, v.as_str().map(|x| x.to_string()).unwrap_or_else(|| v.to_string()))).collect()
}
fn id_name(file: &str, id: u32) -> String {
    for l in fs::read_to_string(file).unwrap_or_default().lines() { let f: Vec<&str> = l.split(':').collect(); if f.len() > 2 && f[2].parse::<u32>().ok() == Some(id) { return f[0].to_string(); } }
    id.to_string()
}
fn rfc3339(secs: i64, nanos: u32) -> String {
    let days = secs.div_euclid(86400); let rem = secs.rem_euclid(86400);
    let z = days + 719468; let era = z.div_euclid(146097); let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1; let m = if mp < 10 { mp + 3 } else { mp - 9 }; let y = if m <= 2 { y + 1 } else { y };
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{:09}Z", rem / 3600, rem % 3600 / 60, rem % 60, nanos)
}
fn perm_string(mode: u32, dir: bool, link: bool) -> String {
    let mut s = String::new(); s.push(if link { 'l' } else if dir { 'd' } else { '-' });
    for (i, c) in ['r', 'w', 'x'].iter().cycle().take(9).enumerate() { s.push(if mode & (1 << (8 - i)) != 0 { *c } else { '-' }); }
    s
}

// ---------------- filesystem ----------------
fn entry(path: &Path) -> Result<Value, CErr> {
    let lmd = fs::symlink_metadata(path).map_err(|e| io_err(e, &path.display().to_string()))?;
    let is_link = lmd.file_type().is_symlink();
    let md = if is_link { fs::metadata(path).unwrap_or_else(|_| lmd.clone()) } else { lmd.clone() };
    let (mut v, mode) = (Map::new(), md.mode() & 0o7777);
    v.insert("name".into(), json!(path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "/".into())));
    v.insert("type".into(), json!(if md.is_dir() { "FILE_TYPE_DIRECTORY" } else { "FILE_TYPE_FILE" }));
    v.insert("path".into(), json!(path.to_string_lossy()));
    v.insert("size".into(), json!(md.len().to_string()));
    v.insert("mode".into(), json!(mode));
    v.insert("permissions".into(), json!(perm_string(mode, md.is_dir(), is_link)));
    v.insert("owner".into(), json!(id_name("/etc/passwd", md.uid())));
    v.insert("group".into(), json!(id_name("/etc/group", md.gid())));
    v.insert("modifiedTime".into(), json!(rfc3339(md.mtime(), md.mtime_nsec() as u32)));
    v.insert("isSymlink".into(), json!(is_link));
    if is_link { if let Ok(t) = fs::read_link(path) { v.insert("symlinkTarget".into(), json!(t.to_string_lossy())); } }
    Ok(Value::Object(v))
}
fn chown_to(path: &Path, uid: u32, gid: u32) { if let Ok(c) = std::ffi::CString::new(path.to_string_lossy().as_bytes()) { unsafe { libc::chown(c.as_ptr(), uid, gid); } } }
fn mkdir_all_owned(dir: &Path, uid: u32, gid: u32) -> io::Result<()> {
    let mut missing = Vec::new(); let mut p = dir.to_path_buf();
    while !p.exists() { missing.push(p.clone()); if !p.pop() { break; } }
    fs::create_dir_all(dir)?; for m in missing.iter().rev() { chown_to(m, uid, gid); } Ok(())
}
fn s_of<'a>(v: &'a Value, k: &str) -> Option<&'a str> { v.get(k).and_then(|x| x.as_str()) }

fn fs_rpc(method: &str, req: &Request, body: &Value) -> Result<Value, CErr> {
    let name = req_user(req); let (uid, gid, home) = home_of(&name)?;
    let path_arg = |k: &str| -> Result<PathBuf, CErr> { s_of(body, k).filter(|p| !p.is_empty()).map(|p| resolve(p, &home)).ok_or_else(|| cerr("invalid_argument", format!("{k} is required"))) };
    match method {
        "Stat" => { let p = path_arg("path")?; Ok(json!({"entry": entry(&p)?})) }
        "MakeDir" => {
            let p = path_arg("path")?;
            if p.exists() { return Err(cerr("already_exists", format!("{}: already exists", p.display()))); }
            mkdir_all_owned(&p, uid, gid).map_err(|e| io_err(e, &p.display().to_string()))?;
            Ok(json!({"entry": entry(&p)?}))
        }
        "Move" => {
            let (src, dst) = (path_arg("source")?, path_arg("destination")?);
            if !src.exists() && fs::symlink_metadata(&src).is_err() { return Err(cerr("not_found", format!("{}: not found", src.display()))); }
            if let Some(par) = dst.parent() { mkdir_all_owned(par, uid, gid).map_err(|e| io_err(e, &par.display().to_string()))?; }
            fs::rename(&src, &dst).map_err(|e| io_err(e, &src.display().to_string()))?;
            Ok(json!({"entry": entry(&dst)?}))
        }
        "ListDir" => {
            let p = path_arg("path")?; let depth = body.get("depth").and_then(|d| d.as_u64()).unwrap_or(1).max(1) as usize;
            let md = fs::metadata(&p).map_err(|e| io_err(e, &p.display().to_string()))?;
            if !md.is_dir() { return Err(cerr("invalid_argument", format!("{} is not a directory", p.display()))); }
            let mut out = Vec::new(); list_rec(&p, depth, &mut out)?;
            Ok(json!({"entries": out}))
        }
        "Remove" => {
            let p = path_arg("path")?; let md = fs::symlink_metadata(&p).map_err(|e| io_err(e, &p.display().to_string()))?;
            if md.is_dir() { fs::remove_dir_all(&p) } else { fs::remove_file(&p) }.map_err(|e| io_err(e, &p.display().to_string()))?;
            Ok(json!({}))
        }
        "CreateWatcher" => {
            let p = path_arg("path")?; let recursive = body.get("recursive").and_then(|v| v.as_bool()).unwrap_or(false);
            fs::metadata(&p).map_err(|e| io_err(e, &p.display().to_string()))?;
            let id = format!("w-{}", crate::short_id()); let snap = snapshot(&p, recursive);
            watchers().lock().unwrap().insert(id.clone(), Watcher { path: p, recursive, snap });
            Ok(json!({"watcherId": id}))
        }
        "GetWatcherEvents" => {
            let id = s_of(body, "watcherId").or_else(|| s_of(body, "watcher_id")).unwrap_or("");
            let mut g = watchers().lock().unwrap(); let w = g.get_mut(id).ok_or_else(|| cerr("not_found", "watcher not found"))?;
            let now = snapshot(&w.path, w.recursive); let ev = diff_events(&w.path, &w.snap, &now);
            w.snap = now; Ok(json!({"events": ev}))
        }
        "RemoveWatcher" => { let id = s_of(body, "watcherId").or_else(|| s_of(body, "watcher_id")).unwrap_or(""); watchers().lock().unwrap().remove(id); Ok(json!({})) }
        other => Err(cerr("unimplemented", format!("filesystem.Filesystem/{other} is not implemented"))),
    }
}
fn list_rec(dir: &Path, depth: usize, out: &mut Vec<Value>) -> Result<(), CErr> {
    let mut names: Vec<_> = fs::read_dir(dir).map_err(|e| io_err(e, &dir.display().to_string()))?.filter_map(|e| e.ok()).map(|e| e.path()).collect();
    names.sort();
    for p in names {
        if let Ok(e) = entry(&p) { out.push(e); }
        if depth > 1 && fs::symlink_metadata(&p).map(|m| m.is_dir()).unwrap_or(false) { let _ = list_rec(&p, depth - 1, out); }
    }
    Ok(())
}
struct Watcher { path: PathBuf, recursive: bool, snap: HashMap<PathBuf, (u64, i64, i64)> }
fn watchers() -> &'static Mutex<HashMap<String, Watcher>> { static W: OnceLock<Mutex<HashMap<String, Watcher>>> = OnceLock::new(); W.get_or_init(|| Mutex::new(HashMap::new())) }
fn snapshot(root: &Path, recursive: bool) -> HashMap<PathBuf, (u64, i64, i64)> {
    let mut m = HashMap::new(); let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(rd) = fs::read_dir(&d) else { continue };
        for e in rd.flatten() {
            let p = e.path(); let Ok(md) = fs::symlink_metadata(&p) else { continue };
            m.insert(p.clone(), (md.len(), md.mtime(), md.mtime_nsec()));
            if recursive && md.is_dir() { stack.push(p); }
        }
    }
    m
}
fn peer_closed(s: &UnixStream) -> bool {
    use std::os::unix::io::AsRawFd;
    let mut b = [0u8; 1];
    unsafe { libc::recv(s.as_raw_fd(), b.as_mut_ptr() as *mut _, 1, libc::MSG_PEEK | libc::MSG_DONTWAIT) == 0 }
}
/// Diff of two snapshots as filesystem events (a rename shows up as a remove plus a create: there is no inotify here).
fn diff_events(root: &Path, old: &HashMap<PathBuf, (u64, i64, i64)>, now: &HashMap<PathBuf, (u64, i64, i64)>) -> Vec<Value> {
    let mut ev = Vec::new();
    for (p, sig) in now { match old.get(p) { None => ev.push(json!({"name": rel(root, p), "type": "EVENT_TYPE_CREATE"})), Some(o) if o != sig => ev.push(json!({"name": rel(root, p), "type": "EVENT_TYPE_WRITE"})), _ => {} } }
    for p in old.keys() { if !now.contains_key(p) { ev.push(json!({"name": rel(root, p), "type": "EVENT_TYPE_REMOVE"})); } }
    ev
}
/// filesystem.Filesystem/WatchDir (server stream): the directory is polled four times a second; the stream ends when the client goes away.
fn watch_stream(w: &mut UnixStream, rd: &mut BufReader<UnixStream>, req: &Request) -> io::Result<()> {
    let first = match first_frame(&mut Body::new(rd, req)) { Ok(v) => v, Err(e) => return respond_err(w, &e) };
    let (_, _, home) = match home_of(&req_user(req)) { Ok(h) => h, Err(e) => return respond_err(w, &e) };
    let Some(p) = s_of(&first, "path").filter(|p| !p.is_empty()) else { return respond_err(w, &cerr("invalid_argument", "path is required")) };
    let (path, recursive) = (resolve(p, &home), first.get("recursive").and_then(|v| v.as_bool()).unwrap_or(false));
    if let Err(e) = fs::metadata(&path) { return respond_err(w, &io_err(e, &path.display().to_string())); }
    let mut snap = snapshot(&path, recursive);
    stream_begin(w)?; stream_frame(w, 0, &json!({"start": {}}))?;
    let (ka, mut last) = (keepalive(req), std::time::Instant::now());
    loop {
        thread::sleep(Duration::from_millis(250));
        if peer_closed(w) { return Ok(()); }
        let now = snapshot(&path, recursive);
        let evs = diff_events(&path, &snap, &now); snap = now;
        for e in &evs { stream_frame(w, 0, &json!({"filesystem": e}))?; }
        if !evs.is_empty() { last = std::time::Instant::now(); }
        if last.elapsed() >= ka { stream_frame(w, 0, &json!({"keepalive": {}}))?; last = std::time::Instant::now(); }
    }
}
fn rel(root: &Path, p: &Path) -> String { p.strip_prefix(root).unwrap_or(p).to_string_lossy().into_owned() }

// ---------------- /files ----------------
fn files_get(w: &mut UnixStream, req: &Request) -> io::Result<()> {
    let name = req_user(req);
    let (_, _, home) = match home_of(&name) { Ok(h) => h, Err(e) => return respond_err(w, &e) };
    let Some(p) = req.q("path").filter(|p| !p.is_empty()) else { return respond_err(w, &cerr("invalid_argument", "path is required")) };
    let path = resolve(p, &home);
    let md = match fs::metadata(&path) { Ok(m) => m, Err(e) => return respond_err(w, &io_err(e, &path.display().to_string())) };
    if md.is_dir() { return respond_err(w, &cerr("invalid_argument", format!("{} is a directory", path.display()))); }
    let mut f = match fs::File::open(&path) { Ok(f) => f, Err(e) => return respond_err(w, &io_err(e, &path.display().to_string())) };
    w.write_all(format!("HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", md.len()).as_bytes())?;
    io::copy(&mut f, w)?; w.flush()
}

fn write_file(path: &Path, data: &mut dyn Read, uid: u32, gid: u32) -> Result<(), CErr> {
    if let Some(par) = path.parent() { if !par.as_os_str().is_empty() { mkdir_all_owned(par, uid, gid).map_err(|e| io_err(e, &par.display().to_string()))?; } }
    let existed = path.exists();
    let mut f = fs::File::create(path).map_err(|e| io_err(e, &path.display().to_string()))?;
    io::copy(data, &mut f).map_err(|e| if e.raw_os_error() == Some(libc::ENOSPC) { cerr("resource_exhausted", "no space left on device") } else { io_err(e, &path.display().to_string()) })?;
    if !existed { let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o644)); chown_to(path, uid, gid); }
    Ok(())
}
fn multipart_boundary(ct: &str) -> Option<String> {
    let i = ct.to_ascii_lowercase().find("boundary=")?; let v = ct[i + 9..].split(';').next()?.trim().trim_matches('"'); Some(v.to_string())
}
fn find(h: &[u8], n: &[u8], from: usize) -> Option<usize> { if n.is_empty() || h.len() < n.len() || from > h.len() - n.len() { return None; } (from..=h.len() - n.len()).find(|&i| &h[i..i + n.len()] == n) }
/// (filename, data) for every part named "file".
fn multipart_parts(body: &[u8], boundary: &str) -> Vec<(String, Vec<u8>)> {
    let delim = format!("--{boundary}").into_bytes(); let mut out = Vec::new(); let mut pos = match find(body, &delim, 0) { Some(p) => p + delim.len(), None => return out };
    loop {
        if body[pos..].starts_with(b"--") { break; }
        if body[pos..].starts_with(b"\r\n") { pos += 2; }
        let Some(hend) = find(body, b"\r\n\r\n", pos) else { break };
        let head = String::from_utf8_lossy(&body[pos..hend]).into_owned(); let start = hend + 4;
        let Some(next) = find(body, &[b"\r\n".as_slice(), &delim].concat(), start) else { break };
        let (mut is_file, mut fname) = (false, String::new());
        for l in head.lines() { if l.to_ascii_lowercase().starts_with("content-disposition") {
            for part in l.split(';') { let part = part.trim(); if let Some(n) = part.strip_prefix("name=") { if n.trim_matches('"') == "file" { is_file = true; } }
                if let Some(f) = part.strip_prefix("filename=") { fname = f.trim_matches('"').to_string(); } } } }
        if is_file { out.push((fname, body[start..next].to_vec())); }
        pos = next + 2 + delim.len();
    }
    out
}

fn files_post(w: &mut UnixStream, rd: &mut BufReader<UnixStream>, req: &Request) -> io::Result<()> {
    let name = req_user(req);
    let (uid, gid, home) = match home_of(&name) { Ok(h) => h, Err(e) => return respond_err(w, &e) };
    if req.h("content-encoding").map(|v| !v.is_empty() && v != "identity").unwrap_or(false) { return respond(w, 415, "application/json", json!({"code": "unsupported", "message": "Content-Encoding is not supported"}).to_string().as_bytes()); }
    let ct = req.h("content-type").unwrap_or("").to_string(); let mut body = Body::new(rd, req);
    let mut written: Vec<Value> = Vec::new();
    let mut put = |path: PathBuf, data: &mut dyn Read| -> Result<(), CErr> {
        write_file(&path, data, uid, gid)?;
        written.push(json!({"name": path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(), "path": path.to_string_lossy(), "type": "file"})); Ok(())
    };
    let res: Result<(), CErr> = (|| {
        if ct.to_ascii_lowercase().starts_with("multipart/form-data") {
            let b = multipart_boundary(&ct).ok_or_else(|| cerr("invalid_argument", "multipart boundary missing"))?;
            let raw = read_all(&mut body, MAX_MULTIPART)?; let parts = multipart_parts(&raw, &b);
            if parts.is_empty() { return Err(cerr("invalid_argument", "no file part")); }
            let single = parts.len() == 1;
            for (fname, data) in parts {
                let p = req.q("path").filter(|p| !p.is_empty() && single).map(|p| p.to_string()).unwrap_or(fname);
                if p.is_empty() { return Err(cerr("invalid_argument", "path is required")); }
                put(resolve(&p, &home), &mut &data[..])?;
            }
        } else {
            let p = req.q("path").filter(|p| !p.is_empty()).ok_or_else(|| cerr("invalid_argument", "path is required"))?;
            put(resolve(p, &home), &mut body)?;
        }
        Ok(())
    })();
    match res { Ok(()) => respond_json(w, &Value::Array(written)), Err(e) => respond_err(w, &e) }
}
// A single-file multipart upload names its target in the `path` query; several files carry their paths as filenames.

// ---------------- processes ----------------
#[derive(Clone)]
enum Ev { Out(&'static str, Vec<u8>), End(i32, String) }
struct Proc {
    pid: i32, tag: Option<String>, cmd: String, args: Vec<String>, envs: Map<String, Value>, cwd: Option<String>,
    master: Option<RawFd>, stdin: Mutex<Option<ChildStdin>>, stdin_enabled: bool, subs: Mutex<Vec<Sender<Ev>>>,
}
impl Proc {
    fn emit(&self, ev: Ev) { self.subs.lock().unwrap().retain(|s| s.send(ev.clone()).is_ok()); }
    fn info(&self) -> Value {
        let mut cfg = json!({"cmd": self.cmd, "args": self.args, "envs": self.envs});
        if let Some(c) = &self.cwd { cfg["cwd"] = json!(c); }
        let mut v = json!({"config": cfg, "pid": self.pid}); if let Some(t) = &self.tag { v["tag"] = json!(t); } v
    }
}
fn procs() -> &'static Mutex<HashMap<i32, Arc<Proc>>> { static P: OnceLock<Mutex<HashMap<i32, Arc<Proc>>>> = OnceLock::new(); P.get_or_init(|| Mutex::new(HashMap::new())) }

fn select(sel: &Value) -> Result<Arc<Proc>, CErr> {
    let sel = sel.get("process").unwrap_or(sel);
    let g = procs().lock().unwrap();
    if let Some(pid) = sel.get("pid").and_then(|p| p.as_i64()) { return g.get(&(pid as i32)).cloned().ok_or_else(|| cerr("not_found", format!("process with pid {pid} not found"))); }
    if let Some(tag) = sel.get("tag").and_then(|t| t.as_str()) { return g.values().find(|p| p.tag.as_deref() == Some(tag)).cloned().ok_or_else(|| cerr("not_found", format!("process with tag '{tag}' not found"))); }
    Err(cerr("invalid_argument", "process selector (pid or tag) is required"))
}

fn set_winsize(fd: RawFd, cols: u16, rows: u16) { let ws = libc::winsize { ws_row: rows, ws_col: cols, ws_xpixel: 0, ws_ypixel: 0 }; unsafe { libc::ioctl(fd, libc::TIOCSWINSZ as _, &ws); } }
fn read_fd_loop(fd: RawFd, kind: &'static str, p: Arc<Proc>, done: Sender<()>) {
    let mut b = vec![0u8; 32768];
    loop { let n = unsafe { libc::read(fd, b.as_mut_ptr() as *mut _, b.len()) }; if n <= 0 { break; } p.emit(Ev::Out(kind, b[..n as usize].to_vec())); }
    let _ = done.send(());
}

/// Spawn a process the way envd does (any user, the request's env over the sandbox env over the user's base env) and register it.
/// `first` is subscribed before any output can arrive.
fn spawn_proc(name: &str, p: &Value, pty: Option<(u16, u16)>, tag: Option<String>, stdin_enabled: bool, first: Sender<Ev>) -> Result<Arc<Proc>, CErr> {
    let (uid, gid, home) = home_of(name)?;
    let cmd = p.get("cmd").and_then(|c| c.as_str()).filter(|c| !c.is_empty()).ok_or_else(|| cerr("invalid_argument", "process.cmd is required"))?.to_string();
    let args: Vec<String> = p.get("args").and_then(|a| a.as_array()).map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect()).unwrap_or_default();
    let envs: Map<String, Value> = p.get("envs").and_then(|e| e.as_object()).cloned().unwrap_or_default();
    let cwd = p.get("cwd").and_then(|c| c.as_str()).filter(|c| !c.is_empty()).map(|c| c.to_string());
    let workdir = cwd.as_ref().map(|c| resolve(c, &home)).unwrap_or_else(|| PathBuf::from(&home));
    if !workdir.is_dir() { return Err(cerr("invalid_argument", format!("cwd '{}' does not exist", workdir.display()))); }
    let mut env = base_env(name, &home); env.extend(sandbox_env());
    for (k, v) in &envs { env.push((k.clone(), v.as_str().map(|s| s.to_string()).unwrap_or_else(|| v.to_string()))); }
    let mut c = Command::new(&cmd); c.args(&args).current_dir(&workdir).env_clear().envs(env.iter().map(|(k, v)| (k, v)));
    let (done_tx, done_rx) = mpsc::channel::<()>();
    let (mut master, mut readers) = (None, 0usize);
    if let Some((cols, rows)) = pty {
        let (mut m, mut s) = (0 as RawFd, 0 as RawFd);
        if unsafe { libc::openpty(&mut m, &mut s, std::ptr::null_mut(), std::ptr::null(), std::ptr::null()) } != 0 { return Err(cerr("internal", "openpty failed")); }
        set_winsize(m, cols, rows);
        let (s1, s2, s3) = unsafe { (libc::dup(s), libc::dup(s), s) };
        c.stdin(unsafe { Stdio::from_raw_fd(s1) }).stdout(unsafe { Stdio::from_raw_fd(s2) }).stderr(unsafe { Stdio::from_raw_fd(s3) });
        unsafe { c.pre_exec(move || drop_privs(uid, gid, true)); } master = Some(m);
    } else {
        c.stdin(if stdin_enabled { Stdio::piped() } else { Stdio::null() }).stdout(Stdio::piped()).stderr(Stdio::piped());
        unsafe { c.pre_exec(move || drop_privs(uid, gid, false)); }
    }
    let mut child = c.spawn().map_err(|e| cerr("invalid_argument", format!("error starting process: {e}")))?;
    let pid = child.id() as i32;
    let proc_ = Arc::new(Proc { pid, tag, cmd, args, envs, cwd, master, stdin: Mutex::new(if master.is_none() { child.stdin.take() } else { None }), stdin_enabled: stdin_enabled || master.is_some(), subs: Mutex::new(vec![first]) });
    procs().lock().unwrap().insert(pid, proc_.clone());
    if let Some(m) = master { readers += 1; let (pp, d) = (proc_.clone(), done_tx.clone()); thread::spawn(move || read_fd_loop(m, "pty", pp, d)); }
    else {
        use std::os::unix::io::IntoRawFd;
        let (o, e) = (child.stdout.take().unwrap().into_raw_fd(), child.stderr.take().unwrap().into_raw_fd());
        for (fd, kind) in [(o, "stdout"), (e, "stderr")] { readers += 1; let (pp, d) = (proc_.clone(), done_tx.clone()); thread::spawn(move || { read_fd_loop(fd, kind, pp, d); unsafe { libc::close(fd); } }); }
    }
    let pp = proc_.clone();
    thread::spawn(move || {
        let st = child.wait();
        for _ in 0..readers { if done_rx.recv_timeout(Duration::from_millis(400)).is_err() { break; } }   // drained, or a background child still holds the pipe
        let (code, status) = match st { Ok(s) => match (s.code(), s.signal()) { (Some(c), _) => (c, format!("exit status {c}")), (None, Some(sig)) => (-1, format!("signal: {}", sig_name(sig))), _ => (-1, "unknown".into()) }, Err(e) => (-1, e.to_string()) };
        procs().lock().unwrap().remove(&pp.pid);
        pp.emit(Ev::End(code, status));
        if let Some(m) = pp.master { unsafe { libc::close(m); } }
    });
    Ok(proc_)
}
fn sig_name(s: i32) -> String { match s { 9 => "killed".into(), 15 => "terminated".into(), 2 => "interrupt".into(), 11 => "segmentation fault".into(), n => n.to_string() } }

fn pump_events(w: &mut UnixStream, rx: &Receiver<Ev>, pid: i32, ka: Duration) -> io::Result<()> {
    stream_frame(w, 0, &json!({"event": {"start": {"pid": pid}}}))?;
    loop {
        match rx.recv_timeout(ka) {
            Ok(Ev::Out(kind, data)) => stream_frame(w, 0, &json!({"event": {"data": {kind: B64.encode(&data)}}}))?,
            Ok(Ev::End(code, status)) => { stream_frame(w, 0, &json!({"event": {"end": {"exitCode": code, "exited": true, "status": status}}}))?; break; }
            Err(mpsc::RecvTimeoutError::Timeout) => stream_frame(w, 0, &json!({"event": {"keepalive": {}}}))?,
            Err(_) => break,
        }
    }
    stream_end(w, None)
}
fn keepalive(req: &Request) -> Duration { Duration::from_secs(req.h("keepalive-ping-interval").and_then(|v| v.parse::<u64>().ok()).unwrap_or(50).clamp(1, 600)) }

fn process_stream(w: &mut UnixStream, rd: &mut BufReader<UnixStream>, req: &Request, method: &str) -> io::Result<()> {
    let first = match first_frame(&mut Body::new(rd, req)) { Ok(v) => v, Err(e) => return respond_err(w, &e) };
    let (tx, rx) = mpsc::channel::<Ev>();
    let pid = if method == "Start" {
        let Some(p) = first.get("process") else { return respond_err(w, &cerr("invalid_argument", "process is required")) };
        let pty = first.get("pty").map(|t| { let s = t.get("size").unwrap_or(t); (s.get("cols").and_then(|c| c.as_u64()).unwrap_or(80) as u16, s.get("rows").and_then(|r| r.as_u64()).unwrap_or(24) as u16) });
        let tag = first.get("tag").and_then(|t| t.as_str()).map(|t| t.to_string()); let stdin = first.get("stdin").and_then(|s| s.as_bool()).unwrap_or(true);
        match spawn_proc(&req_user(req), p, pty, tag, stdin, tx) { Ok(pr) => pr.pid, Err(e) => return respond_err(w, &e) }
    } else {
        match select(&first) { Ok(pr) => { pr.subs.lock().unwrap().push(tx); pr.pid } Err(e) => return respond_err(w, &e) }
    };
    stream_begin(w)?; let _ = pump_events(w, &rx, pid, keepalive(req)); Ok(())
}

fn process_rpc(method: &str, body: &Value) -> Result<Value, CErr> {
    match method {
        "List" => Ok(json!({"processes": procs().lock().unwrap().values().map(|p| p.info()).collect::<Vec<_>>()})),
        "SendInput" => {
            let p = select(body)?; let inp = body.get("input").ok_or_else(|| cerr("invalid_argument", "input is required"))?;
            let (data, pty) = if let Some(s) = inp.get("pty").and_then(|v| v.as_str()) { (s, true) } else { (inp.get("stdin").and_then(|v| v.as_str()).unwrap_or(""), false) };
            let bytes = B64.decode(data).map_err(|e| cerr("invalid_argument", format!("input is not base64: {e}")))?;
            let _ = pty;
            if let Some(m) = p.master { if unsafe { libc::write(m, bytes.as_ptr() as *const _, bytes.len()) } < 0 { return Err(cerr("internal", "write to pty failed")); } }
            else { let mut g = p.stdin.lock().unwrap(); match g.as_mut() { Some(s) => { s.write_all(&bytes).and_then(|_| s.flush()).map_err(|e| cerr("internal", format!("write to stdin failed: {e}")))?; }
                None => return Err(cerr("failed_precondition", if p.stdin_enabled { "stdin is closed" } else { "stdin was not enabled for this process" })) } }
            Ok(json!({}))
        }
        "CloseStdin" => { let p = select(body)?; if let Some(m) = p.master { let eot = [4u8]; unsafe { libc::write(m, eot.as_ptr() as *const _, 1); } } else { *p.stdin.lock().unwrap() = None; } Ok(json!({})) }
        "SendSignal" => {
            let p = select(body)?; let sig = match body.get("signal").and_then(|s| s.as_str()) { Some("SIGNAL_SIGKILL") => libc::SIGKILL, Some("SIGNAL_SIGTERM") => libc::SIGTERM, Some(o) => return Err(cerr("invalid_argument", format!("unsupported signal {o}"))), None => return Err(cerr("invalid_argument", "signal is required")) };
            unsafe { if libc::kill(-p.pid, sig) != 0 { libc::kill(p.pid, sig); } } Ok(json!({}))
        }
        "Update" => {
            let p = select(body)?;
            if let (Some(m), Some(sz)) = (p.master, body.get("pty").and_then(|t| t.get("size"))) { set_winsize(m, sz.get("cols").and_then(|c| c.as_u64()).unwrap_or(80) as u16, sz.get("rows").and_then(|r| r.as_u64()).unwrap_or(24) as u16); }
            Ok(json!({}))
        }
        other => Err(cerr("unimplemented", format!("process.Process/{other} is not implemented"))),
    }
}

// ---------------- routing ----------------
fn route(w: &mut UnixStream, rd: &mut BufReader<UnixStream>, req: &Request) -> io::Result<()> {
    match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/health") => return respond(w, 204, "", b""),
        ("GET", "/files") => return files_get(w, req),
        ("POST", "/files") => return files_post(w, rd, req),
        ("GET", "/envs") => return respond_json(w, &Value::Object(sandbox_env().into_iter().map(|(k, v)| (k, json!(v))).collect())),
        _ => {}
    }
    let Some((svc, method)) = req.path.strip_prefix('/').and_then(|p| p.split_once('/')) else { return respond(w, 404, "application/json", br#"{"code":"not_found","message":"no route"}"#) };
    if req.method != "POST" || !(svc == "process.Process" || svc == "filesystem.Filesystem") { return respond(w, 404, "application/json", br#"{"code":"not_found","message":"no route"}"#); }
    let ct = req.h("content-type").unwrap_or("").to_ascii_lowercase();
    if svc == "process.Process" && (method == "Start" || method == "Connect") {
        if !ct.starts_with("application/connect+json") { return respond(w, 415, "application/json", br#"{"code":"unknown","message":"only the Connect JSON codec is supported"}"#); }
        return process_stream(w, rd, req, method);
    }
    if svc == "filesystem.Filesystem" && method == "WatchDir" {
        if !ct.starts_with("application/connect+json") { return respond(w, 415, "application/json", br#"{"code":"unknown","message":"only the Connect JSON codec is supported"}"#); }
        return watch_stream(w, rd, req);
    }
    if method == "StreamInput" { return respond_err(w, &cerr("unimplemented", format!("{svc}/{method} is not implemented"))); }
    if !ct.starts_with("application/json") { return respond(w, 415, "application/json", br#"{"code":"unknown","message":"only the Connect JSON codec is supported"}"#); }
    let raw = match read_all(&mut Body::new(rd, req), 8 << 20) { Ok(b) => b, Err(e) => return respond_err(w, &e) };
    let body: Value = if raw.is_empty() { json!({}) } else { match serde_json::from_slice(&raw) { Ok(v) => v, Err(e) => return respond_err(w, &cerr("invalid_argument", format!("bad JSON: {e}"))) } };
    let res = if svc == "process.Process" { process_rpc(method, &body) } else { fs_rpc(method, req, &body) };
    match res { Ok(v) => respond_json(w, &v), Err(e) => respond_err(w, &e) }
}

pub fn serve_conn(stream: UnixStream) {
    let Ok(rs) = stream.try_clone() else { return };
    let mut rd = BufReader::with_capacity(65536, rs); let mut w = stream;
    match read_head(&mut rd) {
        Ok(Some(req)) => { if let Err(e) = route(&mut w, &mut rd, &req) { eprintln!("envd: {} {}: {e}", req.method, req.path); } }
        Ok(None) => {}
        Err(_) => { let _ = respond(&mut w, 400, "application/json", br#"{"code":"invalid_argument","message":"bad request"}"#); }
    }
    let _ = w.flush(); let _ = w.shutdown(std::net::Shutdown::Both);
}


#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn req(headers: &[(&str, &str)]) -> Request { Request { method: "POST".into(), path: "/x".into(), query: vec![], headers: headers.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect() } }

    #[test] fn percent_decoding() {
        assert_eq!(pct_decode("%2Ftmp%2Fa%20b.txt"), "/tmp/a b.txt"); assert_eq!(pct_decode("a+b"), "a b"); assert_eq!(pct_decode("100%"), "100%"); assert_eq!(pct_decode("%zz"), "%zz");
    }
    #[test] fn head_parse_with_query() {
        let mut c = Cursor::new(b"GET /files?path=%2Ftmp%2Fa.txt&username=root HTTP/1.1\r\nHost: x\r\nE2b-Sandbox-Id: sb1\r\n\r\n".to_vec());
        let r = read_head(&mut c).unwrap().unwrap();
        assert_eq!(r.method, "GET"); assert_eq!(r.path, "/files"); assert_eq!(r.q("path"), Some("/tmp/a.txt")); assert_eq!(r.q("username"), Some("root")); assert_eq!(r.h("e2b-sandbox-id"), Some("sb1"));
    }
    #[test] fn chunked_body_roundtrip() {
        let mut c = Cursor::new(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\nNEXT".to_vec());
        let r = req(&[("transfer-encoding", "chunked")]);
        let mut b = Body::new(&mut c, &r); let mut out = Vec::new(); b.read_to_end(&mut out).unwrap();
        assert_eq!(out, b"hello world");
    }
    #[test] fn fixed_body_stops_at_length() {
        let mut c = Cursor::new(b"abcdefgh".to_vec()); let r = req(&[("content-length", "3")]);
        let mut out = Vec::new(); Body::new(&mut c, &r).read_to_end(&mut out).unwrap(); assert_eq!(out, b"abc");
    }
    #[test] fn connect_envelope_first_frame() {
        let j = br#"{"process":{"cmd":"/bin/bash","args":["-l","-c","echo hi"]},"stdin":false}"#;
        let mut env = vec![0u8]; env.extend_from_slice(&(j.len() as u32).to_be_bytes()); env.extend_from_slice(j);
        let v = first_frame(&mut Cursor::new(env)).ok().unwrap(); assert_eq!(v["process"]["args"][2], "echo hi"); assert_eq!(v["stdin"], false);
        let mut comp = vec![1u8, 0, 0, 0, 0]; assert!(first_frame(&mut Cursor::new(std::mem::take(&mut comp))).is_err());
    }
    #[test] fn stream_frames_are_chunked_envelopes() {
        let mut out = Vec::new(); stream_frame(&mut out, 0, &json!({"a": 1})).unwrap(); stream_end(&mut out, None).unwrap();
        let s = String::from_utf8_lossy(&out).into_owned();
        assert!(s.starts_with("c\r\n")); assert!(s.ends_with("0\r\n\r\n")); assert!(s.contains("\u{2}") || out.windows(1).any(|w| w[0] == 2));
    }
    #[test] fn multipart_single_and_many() {
        let body = b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"/tmp/a.txt\"\r\nContent-Type: text/plain\r\n\r\nhello\r\n--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"/tmp/b.bin\"\r\n\r\n\x00\x01\r\n--B--\r\n";
        let p = multipart_parts(body, "B"); assert_eq!(p.len(), 2); assert_eq!(p[0].0, "/tmp/a.txt"); assert_eq!(p[0].1, b"hello"); assert_eq!(p[1].1, vec![0u8, 1]);
        assert_eq!(multipart_boundary("multipart/form-data; boundary=\"B\""), Some("B".to_string()));
    }
    #[test] fn timestamps_and_permissions() {
        assert_eq!(rfc3339(0, 0), "1970-01-01T00:00:00.000000000Z"); assert_eq!(rfc3339(1_793_849_000, 5), "2026-11-05T03:23:20.000000005Z");
        assert_eq!(perm_string(0o755, true, false), "drwxr-xr-x"); assert_eq!(perm_string(0o644, false, false), "-rw-r--r--");
    }
    #[test] fn path_resolution() {
        assert_eq!(resolve("a/b", "/home/agent"), PathBuf::from("/home/agent/a/b")); assert_eq!(resolve("~/x", "/home/agent"), PathBuf::from("/home/agent/x")); assert_eq!(resolve("/etc", "/h"), PathBuf::from("/etc"));
    }
    #[test] fn basic_auth_user() {
        let r = req(&[("authorization", "Basic cm9vdDo=")]); assert_eq!(req_user(&r), "root"); assert_eq!(req_user(&req(&[])), DEFAULT_USER);
    }
    #[test] fn files_roundtrip_and_fs_rpc() {
        let d = std::env::temp_dir().join(format!("envd-test-{}", std::process::id())); let _ = fs::remove_dir_all(&d); fs::create_dir_all(&d).unwrap();
        let r = req(&[("authorization", "Basic cm9vdDo=")]);   // root: exists in any Linux image
        let p = d.join("sub/dir");
        let v = fs_rpc("MakeDir", &r, &json!({"path": p.to_string_lossy()})).ok().unwrap(); assert_eq!(v["entry"]["type"], "FILE_TYPE_DIRECTORY");
        assert_eq!(fs_rpc("MakeDir", &r, &json!({"path": p.to_string_lossy()})).err().unwrap().code, "already_exists");
        fs::write(p.join("f.txt"), b"hi").unwrap();
        let l = fs_rpc("ListDir", &r, &json!({"path": d.to_string_lossy(), "depth": 3})).ok().unwrap(); assert_eq!(l["entries"].as_array().unwrap().len(), 3);
        let m = fs_rpc("Move", &r, &json!({"source": p.join("f.txt").to_string_lossy(), "destination": d.join("g.txt").to_string_lossy()})).ok().unwrap(); assert_eq!(m["entry"]["size"], "2");
        assert_eq!(fs_rpc("Stat", &r, &json!({"path": d.join("nope").to_string_lossy()})).err().unwrap().code, "not_found");
        let w = fs_rpc("CreateWatcher", &r, &json!({"path": d.to_string_lossy()})).ok().unwrap(); let id = w["watcherId"].as_str().unwrap().to_string();
        fs::write(d.join("new.txt"), b"x").unwrap();
        let e = fs_rpc("GetWatcherEvents", &r, &json!({"watcherId": id})).ok().unwrap(); assert!(e["events"].as_array().unwrap().iter().any(|x| x["name"] == "new.txt" && x["type"] == "EVENT_TYPE_CREATE"));
        fs_rpc("Remove", &r, &json!({"path": d.to_string_lossy()})).ok().unwrap(); assert!(!d.exists());
    }
    #[test] fn watch_diff_reports_create_write_remove() {
        let root = PathBuf::from("/w"); let mut old: HashMap<PathBuf, (u64, i64, i64)> = HashMap::new(); let mut now = old.clone();
        old.insert(PathBuf::from("/w/gone"), (1, 1, 1)); old.insert(PathBuf::from("/w/same"), (1, 1, 1)); old.insert(PathBuf::from("/w/edit"), (1, 1, 1));
        now.insert(PathBuf::from("/w/same"), (1, 1, 1)); now.insert(PathBuf::from("/w/edit"), (2, 2, 2)); now.insert(PathBuf::from("/w/new"), (1, 1, 1));
        let mut ev: Vec<String> = diff_events(&root, &old, &now).iter().map(|e| format!("{}:{}", e["name"].as_str().unwrap(), e["type"].as_str().unwrap())).collect(); ev.sort();
        assert_eq!(ev, vec!["edit:EVENT_TYPE_WRITE", "gone:EVENT_TYPE_REMOVE", "new:EVENT_TYPE_CREATE"]);
    }
    #[test] fn process_start_stream_and_signal() {
        // Start /bin/sh as root (the test runs as root in the container; otherwise skip), collect events through the real subscription path.
        if unsafe { libc::geteuid() } != 0 { return; }
        let (tx, rx) = mpsc::channel(); let p = json!({"cmd": "/bin/sh", "args": ["-c", "echo out; echo err >&2; exit 3"], "cwd": "/tmp"});
        let pr = spawn_proc("root", &p, None, Some("t1".into()), false, tx).ok().unwrap();
        let (mut out, mut err) = (String::new(), String::new()); let end;
        loop { match rx.recv_timeout(Duration::from_secs(5)).unwrap() { Ev::Out("stdout", d) => out.push_str(&String::from_utf8_lossy(&d)), Ev::Out(_, d) => err.push_str(&String::from_utf8_lossy(&d)), Ev::End(c, s) => { end = Some((c, s)); break } } }
        assert_eq!(out, "out\n"); assert_eq!(err, "err\n"); assert_eq!(end, Some((3, "exit status 3".to_string()))); assert_eq!(pr.tag.as_deref(), Some("t1"));
        // a long-running process can be selected by tag and killed with a signal
        let (tx2, rx2) = mpsc::channel(); let pr2 = spawn_proc("root", &json!({"cmd": "/bin/sh", "args": ["-c", "sleep 30"]}), None, Some("sl".into()), true, tx2).ok().unwrap();
        assert_eq!(select(&json!({"process": {"tag": "sl"}})).ok().unwrap().pid, pr2.pid);
        process_rpc("SendSignal", &json!({"process": {"pid": pr2.pid}, "signal": "SIGNAL_SIGKILL"})).ok().unwrap();
        loop { if let Ev::End(c, s) = rx2.recv_timeout(Duration::from_secs(5)).unwrap() { assert_eq!(c, -1); assert_eq!(s, "signal: killed"); break; } }
    }
}
