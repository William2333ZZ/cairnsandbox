// Package e2b is the E2B-shaped control surface (docs/002 沙箱侧 8 的另一张皮; D40): the REST API E2B's SDKs use to create, list, pause, resume and kill
// sandboxes and to read templates, served on its own port so its paths do not collide with CONTRACT §1. Authentication is E2B's `X-API-Key` header,
// whose value is one of our member tokens. The data plane the SDKs then expect inside the sandbox (envd on port 49983, Connect-RPC) is not served:
// commands and files go through CONTRACT §1 / §2 and the port exposure of D39.
package e2b

import (
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"

	"fcnode/cp/internal/images"
	"fcnode/cp/internal/lifecycle"
	"fcnode/cp/internal/tenancy"
)

type Server struct {
	Tenancy *tenancy.Tenancy
	Life    *lifecycle.Lifecycle
	Images  *images.Images
	Default string // the template name to use when the SDK names none (E2B's "base")
}

var reSandbox = regexp.MustCompile(`^/sandboxes/([^/]+)(/(pause|resume|timeout|refreshes|connect))?$`)

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
	case path == "/sandboxes" && r.Method == "POST":
		s.create(w, body, project)
	case (path == "/sandboxes" || path == "/v2/sandboxes") && r.Method == "GET":
		out := []map[string]any{}
		for _, sb := range s.Life.List(scope) {
			out = append(out, s.view(sb))
		}
		send(w, 200, out)
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
			send(w, 200, s.view(withNode(out, s.Life.NodeOf(sid), p)))
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
	send(w, 201, s.view(info))
}

// view: our sandbox info in E2B's shape.
func (s *Server) view(sb map[string]any) map[string]any {
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
		"cpuCount": sb["vcpu"], "memoryMB": sb["mem"], "diskSizeMB": sb["disk"], "envdVersion": "none", "envdAccessToken": "", "metadata": sb["metadata"], "project": sb["project"]}
	return out
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
