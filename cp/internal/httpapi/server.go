// Package httpapi is the HTTP surface (CONTRACT §1) over the contexts: authentication (tenancy), nodes (fleet), sandboxes (lifecycle + exec),
// templates (images), the store (state), the static workbench, /eval and /intake forwarding, and the change stream. It owns no data.
package httpapi

import (
	"bufio"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fcnode/cp/internal/abuse"
	"fcnode/cp/internal/audit"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"fcnode/cp/internal/e2b"
	"fcnode/cp/internal/exec"
	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/images"
	"fcnode/cp/internal/lifecycle"
	"fcnode/cp/internal/metering"
	"fcnode/cp/internal/network"
	"fcnode/cp/internal/state"
	"fcnode/cp/internal/tenancy"
	"fcnode/cp/internal/webhook"
)

type Auditor interface {
	Write(map[string]any)
	Replica() string
}

type Registry interface {
	Kind() string
	Where() string
	Ping() error
}

type Server struct {
	AuditExec     bool // D80: exec, file reads and writes get a row in the hash chain (command line redacted and cut to 200 characters)
	Reg           Registry
	Tenancy       *tenancy.Tenancy
	Fleet         *fleet.Fleet
	Life          *lifecycle.Lifecycle
	Images        *images.Images
	Builds        *images.Builds
	Exec          *exec.Exec
	Metering      *metering.Metering
	Volumes       *state.Volumes
	Creds         *network.Credentials
	NetReq        *network.Requests   // egress relaxations waiting for an owner (D36 补)
	Webhooks      *webhook.Dispatcher // D72: optional
	Abuse         *abuse.Watcher      // D58: optional; flags sandboxes that look like mining or flooding
	PreviewDomain string              // D55: "<port>-<sandbox>.<PreviewDomain>" reaches a sandbox's port from a browser (wildcard DNS + TLS are the deployer's)
	MaxBodyMiB    int64               // request body cap (default 512 MiB: image archives and volume tars come this way)
	RatePerMin    float64             // requests per minute per non-admin token (default 600); 0 = off
	AuditPath     string              // the replica's audit.jsonl, for /audit/verify
	limiter       *limiter
	counts        sync.Map // "2xx" | "3xx" | "4xx" | "5xx" | "rate_limited" -> *atomic.Int64 (this replica, since start)
	storeUp       storeProbe
	Store         state.Store
	EvalURL       string
	IntakeURL     string
	WebDir        string
	ConsoleDir    string // the console's static export, served under /console/ (empty = not installed)
	Audit         Auditor
}

// What a member (not the admin) may reach: their Agent service, their own sandboxes and templates, the evaluator's read-only views, accounts (own) and metering (own).
var memberPaths = []string{"/intake", "/health", "/me", "/eval/versions", "/eval/evolve", "/sandboxes", "/templates", "/accounts", "/projects", "/metering", "/volumes", "/builds", "/layers", "/network/requests", "/audit"}

var (
	reEvacuate  = regexp.MustCompile(`^/nodes/([^/]+)/evacuate$`)
	reNodeOp    = regexp.MustCompile(`^/nodes/([^/]+)/(cordon|uncordon|drain)$`)
	reMigrate   = regexp.MustCompile(`^/sandboxes/([^/]+)/migrate$`)
	reSandbox   = regexp.MustCompile(`^/sandboxes/([^/]+)(/.*)?$`)
	reProjects  = regexp.MustCompile(`^/accounts/([^/]+)/projects$`)
	reWebhooks  = regexp.MustCompile(`^/projects/([^/]+)/webhooks(/([^/]+)(/(test|enable))?)?$`)
	reKeys      = regexp.MustCompile(`^/projects/([^/]+)/keys(/([^/]+))?$`)
	reBilling   = regexp.MustCompile(`^/accounts/([^/]+)/(credit|balance|billing)$`)
	reTemplate  = regexp.MustCompile(`^/templates/([a-z0-9][a-z0-9_.-]{0,39})$`)
	reAccount   = regexp.MustCompile(`^/accounts/([^/]+)$`)
	reSuspend   = regexp.MustCompile(`^/accounts/([^/]+)/(suspend|unsuspend)$`)
	reMembers   = regexp.MustCompile(`^/projects/([^/]+)/members(/([^/]+))?$`)
	reRotate    = regexp.MustCompile(`^/projects/([^/]+)/members/([^/]+)/(rotate|expiry)$`)
	reQuota     = regexp.MustCompile(`^/projects/([^/]+)/quota$`)
	reStatement = regexp.MustCompile(`^/metering/projects/([^/]+)/(statement|today|statements|breakdown)$`)
	reNetDef    = regexp.MustCompile(`^/projects/([^/]+)/network$`)
	reLayerEvw  = regexp.MustCompile(`^/layers/([^/]+)/everywhere$`)
	reNetReq    = regexp.MustCompile(`^/network/requests/([^/]+)/(approve|deny)$`)
	reSbUsage   = regexp.MustCompile(`^/metering/sandboxes/([^/]+)$`)
	reVolume    = regexp.MustCompile(`^/volumes/([^/]+)$`)
	reCred      = regexp.MustCompile(`^/network/credentials/([^/]+)$`)
	reBuild     = regexp.MustCompile(`^/builds/([^/]+)$`)
	reBuildLog  = regexp.MustCompile(`^/builds/([^/]+)/log$`)
)

func sendJSON(w http.ResponseWriter, code int, v any) {
	b, _ := json.Marshal(v)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", fmt.Sprint(len(b)))
	w.WriteHeader(code)
	w.Write(b)
}

// ServeHTTP counts what the replica answered (for /metrics) and passes the request on; Hijack and Flush stay available for WebSocket tunnels and streams.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	sw := &statusWriter{ResponseWriter: w, code: 200}
	s.serve(sw, r)
	k := fmt.Sprintf("%dxx", sw.code/100)
	if sw.code == 429 {
		s.count("rate_limited")
	}
	s.count(k)
}

func (s *Server) count(k string) {
	v, _ := s.counts.LoadOrStore(k, new(atomic.Int64))
	v.(*atomic.Int64).Add(1)
}

type statusWriter struct {
	http.ResponseWriter
	code int
	set  bool
}

func (w *statusWriter) WriteHeader(c int) {
	if !w.set {
		w.code, w.set = c, true
	}
	w.ResponseWriter.WriteHeader(c)
}

