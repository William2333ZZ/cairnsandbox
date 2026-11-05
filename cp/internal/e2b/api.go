// Package e2b is the E2B-shaped control surface (docs/002 沙箱侧 8 的另一张皮; D40): the REST API E2B's SDKs use to create, list, pause, resume and kill
// sandboxes and to read templates, served on its own port so its paths do not collide with CONTRACT §1. Authentication is E2B's `X-API-Key` header,
// whose value is one of our member tokens. The data plane the SDKs then expect inside the sandbox (envd, Connect-RPC JSON and /files) is relayed from here to
// the node and over vsock to sandboxd (envd.go); E2B_SANDBOX_URL points the SDKs at this same port.
package e2b

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/images"
	"fcnode/cp/internal/lifecycle"
	"fcnode/cp/internal/nodeclient"
	"fcnode/cp/internal/tenancy"
)

type Server struct {
	Tenancy *tenancy.Tenancy
	Life    *lifecycle.Lifecycle
	Images  *images.Images
	Default string // the template name to use when the SDK names none (E2B's "base")
	// The envd side (envd.go): the SDKs' data plane, relayed to the node that owns the sandbox.
	Fleet  *fleet.Fleet
	NC     nodeclient.Client
	Audit  Auditor
	Secret []byte // shared by every replica; envd access tokens are HMACs under it
	// PreviewDomain (D55): when set, sandboxes report it as their `domain`, so the SDKs' get_host(port) = "<port>-<sandboxID>.<domain>" is a host this control plane
	// already routes to /sandboxes/<id>/ports/<port>/ (the browser opens it with ?token=, see docs/guide/manual.md).
	PreviewDomain string
}

var reSandbox = regexp.MustCompile(`^/(?:v2/)?sandboxes/([^/]+)(/(pause|resume|timeout|refreshes|connect|fork|network|metrics|snapshots))?$`)

func send(w http.ResponseWriter, code int, v any) {
	b, _ := json.Marshal(v)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", fmt.Sprint(len(b)))
	w.WriteHeader(code)
	w.Write(b)
}

