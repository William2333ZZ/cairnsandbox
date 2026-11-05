package e2b

// The envd side of the E2B surface (docs/research/e2b-compat-2026-11.md): the SDKs talk to a sandbox with REST (/health, /files) and Connect-RPC JSON
// (process.Process, filesystem.Filesystem), every request carrying E2b-Sandbox-Id and X-Access-Token. They are pointed here with E2B_SANDBOX_URL, so one URL
// serves every sandbox. The control plane authenticates the caller with a token bound to that one sandbox, audits what the node alone could not see (the command
// line of a Start, the path of a file), and relays the bytes to the node (`/sandboxes/<id>/envd/...`), which pipes them over vsock to sandboxd's envd listener.

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
)

// Counters for the envd side, rendered by MetricsText (the main port's /metrics appends them). Process-wide and per replica, like the rest of /metrics.
var envdStats struct {
	mu       sync.Mutex
	requests map[string]*atomic.Int64 // "<kind>|<status class>"
	active   atomic.Int64             // relays in flight (a Start stream stays open for the whole command)
	bytesIn  atomic.Int64             // request bodies relayed toward sandboxes
	bytesOut atomic.Int64             // response bodies relayed back
	denied   atomic.Int64             // requests refused for a missing or wrong token
}

func envdKind(path string) string {
	switch {
	case path == "/health":
		return "health"
	case path == "/files":
		return "files"
	case path == "/process.Process/Start":
		return "start"
	case path == "/process.Process/Connect":
		return "connect"
	case strings.HasPrefix(path, "/process.Process/"):
		return "process_rpc"
	case strings.HasPrefix(path, "/filesystem.Filesystem/"):
		return "filesystem_rpc"
	}
	return "other"
}

func envdCount(kind string, status int) {
	key := kind + "|" + string(rune('0'+status/100)) + "xx"
	envdStats.mu.Lock()
	if envdStats.requests == nil {
		envdStats.requests = map[string]*atomic.Int64{}
	}
	c := envdStats.requests[key]
	if c == nil {
		c = &atomic.Int64{}
		envdStats.requests[key] = c
	}
	envdStats.mu.Unlock()
	c.Add(1)
}

// MetricsText: Prometheus lines for the envd side of the E2B surface.
func MetricsText() string {
	var b strings.Builder
	envdStats.mu.Lock()
	keys := make([]string, 0, len(envdStats.requests))
	for k := range envdStats.requests {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		kind, class, _ := strings.Cut(k, "|")
		fmt.Fprintf(&b, "cairn_envd_requests_total{kind=%q,status=%q} %d\n", kind, class, envdStats.requests[k].Load())
	}
	envdStats.mu.Unlock()
	fmt.Fprintf(&b, "cairn_envd_active_relays %d\ncairn_envd_bytes_total{dir=\"to_sandbox\"} %d\ncairn_envd_bytes_total{dir=\"from_sandbox\"} %d\ncairn_envd_auth_denied_total %d\n",
		envdStats.active.Load(), envdStats.bytesIn.Load(), envdStats.bytesOut.Load(), envdStats.denied.Load())
	return b.String()
}

// statusWriter remembers the status and counts the bytes written back.
type statusWriter struct {
	http.ResponseWriter
	status int
	n      int64
}

