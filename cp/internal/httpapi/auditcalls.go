package httpapi

import (
	"encoding/json"
	"net/http"
	"regexp"
	"time"

	"fcnode/cp/internal/tenancy"
)

// actor is the name an audit row gives the caller. The deployer token's identity is the user "default", which is also the name of the default account and
// project, so a row saying `by: default` could not be told from a member of it: the deployer is written as such (D80).
func actor(id tenancy.Identity) string {
	if id.Admin() {
		return "deployer"
	}
	return id.User
}

// secretShapes are what a command line most often carries by accident. This is a net, not a guarantee: the row keeps 200 characters of the command and the
// shapes below are blanked; anything else a person types into a command is in the log (and was already in the node's own log).
var secretShapes = []*regexp.Regexp{
	regexp.MustCompile(`(?i)(authorization:\s*(?:bearer|basic|token)\s+)\S+`),
	regexp.MustCompile(`(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}`),
	regexp.MustCompile(`(?i)\b([A-Za-z0-9_]*(?:token|secret|passw(?:or)?d|api[_-]?key|access[_-]?key)[A-Za-z0-9_]*\s*[=:]\s*)\S+`),
	regexp.MustCompile(`\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|cairn_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{16})\b`),
}

func redactCmd(cmd string, max int) string {
	for _, re := range secretShapes {
		cmd = re.ReplaceAllStringFunc(cmd, func(m string) string {
			if sub := re.FindStringSubmatch(m); len(sub) > 1 && sub[1] != "" {
				return sub[1] + "***"
			}
			return "***"
		})
	}
	if r := []rune(cmd); len(r) > max {
		return string(r[:max]) + "…"
	}
	return cmd
}

var reSessionExec = regexp.MustCompile(`^/sessions/[^/]+/exec$`)

// capWriter passes a response through and remembers its status and (for the calls where it matters) the start of its body.
type capWriter struct {
	http.ResponseWriter
	status int
	keep   int
	body   []byte
}

func (c *capWriter) WriteHeader(code int) { c.status = code; c.ResponseWriter.WriteHeader(code) }
func (c *capWriter) Write(b []byte) (int, error) {
	if c.status == 0 {
		c.status = 200
	}
	if room := c.keep - len(c.body); room > 0 {
		c.body = append(c.body, b[:min(room, len(b))]...)
	}
	return c.ResponseWriter.Write(b)
}
func (c *capWriter) Flush() {
	if f, ok := c.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// sandboxCall sends a /sandboxes/<id><sub> call to the owning node and, for the calls that run something or move a file, records a row in the hash chain once
// the node has answered (the exit code is only known then). Rows carry the project, so a member reads their own. CP_AUDIT_EXEC=0 turns the rows off.
func (s *Server) sandboxCall(w http.ResponseWriter, r *http.Request, id tenancy.Identity, sid, sub string, raw []byte, body map[string]any) {
	if !s.AuditExec {
		s.Exec.Forward(w, r, sid, sub, raw, body, sendJSON)
		return
	}
	row := map[string]any{"sandbox": sid, "project": s.Life.ProjectOf(sid), "by": actor(id)}
	keep := 0
	switch {
	case r.Method == "POST" && (sub == "/exec" || sub == "/exec/stream" || reSessionExec.MatchString(sub)):
		cmd, _ := body["cmd"].(string)
		row["op"], row["cmd"] = "exec", redactCmd(cmd, 200)
		if u, _ := body["user"].(string); u != "" {
			row["user"] = u
		}
		if sub == "/exec/stream" {
			row["stream"] = true
			s.Audit.Write(row) // a stream has no single answer to wait for: the request is the record
			s.Exec.Forward(w, r, sid, sub, raw, body, sendJSON)
			return
		}
		keep = 1 << 20
	case sub == "/files" && (r.Method == "PUT" || r.Method == "GET"):
		row["op"], row["path"] = "files_"+map[string]string{"PUT": "write", "GET": "read"}[r.Method], r.URL.Query().Get("path")
		if r.Method == "PUT" {
			row["bytes"] = len(raw)
		}
	default:
		s.Exec.Forward(w, r, sid, sub, raw, body, sendJSON)
		return
	}
	cw, t0 := &capWriter{ResponseWriter: w, keep: keep}, time.Now()
	s.Exec.Forward(cw, r, sid, sub, raw, body, sendJSON)
	row["status"], row["s"] = cw.status, float64(time.Since(t0).Milliseconds())/1000
	if keep > 0 {
		var m map[string]any
		if json.Unmarshal(cw.body, &m) == nil {
			if e, ok := m["exit"]; ok {
				row["exit"] = e
			}
			if m["oom_killed"] == true {
				row["oom_killed"] = true
			}
		}
	}
	s.Audit.Write(row)
}