func e2bErr(w http.ResponseWriter, code int, msg string) {
	send(w, code, map[string]any{"code": code, "message": msg})
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("E2b-Sandbox-Id") != "" { // an SDK talking to a sandbox, not to the API (E2B_SANDBOX_URL points here too)
		s.serveEnvd(w, r)
		return
	}
	path := strings.TrimSuffix(r.URL.Path, "/")
	if path == "/health" {
		send(w, 200, map[string]any{"ok": true, "surface": "e2b"})
		return
	}
	key := r.Header.Get("X-API-Key")
	if key == "" {
		key = strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	}
	id, ok := s.Tenancy.Resolve(strings.TrimSpace(key))
	if !ok {
		e2bErr(w, 401, "invalid API key")
		return
	}
	project, err := s.Tenancy.ResolveProject(id, r.Header.Get("X-Project"))
	if err != nil {
		e2bErr(w, 403, "no project for this key")
		return
	}
	if id.Role == tenancy.RoleViewer && r.Method != "GET" && r.Method != "HEAD" { // D56
		e2bErr(w, 403, "read-only credential")
		return
	}
	if !id.Admin() && r.Method != "GET" && r.Method != "HEAD" && r.Method != "DELETE" { // D53: the same stop as the native surface
		if sp, on := s.Tenancy.SuspensionOf(id.Account); on {
			e2bErr(w, 403, "account suspended: "+sp.Reason)
			return
		}
	}
	var body map[string]any
	if r.Body != nil {
		json.NewDecoder(r.Body).Decode(&body)
	}
	if body == nil {
		body = map[string]any{}
	}
	scope := map[string]bool{}
	if !id.Admin() {
		ps, _ := s.Tenancy.Projects(id.Account)
		if id.Project != "" {
			ps = nil
			scope[id.Project] = true
		}
		for _, p := range ps {
			scope[p.ID] = true
		}
	} else {
		scope = nil
	}
	switch {
	case (path == "/sandboxes" || path == "/v2/sandboxes") && r.Method == "POST":
		s.create(w, body, project)
	case (path == "/sandboxes" || path == "/v2/sandboxes") && r.Method == "GET":
		s.list(w, r, scope)
	case path == "/sandboxes/metrics" && r.Method == "GET":
		s.batchMetrics(w, r, scope)
	case path == "/snapshots" && r.Method == "GET":
		s.snapshots(w, scope)
	case strings.HasPrefix(path, "/templates/") && r.Method == "DELETE":
		s.deleteSnapshot(w, id, scope, strings.TrimPrefix(path, "/templates/"))
	case path == "/templates" && r.Method == "GET":
		out := []map[string]any{}
		for _, t := range s.Images.Templates() {
			key, _ := t["key"].(string)
			out = append(out, map[string]any{"templateID": key, "aliases": []string{key}, "buildID": key, "cpuCount": t["vcpu"], "memoryMB": t["mem"], "public": false, "createdAt": stamp(t["created"]), "node": t["node"]})
		}
		send(w, 200, out)
	case reSandbox.MatchString(path):
		mm := reSandbox.FindStringSubmatch(path)
		sid, op := mm[1], mm[3]
		p := s.Life.ProjectOf(sid)
		if p == "" || (scope != nil && !scope[p]) {
			e2bErr(w, 404, "sandbox not found")
			return
		}
		switch {
		case op == "" && r.Method == "GET":
			for _, sb := range s.Life.List(map[string]bool{p: true}) {
				if sb["id"] == sid {
					send(w, 200, s.view(sb))
					return
				}
			}
			e2bErr(w, 404, "sandbox not found")
		case op == "" && r.Method == "DELETE":
			st, _ := s.Life.NodeCall(sid, "DELETE", "", nil)
			if st == 200 {
				w.WriteHeader(204)
				return
			}
			e2bErr(w, st, "kill failed")
		case op == "pause" && r.Method == "POST":
			st, out := s.Life.NodeCall(sid, "POST", "/pause", nil)
			s.relay(w, st, out, 204)
		case op == "resume" && r.Method == "POST":
			st, out := s.Life.NodeCall(sid, "POST", "/resume", nil)
			s.relay(w, st, out, 204)
		case op == "connect" && r.Method == "POST":
			st, out := s.Life.NodeCall(sid, "POST", "/resume", nil) // connect = make sure it runs, then describe it
			if st != 200 {
				s.relay(w, st, out, 200)
				return
			}
			if secs, _ := body["timeout"].(float64); secs > 0 { // v2 connect extends the lifetime
				s.Life.NodeCall(sid, "POST", "/ttl", map[string]any{"ttl_idle_s": int(secs)})
			}
			send(w, 200, s.viewTok(withNode(out, s.Life.NodeOf(sid), p), true))
		case op == "snapshots" && r.Method == "POST":
			s.snapshot(w, sid, p)
		case op == "fork" && r.Method == "POST":
			s.fork(w, sid, p, body)
		case op == "network" && r.Method == "PUT":
			s.network(w, id, sid, body)
		case op == "metrics" && r.Method == "GET":
			s.metrics(w, r, sid)
		case (op == "timeout" || op == "refreshes") && r.Method == "POST":
			secs, _ := body["timeout"].(float64)
			if secs <= 0 {
				secs = 300
			}
			st, out := s.Life.NodeCall(sid, "POST", "/ttl", map[string]any{"ttl_idle_s": int(secs)})
			s.relay(w, st, out, 204)
		default:
			e2bErr(w, 404, "no route")
		}
	default:
		e2bErr(w, 404, "no route")
	}
}

func (s *Server) relay(w http.ResponseWriter, st int, out map[string]any, okCode int) {
	if st == 200 || st == 201 {
		if okCode == 204 {
			w.WriteHeader(204)
			return
		}
		send(w, okCode, out)
		return
	}
	msg, _ := out["error"].(string)
	if st == 0 {
		st = 502
	}
	e2bErr(w, st, msg)
}

// create maps E2B's body onto ours: templateID -> template_key (a named template) or the default, timeout (seconds) -> ttl_idle_s, metadata kept on the route.
func (s *Server) create(w http.ResponseWriter, body map[string]any, project string) {
	spec := map[string]any{"class": "persistent"}
	tpl, _ := body["templateID"].(string)
	if tpl == "" {
		tpl, _ = body["template"].(string)
	}
	if tpl != "" && tpl != "base" {
		spec["template_key"] = tpl
		spec["boot"] = "snapshot"
	} else if s.Default != "" {
		spec["template_key"] = s.Default
		spec["boot"] = "snapshot"
	}
	if t, ok := body["timeout"].(float64); ok && t > 0 {
		spec["ttl_idle_s"] = int(t)
	}
	if md, ok := body["metadata"].(map[string]any); ok {
		meta := map[string]string{}
		for k, v := range md {
			meta[k] = fmt.Sprint(v)
		}
		spec["metadata"] = meta
	}
	if pol := networkPolicy(mapOf(body["network"])); pol != nil {
		spec["network"] = pol
	} else if b, ok := body["allow_internet_access"].(bool); ok {
		if b {
			spec["network"] = map[string]any{"mode": "audited"} // asked for the internet: everything allowed and every request recorded
		} else {
			spec["network"] = map[string]any{"mode": "closed"}
		}
	}
	if ev, ok := body["envVars"].(map[string]any); ok && len(ev) > 0 {
		spec["env"] = ev // the node puts these under every exec's environment (there is no init process to hold them)
	}
	res := s.Life.Create(spec, project)
	if res.Code != 201 {
		msg := fmt.Sprint(res.Body)
		if m, ok := res.Body.(map[string]any); ok {
			msg, _ = m["error"].(string)
		}
		e2bErr(w, res.Code, msg)
		return
	}
	info, _ := res.Body.(map[string]any)
	send(w, 201, s.viewTok(info, true))
}