func (w *statusWriter) WriteHeader(c int) { w.status = c; w.ResponseWriter.WriteHeader(c) }
func (w *statusWriter) Write(p []byte) (int, error) {
	if w.status == 0 {
		w.status = 200
	}
	n, err := w.ResponseWriter.Write(p)
	w.n += int64(n)
	return n, err
}
func (w *statusWriter) Flush() {
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

type countingBody struct {
	io.Reader
	n *int64
}

func (c countingBody) Read(p []byte) (int, error) {
	n, err := c.Reader.Read(p)
	*c.n += int64(n)
	return n, err
}

// EnvdVersion is what we tell the SDKs. They gate behaviour on it (e2b/envd/versions.py): 0.5.7 is the newest version whose features we implement
// (octet-stream upload, stdin, default user; not file metadata or watch entry info).
const EnvdVersion = "0.5.7"

type Auditor interface{ Write(map[string]any) }

// EnvdToken is the access token handed out at create time: an HMAC of the sandbox id under a secret every replica shares, so it needs no storage and
// opens exactly one sandbox.
func (s *Server) EnvdToken(sid string) string {
	if len(s.Secret) == 0 {
		return ""
	}
	m := hmac.New(sha256.New, s.Secret)
	m.Write([]byte("envd|" + sid))
	return hex.EncodeToString(m.Sum(nil))
}

func (s *Server) envdAuthorized(r *http.Request, sid string) bool {
	want := s.EnvdToken(sid)
	if want == "" {
		return false
	}
	return hmac.Equal([]byte(r.Header.Get("X-Access-Token")), []byte(want))
}

func isEnvdPath(p string) bool {
	if strings.Contains(p, "..") || strings.Contains(p, "//") {
		return false
	}
	return p == "/health" || p == "/files" || p == "/envs" || strings.HasPrefix(p, "/process.Process/") || strings.HasPrefix(p, "/filesystem.Filesystem/")
}

func connectErr(w http.ResponseWriter, status int, code, msg string) {
	b, _ := json.Marshal(map[string]any{"code": code, "message": msg})
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	w.Write(b)
}

// serveEnvd handles one SDK -> sandbox request.
func (s *Server) serveEnvd(w0 http.ResponseWriter, r *http.Request) {
	sid := r.Header.Get("E2b-Sandbox-Id")
	path := r.URL.Path
	sw := &statusWriter{ResponseWriter: w0}
	var w http.ResponseWriter = sw
	envdStats.active.Add(1)
	var upBytes int64
	defer func() {
		envdStats.active.Add(-1)
		envdStats.bytesIn.Add(upBytes)
		envdStats.bytesOut.Add(sw.n)
		if sw.status == 0 {
			sw.status = 200
		}
		envdCount(envdKind(path), sw.status)
	}()
	if !isEnvdPath(path) {
		connectErr(w, 404, "not_found", "no route")
		return
	}
	if !s.envdAuthorized(r, sid) {
		envdStats.denied.Add(1)
		connectErr(w, 401, "unauthenticated", "invalid or missing X-Access-Token for this sandbox")
		return
	}
	rt, err := s.Life.RouteErr(sid)
	if err != nil {
		connectErr(w, 503, "unavailable", "registry unavailable")
		return
	}
	if rt == nil { // E2B's proxy answers 502 "... was not found" for a sandbox that is gone (killed, timed out); the SDKs key is_running() and SandboxNotRunningException on it
		connectErr(w, 502, "unavailable", "The sandbox "+sid+" was not found")
		return
	}
	node := s.Fleet.Get(rt.Node)
	if node == nil || !node.Alive() { // "port is not open" is the proxy's words for a sandbox that exists but whose envd cannot be reached
		connectErr(w, 502, "unavailable", "The sandbox "+sid+" exists but its envd port is not open: the node is not reachable")
		return
	}
	project := s.Life.ProjectOf(sid)
	if !id0Allowed(s, project, r) {
		connectErr(w, 403, "permission_denied", "read-only or suspended")
		return
	}

	var body io.Reader = r.Body
	if r.Body == nil || r.Body == http.NoBody {
		body = nil
	}
	ev := map[string]any{"op": "envd", "sandbox": sid, "project": project, "method": r.Method, "path": path, "user": basicUser(r.Header.Get("Authorization"))}
	switch {
	case path == "/process.Process/Start" && body != nil:
		// The request is one Connect envelope: read it to audit the command, then send the same bytes on.
		head := make([]byte, 5)
		if _, err := io.ReadFull(body, head); err != nil {
			connectErr(w, 400, "invalid_argument", "bad envelope")
			return
		}
		n := int(binary.BigEndian.Uint32(head[1:]))
		if n < 0 || n > 8<<20 {
			connectErr(w, 400, "invalid_argument", "message too large")
			return
		}
		payload := make([]byte, n)
		if _, err := io.ReadFull(body, payload); err != nil {
			connectErr(w, 400, "invalid_argument", "bad envelope")
			return
		}
		var st struct {
			Process struct {
				Cmd  string   `json:"cmd"`
				Args []string `json:"args"`
				Cwd  string   `json:"cwd"`
			} `json:"process"`
			Tag string `json:"tag"`
			PTY any    `json:"pty"`
		}
		if json.Unmarshal(payload, &st) == nil {
			ev["rpc"] = "Start"
			ev["cmd"] = clip(strings.TrimSpace(st.Process.Cmd+" "+strings.Join(st.Process.Args, " ")), 400)
			ev["cwd"], ev["tag"], ev["pty"] = st.Process.Cwd, st.Tag, st.PTY != nil
		}
		body = io.MultiReader(bytes.NewReader(head), bytes.NewReader(payload), body)
	case strings.HasPrefix(path, "/process.Process/") || strings.HasPrefix(path, "/filesystem.Filesystem/"):
		ev["rpc"] = path[strings.LastIndex(path, "/")+1:]
	case path == "/files":
		ev["file"] = clip(r.URL.Query().Get("path"), 300)
	}
	if path != "/health" {
		s.Audit.Write(ev)
	}

	hdr := http.Header{}
	for k, vs := range r.Header {
		switch strings.ToLower(k) {
		case "host", "authorization", "x-access-token", "x-api-key", "connection", "proxy-authorization", "keep-alive", "te", "upgrade":
			continue
		}
		for _, v := range vs {
			hdr.Add(k, v)
		}
	}
	if a := r.Header.Get("Authorization"); a != "" {
		hdr.Set("X-Envd-Authorization", a) // the node bearer takes the Authorization header on this hop
	}
	uri := "/sandboxes/" + sid + "/envd" + path
	if r.URL.RawQuery != "" {
		uri += "?" + r.URL.RawQuery
	}
	cl := r.ContentLength
	if body != nil {
		body = countingBody{Reader: body, n: &upBytes}
	}
	resp, err := s.NC.DoStream(node, r.Method, uri, body, cl, hdr)
	if err != nil {
		connectErr(w, 503, "unavailable", "node unreachable: "+err.Error())
		return
	}
	defer resp.Body.Close()
	for _, k := range []string{"Content-Type", "Content-Length"} {
		if v := resp.Header.Get(k); v != "" {
			w.Header().Set(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	flusher, _ := w.(http.Flusher)
	buf := make([]byte, 32768)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return // the SDK went away; the process keeps running in the sandbox
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if rerr != nil {
			return
		}
	}
}

// id0Allowed: a suspended account cannot start processes or write files (a paused sandbox would be resumed by the relay, which is exactly what the
// suspension stops); reads of /health and the like are fine.
func id0Allowed(s *Server, project string, r *http.Request) bool {
	p, err := s.Tenancy.Project(project)
	if err != nil {
		return true
	}
	if _, on := s.Tenancy.SuspensionOf(p.Account); on && r.Method != "GET" {
		return false
	}
	return true
}

func basicUser(h string) string {
	h = strings.TrimPrefix(h, "Basic ")
	if d, err := base64.StdEncoding.DecodeString(strings.TrimSpace(h)); err == nil {
		return strings.SplitN(string(d), ":", 2)[0]
	}
	return ""
}

func clip(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}