func (w *statusWriter) Flush() {
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (w *statusWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	if h, ok := w.ResponseWriter.(http.Hijacker); ok {
		w.code, w.set = 101, true // a hijacked connection is an upgrade (the WebSocket tunnel): counted as 1xx, not an error
		return h.Hijack()
	}
	return nil, nil, errors.New("hijack not supported")
}

// previewHost: "<port>-<sandbox>.<domain>[:port]" -> (sandbox, port).
func previewHost(host, domain string) (string, int, bool) {
	if domain == "" {
		return "", 0, false
	}
	if i := strings.LastIndexByte(host, ':'); i > 0 && !strings.Contains(host[i:], "]") {
		host = host[:i]
	}
	host = strings.ToLower(host)
	sfx := "." + strings.ToLower(strings.TrimPrefix(domain, "."))
	if !strings.HasSuffix(host, sfx) {
		return "", 0, false
	}
	m := rePreview.FindStringSubmatch(strings.TrimSuffix(host, sfx))
	if m == nil {
		return "", 0, false
	}
	port, _ := strconv.Atoi(m[1])
	if port < 1 || port > 65535 {
		return "", 0, false
	}
	return m[2], port, true
}

var rePreview = regexp.MustCompile(`^(\d{1,5})-([a-z0-9][a-z0-9-]*)$`)

const previewCookie = "cairn_preview"

// preview turns a browser request for <port>-<sandbox>.<domain> into the path form the rest of the server already handles, and carries the bearer in a
// cookie (a browser cannot add headers to the page's own sub-requests). A first visit with ?token= sets the cookie and redirects so the token does not stay in the URL.
// Returns true when it answered the request itself.
func (s *Server) preview(w http.ResponseWriter, r *http.Request) bool {
	sid, port, ok := previewHost(r.Host, s.PreviewDomain)
	if !ok {
		return false
	}
	if t := r.URL.Query().Get("token"); t != "" && r.Method == "GET" {
		q := r.URL.Query()
		q.Del("token")
		http.SetCookie(w, &http.Cookie{Name: previewCookie, Value: t, Path: "/", HttpOnly: true, SameSite: http.SameSiteLaxMode, Secure: r.TLS != nil || r.Header.Get("X-Forwarded-Proto") == "https"})
		dst := "/" + strings.TrimLeft(r.URL.Path, "/") // never "//host": that would redirect off the preview site
		if e := q.Encode(); e != "" {
			dst += "?" + e
		}
		http.Redirect(w, r, dst, http.StatusFound)
		return true
	}
	if c, err := r.Cookie(previewCookie); err == nil && r.Header.Get("Authorization") == "" {
		r.Header.Set("Authorization", "Bearer "+c.Value)
	}
	var keep []string // the guest app must not see our credential
	for _, c := range r.Cookies() {
		if c.Name != previewCookie {
			keep = append(keep, c.Name+"="+c.Value)
		}
	}
	if len(keep) > 0 {
		r.Header.Set("Cookie", strings.Join(keep, "; "))
	} else {
		r.Header.Del("Cookie")
	}
	r.URL.Path = fmt.Sprintf("/sandboxes/%s/ports/%d%s", sid, port, r.URL.Path)
	r.URL.RawPath = ""
	return false
}

func (s *Server) serve(w http.ResponseWriter, r *http.Request) {
	if s.PreviewDomain != "" && s.preview(w, r) {
		return
	}
	path := r.URL.Path
	if path == "/" || strings.HasPrefix(path, "/ui") {
		s.static(w, path)
		return
	}
	if path == "/console" || strings.HasPrefix(path, "/console/") { // the Next.js console's static export (D51): its pages live under this prefix so they never collide with the API paths
		s.console(w, r)
		return
	}
	maxBody := s.MaxBodyMiB
	if maxBody <= 0 {
		maxBody = 512
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxBody<<20)
	raw, rerr := io.ReadAll(r.Body)
	if rerr != nil {
		sendJSON(w, 413, map[string]any{"error": fmt.Sprintf("request body over %d MiB", maxBody)})
		return
	}
	if path == "/intake/hooks/chat" && r.Method == "POST" { // the chat tool's webhook: no bearer, its own shared secret (verified by the Agent service)
		s.forward(w, r, s.IntakeURL, "intake", strings.TrimPrefix(r.URL.RequestURI(), "/intake"), raw, tenancy.Admin, []string{"X-Chat-Secret"})
		return
	}
	bearer := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	if bearer == "" {
		bearer = r.URL.Query().Get("token")
	}
	if path == "/status" && r.Method == "GET" { // D61: public and coarse; no names, no errors
		s.status(w)
		return
	}
	if path == "/signup" && r.Method == "POST" { // D60: no bearer; the invite code is the credential
		s.signup(w, r, raw)
		return
	}
	if path == "/login" && r.Method == "POST" {
		var b struct {
			Token any `json:"token"`
		}
		_ = json.Unmarshal(raw, &b)
		tok := ""
		if b.Token != nil {
			tok = fmt.Sprint(b.Token)
		}
		id, ok := s.Tenancy.Resolve(tok)
		code := 401
		if ok {
			code = 200
		}
		var user any
		if ok {
			user = id.User
		}
		sendJSON(w, code, map[string]any{"ok": ok, "auth": s.Tenancy.AuthEnabled(), "user": user, "admin": ok && id.Admin()})
		return
	}
	id, ok := s.Tenancy.Resolve(bearer)
	if path != "/health" && !ok {
		sendJSON(w, 401, map[string]any{"error": "bad token"})
		return
	}
	if ok && !id.Admin() && s.RatePerMin > 0 && !s.limit().allow(id.User, s.RatePerMin) { // 013: one member cannot flood the control plane
		w.Header().Set("Retry-After", "1")
		sendJSON(w, 429, map[string]any{"error": "rate limit", "per_min": s.RatePerMin})
		return
	}
	if path == "/metrics" && r.Method == "GET" {
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		s.metrics(w)
		return
	}
	if path == "/audit" && r.Method == "GET" { // the tail of this replica's audit log; members see the lines of their account's projects
		n := 200
		if v, err := strconv.Atoi(r.URL.Query().Get("n")); err == nil && v > 0 && v <= 5000 {
			n = v
		}
		if s.AuditPath == "" {
			sendJSON(w, 200, map[string]any{"events": []any{}})
			return
		}
		lines, err := audit.Tail(s.AuditPath, n*4)
		if err != nil {
			sendJSON(w, 500, map[string]any{"error": err.Error()})
			return
		}
		scope := s.accountScope(id)
		want := r.URL.Query().Get("project")
		out := []map[string]any{}
		for i := len(lines) - 1; i >= 0 && len(out) < n; i-- {
			ev := lines[i]
			p, _ := ev["project"].(string)
			if p == "" {
				p, _ = ev["for_project"].(string)
			}
			if scope != nil && !scope[p] {
				continue
			}
			if want != "" && p != want {
				continue
			}
			out = append(out, ev)
		}
		sendJSON(w, 200, map[string]any{"events": out})
		return
	}
	if path == "/audit/verify" && r.Method == "GET" {
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		if s.AuditPath == "" {
			sendJSON(w, 404, map[string]any{"error": "this replica keeps no audit file"})
			return
		}
		n, legacy, bad, err := audit.Verify(s.AuditPath)
		if err != nil {
			sendJSON(w, 500, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 200, map[string]any{"lines": n, "legacy": legacy, "ok": bad == 0, "first_bad": bad, "path": s.AuditPath})
		return
	}
	if path == "/me" && r.Method == "GET" {
		sendJSON(w, 200, s.Tenancy.Describe(id))
		return
	}
	if ok && !id.Admin() && path != "/events/stream" && path != "/health" {
		allowed := false
		for _, p := range memberPaths {
			if path == p || strings.HasPrefix(path, p+"/") {
				allowed = true
			}
		}
		if !allowed {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
	}
	if ok && id.Role == tenancy.RoleViewer && (r.Method != "GET" && r.Method != "HEAD" || isUpgrade(r)) { // D56: a viewer reads, nothing else (a WebSocket is a GET that can write)
		sendJSON(w, 403, map[string]any{"error": "read-only credential"})
		return
	}
	if ok && !id.Admin() { // D53: a suspended account can look at its sandboxes and delete them, nothing else
		if sp, on := s.Tenancy.SuspensionOf(id.Account); on && !suspendedMayDo(r, path) {
			sendJSON(w, 403, map[string]any{"error": "account suspended", "reason": sp.Reason, "since": sp.At})
			return
		}
	}
	if path == "/events/stream" && r.Method == "GET" {
		s.eventsStream(w, r)
		return
	}
	if strings.HasPrefix(path, "/eval/") || path == "/eval" {
		s.forward(w, r, s.EvalURL, "evaluator", orSlash(strings.TrimPrefix(r.URL.RequestURI(), "/eval")), raw, id.User, nil)
		return
	}
	if strings.HasPrefix(path, "/intake/") || path == "/intake" {
		s.forward(w, r, s.IntakeURL, "intake", orSlash(strings.TrimPrefix(r.URL.RequestURI(), "/intake")), raw, id.User, []string{"X-Agent"})
		return
	}
	s.api(w, r, path, raw, id)
}

// projectFor: the project a request names (X-Project header, body.project or ?project=), resolved and authorised for the identity.
func (s *Server) projectFor(r *http.Request, body map[string]any, id tenancy.Identity) (string, *int) {
	named := r.Header.Get("X-Project")
	if named == "" {
		named, _ = body["project"].(string)
	}
	if named == "" {
		named = r.URL.Query().Get("project")
	}
	p, err := s.Tenancy.ResolveProject(id, named)
	switch err {
	case nil:
		return p, nil
	case tenancy.ErrForbidden:
		c := 403
		return "", &c
	default:
		c := 404
		if !errors.Is(err, tenancy.ErrNotFound) {
			c = 503 // the tenancy database is unreachable: not a missing project
		}
		return "", &c
	}
}

// badProject is the body for a projectFor refusal: the code says which kind it is.
func badProject(code int) map[string]any {
	if code == 503 {
		return map[string]any{"error": "tenancy store unavailable"}
	}
	return map[string]any{"error": "no such project or not yours"}
}

// canSee: the admin sees everything; a member only their account's projects.
func (s *Server) canSee(id tenancy.Identity, project string) bool {
	if id.Admin() {
		return true
	}
	if id.Project != "" && project != id.Project {
		return false
	}
	p, err := s.Tenancy.Project(project)
	return err == nil && p.Account == id.Account
}

// accountScope: nil for the admin (everything), else the set of the member's projects.
func (s *Server) accountScope(id tenancy.Identity) map[string]bool {
	if id.Admin() {
		return nil
	}
	if id.Project != "" {
		return map[string]bool{id.Project: true}
	}
	ps, _ := s.Tenancy.Projects(id.Account)
	out := map[string]bool{}
	for _, p := range ps {
		out[p.ID] = true
	}
	return out
}

func (s *Server) api(w http.ResponseWriter, r *http.Request, path string, raw []byte, id tenancy.Identity) {
	body := map[string]any{}
	if len(raw) > 0 && !(r.Method == "PUT" && strings.Contains(path, "/files")) && path != "/builds/archive" && path != "/volumes/from-archive" {
		if err := json.Unmarshal(raw, &body); err != nil {
			sendJSON(w, 400, map[string]any{"error": "bad json"})
			return
		}
	}
	m := r.Method
	switch {
	case path == "/health" && m == "GET":
		nodes := map[string]any{}
		for _, n := range s.Fleet.All() {
			var running, total, memFree any
			if h := n.Health(); h != nil {
				running, total = h["vms_running"], h["vms_total"]
				memFree = fleet.NumOf(h, "mem_budget_mib") - fleet.NumOf(h, "mem_mib_running")
			}
			nodes[n.Name] = map[string]any{"alive": n.Alive(), "running": running, "total": total, "mem_free_mib": memFree, "last_err": n.LastErr()}
		}
		sendJSON(w, 200, map[string]any{"ok": true, "replica": s.Audit.Replica(), "registry": map[string]any{"kind": s.Reg.Kind(), "ok": s.Reg.Ping() == nil, "where": s.Reg.Where()}, "nodes": nodes, "routes": len(s.Life.Routes())})
	case path == "/users" && m == "GET":
		us, _ := s.Tenancy.UsersList()
		sendJSON(w, 200, map[string]any{"users": us})
	case path == "/users" && m == "POST":
		u, _ := body["user"].(string)
		name, _ := body["name"].(string)
		out, err := s.Tenancy.UserAdd(u, name)
		if err != nil {
			sendJSON(w, 400, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 201, out)
	case strings.HasPrefix(path, "/users/") && m == "DELETE":
		u := strings.TrimPrefix(path, "/users/")
		if err := s.Tenancy.UserDelete(u); err != nil {
			sendJSON(w, 404, map[string]any{"error": "no such user"})
			return
		}
		sendJSON(w, 200, map[string]any{"deleted": u})
	// ---- tenancy (docs/002 沙箱侧的领域 1): accounts are the admin's; projects, members and quotas are the account owner's ----
	case path == "/accounts" && m == "GET":
		if !id.Admin() {
			a, err := s.Tenancy.Account(id.Account)
			if err != nil {
				sendJSON(w, 404, map[string]any{"error": "no account"})
				return
			}
			sendJSON(w, 200, map[string]any{"accounts": []tenancy.Account{a}})
			return
		}
		as, _ := s.Tenancy.Accounts()
		sendJSON(w, 200, map[string]any{"accounts": as})
	case path == "/accounts" && m == "POST":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		aid, _ := body["id"].(string)
		name, _ := body["name"].(string)
		a, err := s.Tenancy.AccountAdd(aid, name)
		if err != nil {
			sendJSON(w, 400, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 201, a)
	case reWebhooks.MatchString(path) && s.Webhooks != nil:
		mm := reWebhooks.FindStringSubmatch(path)
		p, err := s.Tenancy.Project(mm[1])
		if err != nil || (!id.Admin() && (id.Account != p.Account || id.Project != "")) {
			sendJSON(w, 403, map[string]any{"error": "not your project"})
			return
		}
		if !id.Admin() && id.Role != tenancy.RoleOwner {
			sendJSON(w, 403, map[string]any{"error": "owner only"})
			return
		}
		switch {
		case mm[3] == "" && m == "GET":
			hs, err := s.Webhooks.List(p.ID)
			if err != nil {
				sendJSON(w, 500, map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 200, map[string]any{"webhooks": hs, "events": webhook.Events})
		case mm[3] == "" && m == "POST":
			u, _ := body["url"].(string)
			var evs []string
			if arr, ok := body["events"].([]any); ok {
				for _, e := range arr {
					if es, ok := e.(string); ok {
						evs = append(evs, es)
					}
				}
			}
			h, secret, err := s.Webhooks.Add(p.ID, u, evs, id.User)
			if err != nil {
				sendJSON(w, 400, map[string]any{"error": err.Error()})
				return
			}
			s.Audit.Write(map[string]any{"op": "webhook_create", "project": p.ID, "webhook": h.ID, "url": h.URL, "by": actor(id)})
			sendJSON(w, 201, map[string]any{"webhook": h, "secret": secret, "note": "the signing secret is shown once"})
		case mm[3] != "" && mm[5] == "" && m == "DELETE":
			if !s.Webhooks.Delete(p.ID, mm[3]) {
				sendJSON(w, 404, map[string]any{"error": "no such webhook"})
				return
			}
			sendJSON(w, 200, map[string]any{"deleted": mm[3]})
		case mm[5] == "test" && m == "POST":
			if !s.Webhooks.Ping(p.ID, mm[3]) {
				sendJSON(w, 404, map[string]any{"error": "no such webhook"})
				return
			}
			sendJSON(w, 202, map[string]any{"queued": "ping"})
		case mm[5] == "enable" && m == "POST":
			if !s.Webhooks.Enable(p.ID, mm[3]) {
				sendJSON(w, 404, map[string]any{"error": "no such webhook"})
				return
			}
			sendJSON(w, 200, map[string]any{"enabled": mm[3]})
		default:
			sendJSON(w, 405, map[string]any{"error": "method not allowed"})
		}
	case reKeys.MatchString(path):
		mm := reKeys.FindStringSubmatch(path)
		p, err := s.Tenancy.Project(mm[1])
		if err != nil || (!id.Admin() && (id.Account != p.Account || id.Project != "")) {
			sendJSON(w, 403, map[string]any{"error": "not your project"})
			return
		}
		if !id.Admin() && id.Role != tenancy.RoleOwner {
			sendJSON(w, 403, map[string]any{"error": "owner only"})
			return
		}
		switch {
		case mm[3] == "" && m == "GET":
			ks, err := s.Tenancy.Keys(p.ID)
			if err != nil {
				sendJSON(w, 500, map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 200, map[string]any{"keys": ks})
		case mm[3] == "" && m == "POST":
			name, _ := body["name"].(string)
			role, _ := body["role"].(string)
			ttl, _ := body["ttl_s"].(float64)
			res, err := s.Tenancy.KeyAdd(p.ID, name, role, ttl, id.User)
			if err != nil {
				sendJSON(w, 400, map[string]any{"error": err.Error()})
				return
			}
			s.Audit.Write(map[string]any{"op": "key_create", "project": p.ID, "name": name, "role": res["key"].(tenancy.Key).Role, "by": actor(id)})
			sendJSON(w, 201, res)
		case mm[3] != "" && m == "DELETE":
			if err := s.Tenancy.KeyDel(p.ID, mm[3]); err != nil {
				code := 500
				if errors.Is(err, tenancy.ErrNotFound) {
					code = 404
				}
				sendJSON(w, code, map[string]any{"error": err.Error()})
				return
			}
			s.Audit.Write(map[string]any{"op": "key_revoke", "project": p.ID, "key": mm[3], "by": actor(id)})
			sendJSON(w, 200, map[string]any{"revoked": mm[3]})
		default:
			sendJSON(w, 405, map[string]any{"error": "method not allowed"})
		}
	case reBilling.MatchString(path):
		mm := reBilling.FindStringSubmatch(path)
		acct, what := mm[1], mm[2]
		if _, err := s.Tenancy.Account(acct); err != nil {
			sendJSON(w, 404, map[string]any{"error": "no such account"})
			return
		}
		if what == "balance" && m == "GET" {
			if !id.Admin() && !(id.Account == acct && id.Role == tenancy.RoleOwner) {
				sendJSON(w, 403, map[string]any{"error": "owner only"})
				return
			}
			c, sp, err := s.Metering.Balance(acct)
			if err != nil {
				sendJSON(w, 503, map[string]any{"error": "metering store unavailable"})
				return
			}
			led, _ := s.Metering.Credits(acct, 20)
			sendJSON(w, 200, map[string]any{"account": acct, "mode": s.Tenancy.BillingMode(acct), "credits_usd": c, "spent_usd": sp, "balance_usd": c - sp, "entries": led})
			return
		}
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		switch {
		case what == "credit" && m == "POST":
			usd, _ := body["usd"].(float64)
			note, _ := body["note"].(string)
			if usd == 0 {
				sendJSON(w, 400, map[string]any{"error": "usd: a non-zero amount (negative = adjustment or refund)"})
				return
			}
			e, err := s.Metering.Credit(acct, usd, note, id.User)
			if err != nil {
				sendJSON(w, 500, map[string]any{"error": err.Error()})
				return
			}
			s.Audit.Write(map[string]any{"op": "credit", "account": acct, "usd": usd, "note": note, "by": actor(id)})
			s.creditPass() // a top-up lifts the out-of-credit suspension now, not a minute later
			sendJSON(w, 201, map[string]any{"entry": e, "balance_usd": s.Metering.BalanceUSD(acct)})
		case what == "billing" && m == "PUT":
			mode, _ := body["mode"].(string)
			if err := s.Tenancy.SetBillingMode(acct, mode); err != nil {
				sendJSON(w, 400, map[string]any{"error": err.Error()})
				return
			}
			s.Audit.Write(map[string]any{"op": "billing_mode", "account": acct, "mode": mode, "by": actor(id)})
			sendJSON(w, 200, map[string]any{"account": acct, "mode": mode})
		default:
			sendJSON(w, 405, map[string]any{"error": "method not allowed"})
		}
	case reAccount.MatchString(path) && m == "DELETE": // D68: offboarding
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		acct := reAccount.FindStringSubmatch(path)[1]
		if _, err := s.Tenancy.Account(acct); err != nil {
			sendJSON(w, 404, map[string]any{"error": "no such account"})
			return
		}
		set := map[string]bool{}
		if ps, err := s.Tenancy.Projects(acct); err == nil {
			for _, p := range ps {
				set[p.ID] = true
			}
		}
		sbs := s.Life.List(set)
		vols, _ := s.Volumes.List(set)
		force := r.URL.Query().Get("force") == "1"
		if (len(sbs) > 0 || len(vols) > 0) && !force {
			sendJSON(w, 409, map[string]any{"error": "the account still has sandboxes or volumes; delete them, or repeat with ?force=1", "sandboxes": len(sbs), "volumes": len(vols)})
			return
		}
		killed, dropped := 0, 0
		for _, sb := range sbs {
			if sid, _ := sb["id"].(string); sid != "" {
				if c, _ := s.Life.NodeCall(sid, "DELETE", "", nil); c == 200 {
					killed++
				}
			}
		}
		for _, v := range vols {
			if c, _ := s.Volumes.Delete(v.ID); c == 200 {
				dropped++
			}
		}
		out, err := s.Tenancy.AccountDelete(acct)
		if err != nil {
			sendJSON(w, 400, map[string]any{"error": err.Error()})
			return
		}
		s.Audit.Write(map[string]any{"op": "account_delete", "account": acct, "sandboxes_deleted": killed, "volumes_deleted": dropped, "removed": out, "by": actor(id)})
		sendJSON(w, 200, map[string]any{"account": acct, "deleted": true, "sandboxes_deleted": killed, "volumes_deleted": dropped, "removed": out, "kept": "usage events, credit ledger, statements, audit"})
	case reSuspend.MatchString(path) && m == "POST":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		mm := reSuspend.FindStringSubmatch(path)
		s.suspend(w, mm[1], mm[2] == "suspend", body, id)
	case path == "/invites" && (m == "GET" || m == "POST"):
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		if m == "GET" {
			iv, err := s.Tenancy.Invites()
			if err != nil {
				sendJSON(w, 500, map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 200, map[string]any{"invites": iv})
			return
		}
		in := tenancy.Invite{}
		in.Note, _ = body["note"].(string)
		in.CreditUSD, _ = body["credit_usd"].(float64)
		mx, _ := body["max_sandboxes"].(float64)
		in.MaxSandboxes = int64(mx)
		in.DailyUSD, _ = body["daily_usd"].(float64)
		ttl, _ := body["ttl_s"].(float64)
		res, err := s.Tenancy.InviteAdd(in, ttl, id.User)
		if err != nil {
			sendJSON(w, 400, map[string]any{"error": err.Error()})
			return
		}
		s.Audit.Write(map[string]any{"op": "invite_create", "invite": res["invite"].(tenancy.Invite).ID, "note": in.Note, "credit_usd": in.CreditUSD, "by": actor(id)})
		sendJSON(w, 201, res)
	case path == "/abuse/signals" && m == "GET":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		sigs := []abuse.Signal{}
		if s.Abuse != nil {
			sigs = s.Abuse.Signals()
		}
		sendJSON(w, 200, map[string]any{"enabled": s.Abuse != nil, "signals": sigs})
	case path == "/suspensions" && m == "GET":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		sendJSON(w, 200, map[string]any{"suspensions": s.Tenancy.Suspensions()})
	case reProjects.MatchString(path):
		acct := reProjects.FindStringSubmatch(path)[1]
		if !id.Admin() && id.Account != acct {
			sendJSON(w, 403, map[string]any{"error": "not your account"})
			return
		}
		if m == "GET" {
			ps, err := s.Tenancy.Projects(acct)
			if err != nil {
				sendJSON(w, 500, map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 200, map[string]any{"projects": ps})
			return
		}
		if m == "POST" {
			if !id.Admin() && id.Role != tenancy.RoleOwner {
				sendJSON(w, 403, map[string]any{"error": "owner only"})
				return
			}
			pid, _ := body["id"].(string)
			name, _ := body["name"].(string)
			p, err := s.Tenancy.ProjectAdd(acct, pid, name)
			if err != nil {
				sendJSON(w, codeOf(err, 400), map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 201, p)
			return
		}
		sendJSON(w, 404, map[string]any{"error": "no route"})
	case reRotate.MatchString(path) && m == "POST":
		mm := reRotate.FindStringSubmatch(path)
		p, err := s.Tenancy.Project(mm[1])
		if err != nil {
			sendJSON(w, 404, map[string]any{"error": "no project"})
			return
		}
		self := id.Account == p.Account && id.User == mm[2]
		if !id.Admin() && !(id.Account == p.Account && id.Role == tenancy.RoleOwner) && !self {
			sendJSON(w, 403, map[string]any{"error": "the owner or the member themselves"})
			return
		}
		ttl, _ := body["ttl_s"].(float64)
		if mm[3] == "expiry" {
			if err := s.Tenancy.SetExpiry(p.Account, mm[2], ttl); err != nil {
				sendJSON(w, 404, map[string]any{"error": "no such member"})
				return
			}
			s.Audit.Write(map[string]any{"op": "member_expiry", "account": p.Account, "user": mm[2], "ttl_s": ttl, "by": actor(id)})
			sendJSON(w, 200, map[string]any{"user": mm[2], "ttl_s": ttl})
			return
		}
		out, err := s.Tenancy.Rotate(p.Account, mm[2], ttl)
		if err != nil {
			sendJSON(w, codeOf(err, 400), map[string]any{"error": err.Error()})
			return
		}
		s.Audit.Write(map[string]any{"op": "member_rotate", "account": p.Account, "user": mm[2], "by": actor(id), "ttl_s": ttl})
		sendJSON(w, 200, out)
	case reMembers.MatchString(path):
		mm := reMembers.FindStringSubmatch(path)
		p, err := s.Tenancy.Project(mm[1])
		if err != nil {
			sendJSON(w, 404, map[string]any{"error": "no project"})
			return
		}
		if !id.Admin() && (id.Account != p.Account || (m != "GET" && id.Role != tenancy.RoleOwner)) {
			sendJSON(w, 403, map[string]any{"error": "owner only"})
			return
		}
		switch {
		case m == "GET" && mm[3] == "":
			ms, _ := s.Tenancy.Members(p.Account)
			sendJSON(w, 200, map[string]any{"members": ms})
		case m == "POST" && mm[3] == "":
			u, _ := body["user"].(string)
			name, _ := body["name"].(string)
			role, _ := body["role"].(string)
			out, err := s.Tenancy.MemberAdd(p.Account, u, name, role)
			if err != nil {
				sendJSON(w, codeOf(err, 400), map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 201, out)
		case m == "DELETE" && mm[3] != "":
			if err := s.Tenancy.MemberDelete(p.Account, mm[3]); err != nil {
				sendJSON(w, 404, map[string]any{"error": "no such member"})
				return
			}
			sendJSON(w, 200, map[string]any{"deleted": mm[3]})
		default:
			sendJSON(w, 404, map[string]any{"error": "no route"})
		}
	case reNetDef.MatchString(path):
		pid := reNetDef.FindStringSubmatch(path)[1]
		if !s.canSee(id, pid) {
			sendJSON(w, 403, map[string]any{"error": "not your project"})
			return
		}
		if m == "GET" {
			net, err := s.Tenancy.NetworkDefault(pid)
			if err != nil {
				sendJSON(w, codeOf(err, 500), map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 200, map[string]any{"project": pid, "network": net})
			return
		}
		if m == "PUT" || m == "DELETE" {
			if !id.Admin() && id.Role != tenancy.RoleOwner {
				sendJSON(w, 403, map[string]any{"error": "owner only"})
				return
			}
			var net map[string]any
			if m == "PUT" {
				net, _ = body["network"].(map[string]any)
				if net == nil {
					net = body
				}
				if err := network.Validate(net); err != nil {
					sendJSON(w, 400, map[string]any{"error": err.Error()})
					return
				}
			}
			if err := s.Tenancy.NetworkDefaultSet(pid, net); err != nil {
				sendJSON(w, codeOf(err, 400), map[string]any{"error": err.Error()})
				return
			}
			s.Audit.Write(map[string]any{"op": "project_network", "project": pid, "network": net, "by": actor(id)})
			sendJSON(w, 200, map[string]any{"project": pid, "network": net})
			return
		}
		sendJSON(w, 404, map[string]any{"error": "no route"})
	case reLayerEvw.MatchString(path) && m == "POST":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		res := s.Images.LayerEverywhere(reLayerEvw.FindStringSubmatch(path)[1])
		sendJSON(w, res.Code, res.Body)
	case reQuota.MatchString(path):
		pid := reQuota.FindStringSubmatch(path)[1]
		if !s.canSee(id, pid) {
			sendJSON(w, 403, map[string]any{"error": "not your project"})
			return
		}
		if m == "GET" {
			q, err := s.Tenancy.Quota(pid)
			if err != nil {
				sendJSON(w, 500, map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 200, q)
			return
		}
		if m == "PUT" {
			if !id.Admin() && id.Role != tenancy.RoleOwner {
				sendJSON(w, 403, map[string]any{"error": "owner only"})
				return
			}
			var q tenancy.Quota
			_ = json.Unmarshal(raw, &q)
			out, err := s.Tenancy.QuotaSet(pid, q)
			if err != nil {
				sendJSON(w, codeOf(err, 400), map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 200, out)
			return
		}
		sendJSON(w, 404, map[string]any{"error": "no route"})
	// ---- metering (docs/002 沙箱侧的领域 7) ----
	case path == "/metering/prices" && m == "GET":
		p, err := s.Metering.Prices()
		if err != nil {
			sendJSON(w, 500, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 200, p)
	case path == "/metering/prices" && m == "PUT":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		var per map[string]float64
		if json.Unmarshal(raw, &per) != nil || len(per) == 0 {
			sendJSON(w, 400, map[string]any{"error": "a {kind: usd_per_unit} object"})
			return
		}
		p, err := s.Metering.PricesSet(per)
		if err != nil {
			sendJSON(w, 400, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 200, p)
	case path == "/metering/tick" && m == "POST":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		n, err := s.Metering.Tick()
		if err != nil {
			sendJSON(w, 500, map[string]any{"error": err.Error(), "events": n})
			return
		}
		sendJSON(w, 200, map[string]any{"events": n})
	case reStatement.MatchString(path) && (m == "GET" || m == "POST"):
		mm := reStatement.FindStringSubmatch(path)
		if !s.canSee(id, mm[1]) {
			sendJSON(w, 403, map[string]any{"error": "not your project"})
			return
		}
		if mm[2] == "today" {
			sendJSON(w, 200, map[string]any{"project": mm[1], "spent_usd": s.Metering.SpentToday(mm[1])})
			return
		}
		if mm[2] == "breakdown" { // D66: where the money went, by sandbox or by day
			from, _ := strconv.ParseFloat(r.URL.Query().Get("from"), 64)
			to, _ := strconv.ParseFloat(r.URL.Query().Get("to"), 64)
			by := r.URL.Query().Get("by")
			if by == "" {
				by = "sandbox"
			}
			rows, err := s.Metering.Breakdown(mm[1], by, from, to, 50)
			if err != nil {
				sendJSON(w, 400, map[string]any{"error": err.Error()})
				return
			}
			total := 0.0
			for _, r := range rows {
				total += r.CostUSD
			}
			sendJSON(w, 200, map[string]any{"project": mm[1], "by": by, "rows": rows, "total_usd": total})
			return
		}
		if mm[2] == "statements" { // the settled ledger: GET lists, POST {from, to} freezes a period (owner)
			if m == "POST" {
				if !id.Admin() && id.Role != tenancy.RoleOwner {
					sendJSON(w, 403, map[string]any{"error": "owner only"})
					return
				}
				from, _ := body["from"].(float64)
				to, _ := body["to"].(float64)
				st, err := s.Metering.Settle(mm[1], from, to)
				if err != nil {
					sendJSON(w, 400, map[string]any{"error": err.Error()})
					return
				}
				sendJSON(w, 201, st)
				return
			}
			sts, err := s.Metering.Statements(mm[1])
			if err != nil {
				sendJSON(w, 500, map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 200, map[string]any{"project": mm[1], "statements": sts})
			return
		}
		from, _ := strconv.ParseFloat(r.URL.Query().Get("from"), 64)
		to, _ := strconv.ParseFloat(r.URL.Query().Get("to"), 64)
		st, err := s.Metering.StatementFor(mm[1], from, to)
		if err != nil {
			sendJSON(w, 500, map[string]any{"error": err.Error()})
			return
		}
		if r.URL.Query().Get("format") == "csv" {
			w.Header().Set("Content-Type", "text/csv; charset=utf-8")
			w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%s-%.0f-%.0f.csv", mm[1], st.From, st.To))
			w.WriteHeader(200)
			io.WriteString(w, metering.CSV(st))
			return
		}
		sendJSON(w, 200, st)
	case reSbUsage.MatchString(path) && m == "GET":
		sid := reSbUsage.FindStringSubmatch(path)[1]
		if p := s.Life.ProjectOf(sid); p != "" && !s.canSee(id, p) {
			sendJSON(w, 403, map[string]any{"error": "not your sandbox"})
			return
		}
		lines, err := s.Metering.SandboxUsage(sid)
		if err != nil {
			sendJSON(w, 500, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 200, map[string]any{"sandbox": sid, "usage": lines})
	case path == "/nodes" && m == "GET":
		sendJSON(w, 200, map[string]any{"nodes": s.Fleet.All()})
	case path == "/nodes" && m == "POST":
		var spec fleet.Spec
		_ = json.Unmarshal(raw, &spec)
		if spec.Name == "" || spec.URL == "" {
			sendJSON(w, 400, map[string]any{"error": "name and url required"})
			return
		}
		s.Fleet.Add(spec, true)
		sendJSON(w, 201, map[string]any{"added": spec.Name})
	case reNodeOp.MatchString(path) && m == "POST":
		mm := reNodeOp.FindStringSubmatch(path)
		switch mm[2] {
		case "drain":
			res := s.Life.Drain(mm[1])
			sendJSON(w, res.Code, res.Body)
		default:
			if err := s.Fleet.Cordon(mm[1], mm[2] == "cordon"); err != nil {
				sendJSON(w, 404, map[string]any{"error": err.Error()})
				return
			}
			sendJSON(w, 200, map[string]any{"node": mm[1], "cordoned": mm[2] == "cordon"})
		}
	case path == "/fleet/balance" && m == "POST":
		max, _ := body["max"].(float64)
		gap, _ := body["gap"].(float64)
		res := s.Life.Balance(int(max), gap)
		sendJSON(w, res.Code, res.Body)
	case reEvacuate.MatchString(path) && m == "POST":
		res := s.Life.Evacuate(reEvacuate.FindStringSubmatch(path)[1])
		sendJSON(w, res.Code, res.Body)
	// ---- images (docs/002 沙箱侧 4): builds, templates, layers ----
	case path == "/templates" && m == "GET":
		sendJSON(w, 200, map[string]any{"templates": s.Images.Templates()})
	case path == "/layers" && m == "GET":
		sendJSON(w, 200, map[string]any{"layers": s.Images.Layers()})
	case path == "/builds" && m == "GET":
		bs, err := s.Builds.List(s.accountScope(id))
		if err != nil {
			sendJSON(w, 500, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 200, map[string]any{"builds": bs})
	case path == "/builds/archive" && m == "POST":
		project, bad := s.projectFor(r, body, id)
		if bad != nil {
			sendJSON(w, *bad, badProject(*bad))
			return
		}
		node := r.URL.Query().Get("node")
		if node != "" && !id.Admin() {
			node = ""
		}
		bl, code, err := s.Builds.StartArchive(project, r.URL.Query().Get("name"), node, raw)
		if err != nil {
			sendJSON(w, code, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 201, bl)
	case path == "/builds" && m == "POST":
		project, bad := s.projectFor(r, body, id)
		if bad != nil {
			sendJSON(w, *bad, badProject(*bad))
			return
		}
		kind, _ := body["kind"].(string)
		if kind == "commit" {
			sid, _ := body["sandbox"].(string)
			if p := s.Life.ProjectOf(sid); p == "" || !s.canSee(id, p) {
				sendJSON(w, 404, map[string]any{"error": "no sandbox " + sid})
				return
			}
		}
		if node, _ := body["node"].(string); node != "" && !id.Admin() {
			delete(body, "node")
		}
		input := map[string]any{}
		for k, v := range body {
			if k != "kind" && k != "project" {
				input[k] = v
			}
		}
		bl, code, err := s.Builds.Start(project, kind, input)
		if err != nil {
			sendJSON(w, code, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 201, bl)
	case reBuildLog.MatchString(path) && m == "GET":
		bl, err := s.Builds.Get(reBuildLog.FindStringSubmatch(path)[1])
		if err != nil || !s.canSee(id, bl.Project) {
			sendJSON(w, 404, map[string]any{"error": "no build"})
			return
		}
		since, _ := strconv.Atoi(r.URL.Query().Get("since"))
		code, out := s.Builds.Log(bl, since)
		sendJSON(w, code, out)
	case reBuild.MatchString(path) && m == "GET":
		bl, err := s.Builds.Get(reBuild.FindStringSubmatch(path)[1])
		if err != nil || !s.canSee(id, bl.Project) {
			sendJSON(w, 404, map[string]any{"error": "no build"})
			return
		}
		sendJSON(w, 200, bl)
	case path == "/templates/everywhere" && m == "POST":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		res := s.Images.Everywhere(body)
		sendJSON(w, res.Code, res.Body)
	case reTemplate.MatchString(path) && m == "DELETE": // a named template is global: the deployer may remove any, a member the ones their projects committed
		name := reTemplate.FindStringSubmatch(path)[1]
		if !id.Admin() && !s.Builds.CommittedBy(s.accountScope(id), name) {
			sendJSON(w, 403, map[string]any{"error": "not your template"})
			return
		}
		res := s.Images.DeleteTemplate(name)
		sendJSON(w, res.Code, res.Body)
	case path == "/routes" && m == "GET":
		sendJSON(w, 200, map[string]any{"routes": s.Life.Routes()})
	case path == "/store/gc" && m == "POST":
		sendJSON(w, 200, s.Store.GC())
	case path == "/store/bundles" && m == "GET":
		sendJSON(w, 200, map[string]any{"bundles": s.Store.Bundles()})
	case path == "/sandboxes" && m == "POST":
		project, bad := s.projectFor(r, body, id)
		if bad != nil {
			sendJSON(w, *bad, badProject(*bad))
			return
		}
		if net, _ := body["network"].(map[string]any); net != nil { // the policy's shape is checked here, at the edge, and again on the node
			if err := network.Validate(net); err != nil {
				sendJSON(w, 400, map[string]any{"error": err.Error()})
				return
			}
		}
		if ids := volumeIDs(body); len(ids) > 0 { // the sandbox is placed where its volumes live, and they must be its project's
			node, projects, err := s.Volumes.NodeFor(ids)
			if err != nil {
				sendJSON(w, 404, map[string]any{"error": err.Error()})
				return
			}
			for p := range projects {
				if p != project {
					sendJSON(w, 403, map[string]any{"error": "volume belongs to project " + p})
					return
				}
			}
			if want, _ := body["node"].(string); want != "" && want != node {
				sendJSON(w, 409, map[string]any{"error": "volumes live on node " + node})
				return
			}
			body["node"] = node
		}
		res := s.Life.Create(body, project)
		sendJSON(w, res.Code, res.Body)
	// ---- volumes (docs/002 沙箱侧 5) ----
	case path == "/volumes" && m == "GET":
		vs, err := s.Volumes.List(s.accountScope(id))
		if err != nil {
			sendJSON(w, 500, map[string]any{"error": err.Error()})
			return
		}
		out := []map[string]any{}
		for _, x := range vs {
			out = append(out, s.Volumes.Info(x))
		}
		sendJSON(w, 200, map[string]any{"volumes": out})
	case path == "/volumes/from-archive" && m == "POST":
		project, bad := s.projectFor(r, body, id)
		if bad != nil {
			sendJSON(w, *bad, badProject(*bad))
			return
		}
		node := r.URL.Query().Get("node")
		if node != "" && !id.Admin() {
			node = ""
		}
		size, _ := strconv.ParseInt(r.URL.Query().Get("size_mib"), 10, 64)
		x, code, err := s.Volumes.CreateFrom(project, r.URL.Query().Get("name"), size, node, raw)
		if err != nil {
			sendJSON(w, code, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 201, s.Volumes.Info(x))
	case path == "/volumes" && m == "POST":
		project, bad := s.projectFor(r, body, id)
		if bad != nil {
			sendJSON(w, *bad, badProject(*bad))
			return
		}
		name, _ := body["name"].(string)
		size, _ := body["size_mib"].(float64)
		node, _ := body["node"].(string)
		if node != "" && !id.Admin() {
			node = ""
		}
		x, code, err := s.Volumes.Create(project, name, int64(size), node)
		if err != nil {
			sendJSON(w, code, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 201, s.Volumes.Info(x))
	case reVolume.MatchString(path):
		vid := reVolume.FindStringSubmatch(path)[1]
		x, err := s.Volumes.Get(vid)
		if err != nil || !s.canSee(id, x.Project) {
			sendJSON(w, 404, map[string]any{"error": "no volume " + vid})
			return
		}
		switch m {
		case "GET":
			sendJSON(w, 200, s.Volumes.Info(x))
		case "DELETE":
			code, out := s.Volumes.Delete(vid)
			sendJSON(w, code, out)
		default:
			sendJSON(w, 404, map[string]any{"error": "no route"})
		}
	case path == "/sandboxes" && m == "GET":
		all := s.Life.List(s.accountScope(id))
		page, total := filterSandboxes(all, r.URL.Query())
		sendJSON(w, 200, map[string]any{"sandboxes": page, "total": total})
	case path == "/sandboxes/import" && m == "POST":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		bundle, _ := body["bundle"].(string)
		node, _ := body["node"].(string)
		project, code := s.projectFor(r, body, id)
		if code != nil {
			sendJSON(w, *code, map[string]any{"error": "no such project"})
			return
		}
		res := s.Life.Import(bundle, node, project)
		sendJSON(w, res.Code, res.Body)
	case reMigrate.MatchString(path) && m == "POST":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		to, _ := body["to"].(string)
		res := s.Life.Migrate(reMigrate.FindStringSubmatch(path)[1], to)
		sendJSON(w, res.Code, res.Body)
	case reSandbox.MatchString(path):
		mm := reSandbox.FindStringSubmatch(path)
		if p := s.Life.ProjectOf(mm[1]); p != "" && !s.canSee(id, p) {
			sendJSON(w, 404, map[string]any{"error": "no sandbox " + mm[1]}) // another account's sandbox does not exist for this one
			return
		}
		if mm[2] == "/network" && m == "PUT" {
			if err := network.Validate(body); err != nil {
				sendJSON(w, 400, map[string]any{"error": err.Error()})
				return
			}
			if !id.Admin() && id.Role != tenancy.RoleOwner && s.NetReq != nil { // a member may tighten at once; loosening waits for an owner
				st, info := s.Life.NodeCall(mm[1], "GET", "", nil)
				cur, _ := info["network"].(map[string]any)
				if st == 200 && network.Relaxes(cur, body) {
					req, err := s.NetReq.Add(mm[1], s.Life.ProjectOf(mm[1]), actor(id), body)
					if err != nil {
						sendJSON(w, 500, map[string]any{"error": err.Error()})
						return
					}
					s.Audit.Write(map[string]any{"op": "network_relax_requested", "sandbox": mm[1], "request": req.ID, "by": actor(id), "project": req.Project})
					sendJSON(w, 202, map[string]any{"request": req, "note": "loosening the policy needs an owner: POST /network/requests/<id>/approve"})
					return
				}
			}
		}
		s.sandboxCall(w, r, id, mm[1], mm[2], raw, body)
	case path == "/network/requests" && m == "GET":
		all := r.URL.Query().Get("all") != ""
		reqs, err := s.NetReq.List(s.accountScope(id), all)
		if err != nil {
			sendJSON(w, 500, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 200, map[string]any{"requests": reqs})
	case reNetReq.MatchString(path) && m == "POST":
		rm := reNetReq.FindStringSubmatch(path)
		req, err := s.NetReq.Get(rm[1])
		if err != nil {
			sendJSON(w, 404, map[string]any{"error": "no such request"})
			return
		}
		if !s.canSee(id, req.Project) || (!id.Admin() && id.Role != tenancy.RoleOwner) {
			sendJSON(w, 403, map[string]any{"error": "an owner of the project decides"})
			return
		}
		if rm[2] == "approve" {
			st, info := s.Life.NodeCall(req.Sandbox, "PUT", "/network", req.Body)
			if st != 200 {
				sendJSON(w, st, map[string]any{"error": "the node refused the policy", "detail": info})
				return
			}
		}
		out, err := s.NetReq.Decide(req.ID, actor(id), map[string]string{"approve": "approved", "deny": "denied"}[rm[2]])
		if err != nil {
			sendJSON(w, 409, map[string]any{"error": err.Error()})
			return
		}
		s.Audit.Write(map[string]any{"op": "network_relax_" + out.Status, "sandbox": req.Sandbox, "request": req.ID, "by": actor(id), "project": req.Project})
		sendJSON(w, 200, map[string]any{"request": out})
	// ---- the nodes' credential vaults (docs/002 沙箱侧 6): names here, values only on the nodes ----
	case path == "/network/credentials" && m == "GET":
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		sendJSON(w, 200, map[string]any{"nodes": s.Creds.Names()})
	case reCred.MatchString(path) && (m == "PUT" || m == "DELETE"):
		if !id.Admin() {
			sendJSON(w, 403, map[string]any{"error": "deployer only"})
			return
		}
		name := reCred.FindStringSubmatch(path)[1]
		if m == "DELETE" {
			sendJSON(w, 200, s.Creds.Drop(name))
			return
		}
		header, _ := body["header"].(string)
		value, _ := body["value"].(string)
		out, err := s.Creds.Set(name, header, value)
		if err != nil {
			sendJSON(w, 400, map[string]any{"error": err.Error()})
			return
		}
		sendJSON(w, 200, out)
	default:
		sendJSON(w, 404, map[string]any{"error": "no route"})
	}
}

// forward: /eval/* and /intake/* go to the service behind with the control plane's bearer and the user's name.
func (s *Server) forward(w http.ResponseWriter, r *http.Request, base, what, path string, raw []byte, user string, passthrough []string) {
	if base == "" {
		sendJSON(w, 503, map[string]any{"error": fmt.Sprintf("no %s configured (%s_URL)", what, strings.ToUpper(what))})
		return
	}
	req, err := http.NewRequest(r.Method, base+path, strings.NewReader(string(raw)))
	if err != nil {
		sendJSON(w, 502, map[string]any{"error": err.Error()})
		return
	}
	ct := r.Header.Get("Content-Type")
	if ct == "" {
		ct = "application/json"
	}
	req.Header.Set("Content-Type", ct)
	if t := s.Tenancy.Token(); t != "" {
		req.Header.Set("Authorization", "Bearer "+t) // the services accept the control plane's bearer (docs/009)
	}
	if user == "" {
		user = tenancy.Admin
	}
	req.Header.Set("X-User", user) // whose Agent this request is about; trusted because it comes with the bearer
	for _, k := range passthrough {
		if v := r.Header.Get(k); v != "" {
			req.Header.Set(k, v)
		}
	}
	resp, err := (&http.Client{Timeout: 120 * time.Second}).Do(req)
	if err != nil {
		sendJSON(w, 502, map[string]any{"error": what + " unreachable: " + err.Error()})
		return
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	ct = resp.Header.Get("Content-Type")
	if ct == "" {
		ct = "application/json"
	}
	w.Header().Set("Content-Type", ct)
	w.Header().Set("Content-Length", fmt.Sprint(len(data)))
	w.WriteHeader(resp.StatusCode)
	w.Write(data)
}

var mime = map[string]string{".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
	".txt": "text/plain; charset=utf-8", ".woff2": "font/woff2", ".woff": "font/woff", ".ico": "image/x-icon", ".map": "application/json", ".webp": "image/webp"}

func (s *Server) static(w http.ResponseWriter, path string) {
	rel := ""
	if strings.HasPrefix(path, "/ui") {
		rel = strings.TrimLeft(path[3:], "/")
	}
	if rel == "" {
		rel = "index.html"
	}
	root, _ := filepath.Abs(s.WebDir)
	full, _ := filepath.Abs(filepath.Join(root, rel))
	if !strings.HasPrefix(full, root+string(os.PathSeparator)) {
		sendJSON(w, 404, map[string]any{"error": "no such file"})
		return
	}
	data, err := os.ReadFile(full)
	if err != nil {
		sendJSON(w, 404, map[string]any{"error": "no such file"})
		return
	}
	ct := mime[filepath.Ext(full)]
	if ct == "" {
		ct = "application/octet-stream"
	}
	w.Header().Set("Content-Type", ct)
	w.Header().Set("Content-Length", fmt.Sprint(len(data)))
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(200)
	w.Write(data)
}

// eventsStream: one JSON line per change the control plane notices (nodes, sandboxes, intake, eval); clients that get these stop polling.
func (s *Server) eventsStream(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		sendJSON(w, 500, map[string]any{"error": "no streaming"})
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(200)
	w.Write([]byte("event: hello\ndata: {}\n\n"))
	flusher.Flush()
	type probe struct{ kind, path, url string }
	probes := []probe{{"node", "nodes", ""}, {"sandbox", "routes", ""}}
	if s.IntakeURL != "" {
		probes = append(probes, probe{"ticket", "/attention", s.IntakeURL})
	}
	if s.EvalURL != "" {
		probes = append(probes, probe{"batch", "/batches", s.EvalURL})
	}
	last := map[string]string{}
	for {
		for _, p := range probes {
			d := s.digest(p.path, p.url)
			if d != "" && last[p.kind] != d {
				if _, seen := last[p.kind]; seen {
					b, _ := json.Marshal(map[string]any{"type": p.kind, "at": float64(time.Now().UnixNano()) / 1e9})
					if _, err := w.Write([]byte("data: " + string(b) + "\n\n")); err != nil {
						return
					}
					flusher.Flush()
				}
				last[p.kind] = d
			}
		}
		if _, err := w.Write([]byte(": keepalive\n\n")); err != nil {
			return
		}
		flusher.Flush()
		select {
		case <-r.Context().Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
}

func (s *Server) digest(path, url string) string {
	var data []byte
	switch {
	case url != "":
		resp, err := (&http.Client{Timeout: 5 * time.Second}).Get(url + path)
		if err != nil {
			return ""
		}
		data, _ = io.ReadAll(resp.Body)
		resp.Body.Close()
		var j map[string]any
		if json.Unmarshal(data, &j) == nil { // drop the fields that change on every poll (cursors, 24 h counters) so only real changes fan out
			delete(j, "sources")
			delete(j, "counts")
			data, _ = json.Marshal(j)
		}
	case path == "routes": // sandboxes: routes plus each node's counts (pause/resume/fail change the counts, not the routes)
		counts := map[string]any{}
		for _, n := range s.Fleet.All() {
			counts[n.Name] = []any{n.Num("vms_total"), n.Num("vms_running"), n.Num("vms_pooled")}
		}
		data, _ = json.Marshal([]any{s.Life.Routes(), counts})
	default:
		st := map[string]any{}
		for _, n := range s.Fleet.All() {
			st[n.Name] = []any{n.Alive(), n.LastErr(), n.Num("vms_running")}
		}
		data, _ = json.Marshal(st)
	}
	h := sha1.Sum(data)
	return hex.EncodeToString(h[:])
}

// volumeIDs: the ids in a create body's `volumes` (strings or {id, mount, ro} objects).
func volumeIDs(body map[string]any) []string {
	out := []string{}
	arr, _ := body["volumes"].([]any)
	for _, x := range arr {
		switch v := x.(type) {
		case string:
			out = append(out, v)
		case map[string]any:
			if id, _ := v["id"].(string); id != "" {
				out = append(out, id)
			}
		}
	}
	return out
}

func codeOf(err error, dflt int) int {
	switch err {
	case tenancy.ErrNotFound:
		return 404
	case tenancy.ErrForbidden:
		return 403
	}
	return dflt
}

func orSlash(p string) string {
	if p == "" {
		return "/"
	}
	return p
}

// ---- a token bucket per key (013: rate limit per member token) ----
type limiter struct {
	mu  sync.Mutex
	b   map[string]*bucket
	now func() time.Time
}
type bucket struct {
	tokens float64
	last   time.Time
}

func (s *Server) limit() *limiter {
	if s.limiter == nil {
		s.limiter = &limiter{b: map[string]*bucket{}, now: time.Now}
	}
	return s.limiter
}

func (l *limiter) allow(key string, perMin float64) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	t := l.now()
	if len(l.b) > 20000 { // keys can be attacker-chosen (unauthenticated signup): never let the table grow without bound
		for k, v := range l.b {
			if t.Sub(v.last) > 10*time.Minute {
				delete(l.b, k)
			}
		}
	}
	b := l.b[key]
	if b == nil {
		b = &bucket{tokens: perMin, last: t}
		l.b[key] = b
	}
	b.tokens += t.Sub(b.last).Minutes() * perMin
	if b.tokens > perMin {
		b.tokens = perMin
	}
	b.last = t
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// metrics is the Prometheus text exposition of what an operator watches: nodes, sandboxes by class / priority / node / project, pending egress requests.
func (s *Server) metrics(w http.ResponseWriter) {
	var b strings.Builder
	alive, total := 0, 0
	for _, n := range s.Fleet.All() {
		total++
		up := 0
		if n.Alive() {
			alive++
			up = 1
		}
		h := n.Health()
		fmt.Fprintf(&b, "cairn_node_up{node=%q} %d\n", n.Name, up)
		fmt.Fprintf(&b, "cairn_node_vms_total{node=%q} %g\n", n.Name, fleet.NumOf(h, "vms_total"))
		fmt.Fprintf(&b, "cairn_node_vms_running{node=%q} %g\n", n.Name, fleet.NumOf(h, "vms_running"))
		fmt.Fprintf(&b, "cairn_node_mem_mib_running{node=%q} %g\n", n.Name, fleet.NumOf(h, "mem_mib_running"))
		fmt.Fprintf(&b, "cairn_node_mem_budget_mib{node=%q} %g\n", n.Name, fleet.NumOf(h, "mem_budget_mib"))
		fmt.Fprintf(&b, "cairn_node_load{node=%q} %.3f\n", n.Name, fleet.Load(n))
	}
	fmt.Fprintf(&b, "cairn_nodes_alive %d\ncairn_nodes_total %d\n", alive, total)
	b.WriteString(e2b.MetricsText()) // the E2B envd side (port 8710) counts in this process; its lines are part of the same scrape
	fmt.Fprintf(&b, "cairn_accounts_suspended %d\n", len(s.Tenancy.Suspensions()))
	for _, k := range []string{"cpu", "egress"} {
		n := 0
		if s.Abuse != nil {
			n = s.Abuse.Total(k)
		}
		fmt.Fprintf(&b, "cairn_abuse_signals_total{kind=%q} %d\n", k, n)
	}
	// dependencies: the registry, the SQL database, the chunk store; 1 = answered just now
	up := func(name string, ok bool) {
		v := 0
		if ok {
			v = 1
		}
		fmt.Fprintf(&b, "cairn_dependency_up{dep=%q} %d\n", name, v)
	}
	up("registry", s.Reg.Ping() == nil)
	var one int
	up("database", s.Metering.DB().QueryRow(`SELECT 1`).Scan(&one) == nil)
	if s.Store.Enabled() {
		up("store", s.storeUp.check(s.Store.URL))
	}
	var lastTick float64
	if s.Metering.DB().QueryRow(`SELECT v FROM meter_state WHERE k = 'last_tick'`).Scan(&lastTick) == nil && lastTick > 0 {
		fmt.Fprintf(&b, "cairn_metering_last_tick_age_seconds %.0f\n", float64(time.Now().Unix())-lastTick)
	}
	for _, k := range []string{"2xx", "3xx", "4xx", "5xx", "rate_limited"} {
		var n int64
		if v, ok := s.counts.Load(k); ok {
			n = v.(*atomic.Int64).Load()
		}
		if k == "rate_limited" {
			fmt.Fprintf(&b, "cairn_http_rate_limited_total %d\n", n)
		} else {
			fmt.Fprintf(&b, "cairn_http_responses_total{class=%q} %d\n", k, n)
		}
	}
	downSb := 0
	for _, r := range s.Life.Routes() {
		if n := s.Fleet.Get(r.Node); n == nil || !n.Alive() {
			downSb++
		}
	}
	fmt.Fprintf(&b, "cairnboxes_on_down_nodes %d\n", downSb)
	byKey := map[string]int{}
	for _, r := range s.Life.Routes() {
		cls, prio := r.Class, r.Priority
		if cls == "" {
			cls = "persistent"
		}
		if prio == "" {
			prio = "interactive"
		}
		byKey[fmt.Sprintf("class=%q,priority=%q,node=%q,project=%q", cls, prio, r.Node, r.Project)]++
	}
	for k, v := range byKey {
		fmt.Fprintf(&b, "cairnboxes{%s} %d\n", k, v)
	}
	if s.NetReq != nil {
		if reqs, err := s.NetReq.List(nil, false); err == nil {
			fmt.Fprintf(&b, "cairn_network_requests_pending %d\n", len(reqs))
		}
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	w.WriteHeader(200)
	io.WriteString(w, b.String())
}

// storeProbe asks the chunk store for its index at most once every 10 seconds (a scrape must not hammer it) and remembers the answer.
type storeProbe struct {
	mu   sync.Mutex
	at   time.Time
	last bool
}

func (p *storeProbe) check(url string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if time.Since(p.at) < 10*time.Second {
		return p.last
	}
	c := http.Client{Timeout: 2 * time.Second}
	resp, err := c.Get(strings.TrimRight(url, "/") + "/index.json")
	ok := err == nil && resp.StatusCode == 200
	if resp != nil {
		resp.Body.Close()
	}
	p.at, p.last = time.Now(), ok
	return ok
}

// console serves the Next.js static export under /console/. A directory serves its index.html (the export uses trailing slashes), a path without an extension
// tries <path>/index.html and <path>.html, and anything else is the export's own 404 page with a 404 status. Files under _next/static are content-hashed: cache them for a year.
func (s *Server) console(w http.ResponseWriter, r *http.Request) {
	if s.ConsoleDir == "" {
		sendJSON(w, 404, map[string]any{"error": "console not installed"})
		return
	}
	root, _ := filepath.Abs(s.ConsoleDir)
	rel := strings.TrimPrefix(strings.TrimPrefix(r.URL.Path, "/console"), "/")
	if r.URL.Path == "/console" {
		http.Redirect(w, r, "/console/", http.StatusMovedPermanently)
		return
	}
	try := []string{rel}
	switch {
	case rel == "" || strings.HasSuffix(rel, "/"):
		try = []string{rel + "index.html"}
	case filepath.Ext(rel) == "":
		try = []string{rel + "/index.html", rel + ".html"}
	}
	for _, c := range try {
		full, _ := filepath.Abs(filepath.Join(root, c))
		if !strings.HasPrefix(full, root+string(os.PathSeparator)) {
			break
		}
		if st, err := os.Stat(full); err != nil || st.IsDir() {
			continue
		}
		if filepath.Ext(rel) == "" && !strings.HasSuffix(rel, "/") && strings.HasSuffix(c, "/index.html") { // /console/overview -> /console/overview/ so relative links and the router agree
			http.Redirect(w, r, r.URL.Path+"/"+queryOf(r), http.StatusMovedPermanently)
			return
		}
		s.serveFile(w, full, strings.Contains(rel, "_next/static/"), 200)
		return
	}
	s.serveFile(w, filepath.Join(root, "404.html"), false, 404)
}

func queryOf(r *http.Request) string {
	if r.URL.RawQuery == "" {
		return ""
	}
	return "?" + r.URL.RawQuery
}

func (s *Server) serveFile(w http.ResponseWriter, full string, immutable bool, code int) {
	data, err := os.ReadFile(full)
	if err != nil {
		sendJSON(w, 404, map[string]any{"error": "no such file"})
		return
	}
	ct := mime[filepath.Ext(full)]
	if ct == "" {
		ct = "application/octet-stream"
	}
	w.Header().Set("Content-Type", ct)
	w.Header().Set("Content-Length", fmt.Sprint(len(data)))
	if immutable {
		w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
	} else {
		w.Header().Set("Cache-Control", "no-cache")
	}
	w.WriteHeader(code)
	w.Write(data)
}

// suspend: the deployer's abuse stop (D53). Suspending records the fact first (so nothing new starts), then pauses every running sandbox of the
// account's projects: state is kept for the investigation, compute and egress stop. Unsuspending only lifts the block; the owner resumes what they want.
func (s *Server) suspend(w http.ResponseWriter, account string, on bool, body map[string]any, id tenancy.Identity) {
	if !on {
		if err := s.Tenancy.Unsuspend(account); err != nil {
			code := 500
			if errors.Is(err, tenancy.ErrNotFound) {
				code = 404
			}
			sendJSON(w, code, map[string]any{"error": err.Error()})
			return
		}
		s.Audit.Write(map[string]any{"op": "account_unsuspend", "account": account, "by": actor(id)})
		sendJSON(w, 200, map[string]any{"account": account, "suspended": false})
		return
	}
	reason, _ := body["reason"].(string)
	if strings.TrimSpace(reason) == "" {
		sendJSON(w, 400, map[string]any{"error": "reason is required: the account's members see it"})
		return
	}
	if err := s.Tenancy.Suspend(account, reason, id.User); err != nil {
		code := 500
		if errors.Is(err, tenancy.ErrNotFound) {
			code = 404
		}
		sendJSON(w, code, map[string]any{"error": err.Error()})
		return
	}
	paused, failed := s.pauseAccount(account)
	s.Audit.Write(map[string]any{"op": "account_suspend", "account": account, "reason": reason, "paused": paused, "pause_failed": failed, "by": actor(id)})
	sendJSON(w, 200, map[string]any{"account": account, "suspended": true, "reason": reason, "paused": paused, "pause_failed": failed})
}

// pauseAccount pauses every running sandbox of the account's projects (state kept) and says which it could not.
func (s *Server) pauseAccount(account string) (paused, failed []string) {
	set := map[string]bool{}
	if ps, err := s.Tenancy.Projects(account); err == nil {
		for _, p := range ps {
			set[p.ID] = true
		}
	}
	paused, failed = []string{}, []string{}
	for _, sb := range s.Life.List(set) {
		sid, _ := sb["id"].(string)
		if st, _ := sb["status"].(string); sid == "" || st != "running" {
			continue
		}
		if code, _ := s.Life.NodeCall(sid, "POST", "/pause", nil); code == 200 {
			paused = append(paused, sid)
		} else {
			failed = append(failed, sid)
		}
	}
	return paused, failed
}

const outOfCredit = "out of credit"

// WatchCredit (D59): a prepaid account at zero is suspended the same way an abusive one is (sandboxes paused, state kept), with the reason members can read.
// A top-up lifts that suspension (and only that one: a deployer's suspension for another reason stays).
func (s *Server) WatchCredit(every time.Duration) {
	for {
		time.Sleep(every)
		s.creditPass()
	}
}

func (s *Server) creditPass() {
	if s.Metering == nil {
		return
	}
	for _, a := range s.Tenancy.PrepaidAccounts() {
		bal := s.Metering.BalanceUSD(a)
		sp, on := s.Tenancy.SuspensionOf(a)
		switch {
		case bal <= 0 && !on:
			if err := s.Tenancy.Suspend(a, outOfCredit, "billing"); err == nil {
				paused, failed := s.pauseAccount(a)
				s.Audit.Write(map[string]any{"op": "account_suspend", "account": a, "reason": outOfCredit, "paused": paused, "pause_failed": failed, "by": "billing", "balance_usd": bal})
			}
		case bal > 0 && on && sp.Reason == outOfCredit:
			if s.Tenancy.Unsuspend(a) == nil {
				s.Audit.Write(map[string]any{"op": "account_unsuspend", "account": a, "by": "billing", "balance_usd": bal})
			}
		}
	}
}

// signup: a new account from an invite code (D60). Public, rate limited per client address; every failure about the code looks the same (no probing).
func (s *Server) signup(w http.ResponseWriter, r *http.Request, raw []byte) {
	ip := r.RemoteAddr
	if h, _, err := net.SplitHostPort(ip); err == nil {
		ip = h
	}
	if os.Getenv("CP_TRUST_PROXY") == "1" { // behind a reverse proxy the peer is always the proxy; otherwise a client could pick its own address
		if xf := strings.TrimSpace(strings.Split(r.Header.Get("X-Forwarded-For"), ",")[0]); xf != "" {
			ip = xf
		}
	}
	if !s.limit().allow("signup:"+ip, 10) {
		w.Header().Set("Retry-After", "30")
		sendJSON(w, 429, map[string]any{"error": "too many attempts"})
		return
	}
	var b struct {
		Code    string `json:"code"`
		Account string `json:"account"`
		User    string `json:"user"`
		Name    string `json:"name"`
	}
	if json.Unmarshal(raw, &b) != nil || b.Code == "" || b.Account == "" || b.User == "" {
		sendJSON(w, 400, map[string]any{"error": "code, account and user are required"})
		return
	}
	inv, tok, err := s.Tenancy.Signup(strings.TrimSpace(b.Code), b.Account, b.User, b.Name)
	if err != nil {
		if errors.Is(err, tenancy.ErrInvite) {
			sendJSON(w, 403, map[string]any{"error": "invite code is not valid"})
			return
		}
		sendJSON(w, 400, map[string]any{"error": err.Error()}) // names: letters / digits / _ -, 2-32 chars, or already taken
		return
	}
	if inv.MaxSandboxes > 0 || inv.DailyUSD > 0 {
		_, _ = s.Tenancy.QuotaSet(b.Account, tenancy.Quota{MaxSandboxes: inv.MaxSandboxes, DailyUSD: inv.DailyUSD})
	}
	if inv.CreditUSD > 0 && s.Metering != nil {
		_ = s.Tenancy.SetBillingMode(b.Account, tenancy.Prepaid)
		_, _ = s.Metering.Credit(b.Account, inv.CreditUSD, "invite "+inv.ID, "signup")
	}
	s.Audit.Write(map[string]any{"op": "signup", "account": b.Account, "user": b.User, "invite": inv.ID, "credit_usd": inv.CreditUSD})
	sendJSON(w, 201, map[string]any{"account": b.Account, "project": b.Account, "user": b.User, "token": tok["token"], "credit_usd": inv.CreditUSD, "note": "the token is shown once"})
}

// status: what a customer's status page shows (D61). Component names are generic on purpose: nothing about nodes, hosts or errors leaves here.
// operational = everything answers and at least one node is alive; degraded = a dependency or some (not all) nodes are down; outage = no node or no registry.
func (s *Server) status(w http.ResponseWriter) {
	var one int
	comp := map[string]string{}
	set := func(name string, ok bool) {
		comp[name] = "operational"
		if !ok {
			comp[name] = "outage"
		}
	}
	set("api", true)
	set("registry", s.Reg.Ping() == nil)
	set("database", s.Metering != nil && s.Metering.DB().QueryRow(`SELECT 1`).Scan(&one) == nil)
	if s.Store.Enabled() {
		set("storage", s.storeUp.check(s.Store.URL))
	}
	alive, total := 0, 0
	for _, n := range s.Fleet.All() {
		total++
		if n.Alive() {
			alive++
		}
	}
	comp["sandboxes"] = "operational"
	switch {
	case total == 0 || alive == 0:
		comp["sandboxes"] = "outage"
	case alive < total:
		comp["sandboxes"] = "degraded"
	}
	overall := "operational"
	for name, v := range comp {
		if v == "outage" && (name == "registry" || name == "sandboxes" || name == "database") {
			overall = "outage"
		} else if v != "operational" && overall == "operational" {
			overall = "degraded"
		}
	}
	code := 200
	if overall == "outage" {
		code = 503
	}
	sendJSON(w, code, map[string]any{"status": overall, "components": comp, "updated": time.Now().Unix()})
}

func isUpgrade(r *http.Request) bool { return strings.EqualFold(r.Header.Get("Upgrade"), "websocket") }

var reSafeSandboxRead = regexp.MustCompile(`^/sandboxes/[^/]+(/(checkpoints|egress|metrics))?$`)

// suspendedMayDo: what an account under suspension can still do. The node resumes a paused sandbox transparently on exec, files, events, ports ... (even
// a GET), so "read-only" cannot mean "any GET": only the listings and the information that does not touch the guest are allowed, plus DELETE to clean up.
func suspendedMayDo(r *http.Request, path string) bool {
	switch r.Method {
	case "DELETE":
		return true
	case "GET", "HEAD":
		if isUpgrade(r) {
			return false
		}
		if strings.HasPrefix(path, "/sandboxes/") {
			return reSafeSandboxRead.MatchString(path)
		}
		return true
	}
	return false
}

// filterSandboxes: the list's query parameters (D70) — ?status=running (repeatable), ?metadata=key:value (repeatable, all must match), ?q=text (id / template / node /
// metadata substring), ?limit= and ?offset= (newest first). With none of them the answer is what it always was: everything, in the order the nodes gave. total is the
// count before limit / offset.
func filterSandboxes(all []map[string]any, q url.Values) ([]map[string]any, int) {
	want := map[string]bool{}
	for _, st := range q["status"] {
		want[st] = true
	}
	md := map[string]string{}
	for _, kv := range q["metadata"] {
		if k, v, ok := strings.Cut(kv, ":"); ok && k != "" {
			md[k] = v
		}
	}
	text := strings.ToLower(q.Get("q"))
	out := make([]map[string]any, 0, len(all))
	for _, sb := range all {
		if len(want) > 0 {
			if st, _ := sb["status"].(string); !want[st] {
				continue
			}
		}
		if len(md) > 0 {
			have := map[string]string{}
			switch m := sb["metadata"].(type) { // the route keeps map[string]string; a node's own answer decodes to map[string]any
			case map[string]string:
				have = m
			case map[string]any:
				for k, v := range m {
					have[k] = fmt.Sprint(v)
				}
			}
			ok := true
			for k, v := range md {
				if got, present := have[k]; !present || got != v {
					ok = false
				}
			}
			if !ok {
				continue
			}
		}
		if text != "" {
			hay := strings.ToLower(fmt.Sprint(sb["id"], " ", sb["template"], " ", sb["node"], " ", sb["metadata"]))
			if !strings.Contains(hay, text) {
				continue
			}
		}
		out = append(out, sb)
	}
	total := len(out)
	limit, _ := strconv.Atoi(q.Get("limit"))
	offset, _ := strconv.Atoi(q.Get("offset"))
	if limit > 0 || offset > 0 {
		sort.SliceStable(out, func(i, j int) bool { return num(out[i]["created"]) > num(out[j]["created"]) })
	}
	if offset > 0 {
		if offset > len(out) {
			offset = len(out)
		}
		out = out[offset:]
	}
	if limit > 0 && limit < len(out) {
		out = out[:limit]
	}
	return out, total
}

func num(v any) float64 { f, _ := v.(float64); return f }

// WebhookResolver answers the project questions the audit lines leave open (D72).
type WebhookResolver struct {
	Life    *lifecycle.Lifecycle
	Builds  *images.Builds
	Tenancy *tenancy.Tenancy
}

func (r WebhookResolver) ProjectOfSandbox(id string) string { return r.Life.ProjectOf(id) }
func (r WebhookResolver) ProjectOfBuild(id string) string {
	if b, err := r.Builds.Get(id); err == nil {
		return b.Project
	}
	return ""
}
func (r WebhookResolver) ProjectsOfAccount(account string) []string {
	ps, _ := r.Tenancy.Projects(account)
	out := make([]string, 0, len(ps))
	for _, p := range ps {
		out = append(out, p.ID)
	}
	return out
}