// view: our sandbox info in E2B's shape.
func (s *Server) view(sb map[string]any) map[string]any {
	return s.viewTok(sb, false)
}

// viewTok: the same, with the sandbox's envd access token when the caller just created or connected to it. The token opens a shell in the sandbox, so
// list and get never carry it (a read-only key can list): only create, connect and fork do, and those need a writer.
func (s *Server) viewTok(sb map[string]any, withToken bool) map[string]any {
	id, _ := sb["id"].(string)
	state := "running"
	switch sb["status"] {
	case "paused":
		state = "paused"
	case "unreachable", "failed":
		state = "unknown"
	}
	tpl, _ := sb["template_snapshot"].(string)
	if tpl == "" {
		tpl, _ = sb["template"].(string)
	}
	created, _ := sb["created"].(float64)
	ttl, _ := sb["ttl_idle_s"].(float64)
	idle, _ := sb["idle_s"].(float64)
	endAt := ""
	if ttl > 0 {
		endAt = stamp(float64(time.Now().Unix()) + ttl - idle)
	}
	out := map[string]any{"sandboxID": id, "templateID": tpl, "clientID": sb["node"], "alias": tpl, "state": state, "startedAt": stamp(created), "endAt": endAt,
		"cpuCount": sb["vcpu"], "memoryMB": sb["mem"], "diskSizeMB": sb["disk"], "envdVersion": EnvdVersion, "envdAccessToken": "", "metadata": sb["metadata"], "project": sb["project"]}
	if s.PreviewDomain != "" {
		out["domain"] = s.PreviewDomain
	}
	if withToken {
		out["envdAccessToken"] = s.EnvdToken(id)
		out["trafficAccessToken"] = s.TrafficToken(id)
	}
	return out
}

func mapOf(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func withNode(info map[string]any, node, project string) map[string]any {
	info["node"], info["project"] = node, project
	return info
}

func stamp(v any) string {
	f, _ := v.(float64)
	if f == 0 {
		return ""
	}
	return time.Unix(int64(f), 0).UTC().Format(time.RFC3339)
}

// list: GET /sandboxes and /v2/sandboxes with E2B's filters (state=running,paused; metadata=k=v&k2=v2, URL-encoded) and its pagination (limit, nextToken in
// the x-next-token header; the token here is just an offset into the list ordered by creation).
func (s *Server) list(w http.ResponseWriter, r *http.Request, scope map[string]bool) {
	q := r.URL.Query()
	states := map[string]bool{}
	for _, v := range q["state"] {
		for _, x := range strings.Split(v, ",") {
			if x = strings.TrimSpace(x); x != "" {
				states[x] = true
			}
		}
	}
	meta, _ := url.ParseQuery(q.Get("metadata"))
	all := []map[string]any{}
	for _, sb := range s.Life.List(scope) {
		v := s.view(sb)
		if len(states) > 0 && !states[fmt.Sprint(v["state"])] {
			continue
		}
		md, _ := v["metadata"].(map[string]string)
		okm := true
		for k, vs := range meta {
			if len(vs) > 0 && md[k] != vs[0] {
				okm = false
			}
		}
		if okm {
			all = append(all, v)
		}
	}
	sort.Slice(all, func(i, j int) bool { return fmt.Sprint(all[i]["startedAt"]) < fmt.Sprint(all[j]["startedAt"]) })
	start, _ := strconv.Atoi(q.Get("nextToken"))
	if start < 0 || start > len(all) {
		start = 0
	}
	end := len(all)
	if n, _ := strconv.Atoi(q.Get("limit")); n > 0 && start+n < end {
		end = start + n
		w.Header().Set("x-next-token", strconv.Itoa(end))
	}
	send(w, 200, all[start:end])
}

// fork: POST /sandboxes/<id>/fork {timeout?, count?} -> [{sandbox}|{error}]. Our fork clones the running VM (memory and disk); the clones become routes of the
// parent's project, exactly as on the native surface.
func (s *Server) fork(w http.ResponseWriter, sid, project string, body map[string]any) {
	n := 1
	if c, ok := body["count"].(float64); ok && c >= 1 {
		n = int(c)
	}
	req := map[string]any{"n": n, "class": "persistent"}
	if t, ok := body["timeout"].(float64); ok && t > 0 {
		req["ttl_idle_s"] = int(t)
	}
	st, out := s.Life.NodeCall(sid, "POST", "/fork", req)
	if st != 201 {
		s.relay(w, st, out, 201)
		return
	}
	res := []map[string]any{}
	node := s.Life.NodeOf(sid)
	arr, _ := out["clones"].([]any)
	for _, x := range arr {
		c, ok := x.(map[string]any)
		if !ok {
			continue
		}
		id, _ := c["id"].(string)
		cls, _ := c["class"].(string)
		dur, _ := c["durable"].(bool)
		s.Life.SetRoute(id, lifecycle.Route{Node: node, Class: cls, Durable: dur, Created: float64(time.Now().UnixNano()) / 1e9, Project: project})
		res = append(res, map[string]any{"sandbox": s.viewTok(withNode(c, node, project), true)})
	}
	send(w, 201, res)
}

// network: PUT /sandboxes/<id>/network {allowOut?, denyOut?} onto our policy. A member's loosening of the policy needs an owner's approval on the native
// surface (D79); this surface has no approval queue, so only an owner or admin may change the network here.
func (s *Server) network(w http.ResponseWriter, id tenancy.Identity, sid string, body map[string]any) {
	if !id.Admin() && id.Role != tenancy.RoleOwner {
		e2bErr(w, 403, "changing a sandbox's network needs an owner; members use PUT /sandboxes/<id>/network on the native API (it queues loosening for approval)")
		return
	}
	pol := networkPolicy(body)
	if pol == nil {
		e2bErr(w, 400, "nothing to apply: give allowOut and/or denyOut")
		return
	}
	st, out := s.Life.NodeCall(sid, "PUT", "/network", pol)
	s.relay(w, st, out, 204)
}

// networkPolicy maps E2B's {allowOut: [host|ip|cidr], denyOut: [...]} to ours: allowOut present = an allowlist (hosts by name, addresses direct),
// denyOut alone = closed (E2B's deny-all idiom is denyOut ["0.0.0.0/0"]).
func networkPolicy(body map[string]any) map[string]any {
	allow, _ := body["allowOut"].([]any)
	deny, _ := body["denyOut"].([]any)
	if len(allow) == 0 && len(deny) == 0 {
		return nil
	}
	if len(allow) == 0 {
		return map[string]any{"mode": "closed", "allow_hosts": []string{}, "allow_direct": []string{}, "rules": []any{}}
	}
	hosts, direct := []string{}, []string{}
	for _, a := range allow {
		v := strings.TrimSpace(fmt.Sprint(a))
		if net.ParseIP(strings.SplitN(v, "/", 2)[0]) != nil {
			direct = append(direct, v)
		} else {
			hosts = append(hosts, v)
		}
	}
	return map[string]any{"mode": "allowlist", "allow_hosts": hosts, "allow_direct": direct, "rules": []any{}}
}

// metrics: GET /sandboxes/<id>/metrics -> E2B's array of points (bytes, percent of all cores) from our node's cgroup samples.
func (s *Server) metrics(w http.ResponseWriter, r *http.Request, sid string) {
	sub := ""
	if st := r.URL.Query().Get("start"); st != "" {
		sub = "?since=" + url.QueryEscape(st)
	}
	st, out := s.Life.NodeCall(sid, "GET", "/metrics"+sub, nil)
	if st != 200 {
		s.relay(w, st, out, 200)
		return
	}
	send(w, 200, s.points(sid, out))
}

// metricPoints: the sandbox's samples in E2B's shape (nil when the node has none or cannot answer).
func (s *Server) metricPoints(sid, sub string) []map[string]any {
	st, out := s.Life.NodeCall(sid, "GET", "/metrics"+sub, nil)
	if st != 200 {
		return nil
	}
	return s.points(sid, out)
}

func (s *Server) points(sid string, out map[string]any) []map[string]any {
	vcpu, mem, disk := 1.0, 0.0, 0.0
	if _, info := s.Life.NodeCall(sid, "GET", "", nil); info != nil {
		if v, ok := info["vcpu"].(float64); ok && v > 0 {
			vcpu = v
		}
		mem, _ = info["mem"].(float64)
		disk, _ = info["disk"].(float64)
	}
	res := []map[string]any{}
	pts, _ := out["points"].([]any)
	for _, x := range pts {
		p, _ := x.(map[string]any)
		t, _ := p["t"].(float64)
		cpu, _ := p["cpu_pct"].(float64)
		mu, _ := p["mem_mib"].(float64)
		res = append(res, map[string]any{"timestamp": time.Unix(int64(t), 0).UTC().Format(time.RFC3339), "timestampUnix": int64(t), "cpuCount": int(vcpu), "cpuUsedPct": cpu / vcpu,
			"memUsed": int64(mu * 1048576), "memTotal": int64(mem * 1048576), "memCache": 0, "diskUsed": 0, "diskTotal": int64(disk * 1048576)})
	}
	return res
}

// Snapshots (E2B: a persistent snapshot of a sandbox that later sandboxes are created from, by id as the templateID) are our "commit a sandbox as a template"
// (D37: memory and writable layer). The name encodes the owning project (snap-<6 hex of sha256(project)>-<8 random hex>) because template names are one
// global namespace and the listing has to know whose they are.
func snapTag(project string) string {
	h := sha256.Sum256([]byte(project))
	return "snap-" + hex.EncodeToString(h[:3]) + "-"
}

func (s *Server) snapshot(w http.ResponseWriter, sid, project string) {
	rnd := make([]byte, 4)
	rand.Read(rnd)
	name := snapTag(project) + hex.EncodeToString(rnd)
	st, out := s.Life.NodeCall(sid, "POST", "/commit", map[string]any{"name": name, "note": "E2B snapshot of " + sid})
	if st != 201 {
		s.relay(w, st, out, 201)
		return
	}
	send(w, 201, map[string]any{"snapshotID": name, "names": []string{name}})
}

func (s *Server) snapshots(w http.ResponseWriter, scope map[string]bool) {
	var tags []string
	for p := range scope {
		tags = append(tags, snapTag(p))
	}
	out := []map[string]any{}
	for _, t := range s.Images.Templates() {
		key, _ := t["key"].(string)
		if !strings.HasPrefix(key, "snap-") {
			continue
		}
		mine := scope == nil
		for _, tg := range tags {
			if strings.HasPrefix(key, tg) {
				mine = true
			}
		}
		if mine {
			out = append(out, map[string]any{"snapshotID": key, "names": []string{key}})
		}
	}
	send(w, 200, out)
}

// deleteSnapshot: DELETE /templates/<snapshotID>. Only snapshots (snap-...) of the caller's own projects; the deployer may delete any.
func (s *Server) deleteSnapshot(w http.ResponseWriter, id tenancy.Identity, scope map[string]bool, name string) {
	own := id.Admin()
	for p := range scope {
		if strings.HasPrefix(name, snapTag(p)) {
			own = true
		}
	}
	if !strings.HasPrefix(name, "snap-") || !own {
		e2bErr(w, 404, "snapshot not found")
		return
	}
	res := s.Images.DeleteTemplate(name)
	if res.Code != 200 {
		m, _ := res.Body.(map[string]any)
		msg, _ := m["error"].(string)
		e2bErr(w, res.Code, msg)
		return
	}
	w.WriteHeader(204)
}

// batchMetrics: GET /sandboxes/metrics?sandbox_ids=a,b -> {"sandboxes": {id: latest metric}}; ids outside the caller's projects are left out, like unknown ones.
func (s *Server) batchMetrics(w http.ResponseWriter, r *http.Request, scope map[string]bool) {
	out := map[string]any{}
	ids := r.URL.Query()["sandbox_ids"]
	if len(ids) == 1 {
		ids = strings.Split(ids[0], ",")
	}
	for i, sid := range ids {
		if i >= 100 {
			break
		}
		sid = strings.TrimSpace(sid)
		p := s.Life.ProjectOf(sid)
		if p == "" || (scope != nil && !scope[p]) {
			continue
		}
		if pts := s.metricPoints(sid, ""); len(pts) > 0 {
			out[sid] = pts[len(pts)-1]
		}
	}
	send(w, 200, map[string]any{"sandboxes": out})
}
