package network

import (
	"database/sql"
	"encoding/json"
	"errors"
	"reflect"
	"time"

	"fcnode/cp/internal/db"
	"fcnode/cp/internal/registry"
)

// Relaxing a sandbox's egress needs an owner's approval; tightening takes effect at once (docs/002 沙箱侧 6 PolicyRelaxRequested; D36 补).
// A Request is a member's asked-for policy, kept until an owner decides.
type Request struct {
	ID        string         `json:"id"`
	Sandbox   string         `json:"sandbox"`
	Project   string         `json:"project"`
	By        string         `json:"by"`
	Body      map[string]any `json:"network"`
	Created   float64        `json:"created"`
	Status    string         `json:"status"` // pending | approved | denied
	DecidedBy string         `json:"decided_by,omitempty"`
	Decided   float64        `json:"decided,omitempty"`
}

var modeRank = map[string]int{"closed": 0, "allowlist": 1, "audited": 2}

func modeOf(net map[string]any) string {
	if m, _ := net["mode"].(string); m != "" {
		return m
	}
	if hs, _ := net["allow_hosts"].([]any); len(hs) > 0 {
		return "allowlist"
	}
	return "closed"
}

func strSet(v any) map[string]bool {
	out := map[string]bool{}
	arr, _ := v.([]any)
	for _, x := range arr {
		if s, ok := x.(string); ok {
			out[s] = true
		}
	}
	return out
}

func numOf(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case int:
		return float64(x)
	case int64:
		return float64(x)
	}
	return 0
}

// Relaxes says whether `next` (the keys it carries; the node merges key by key) lets the sandbox reach more than `cur` does: a looser mode,
// a new allowed host or direct address, a new allow rule or a dropped deny rule, a higher or removed limit.
func Relaxes(cur, next map[string]any) bool {
	if cur == nil {
		cur = map[string]any{}
	}
	if _, has := next["mode"]; has || next["allow_hosts"] != nil {
		if modeRank[modeOf(next)] > modeRank[modeOf(cur)] {
			return true
		}
	}
	for _, k := range []string{"allow_hosts", "allow_direct"} {
		if v, has := next[k]; has {
			have := strSet(cur[k])
			for h := range strSet(v) {
				if !have[h] {
					return true
				}
			}
		}
	}
	if rs, has := next["rules"]; has {
		nextRules, _ := rs.([]any)
		curRules, _ := cur["rules"].([]any)
		contains := func(list []any, r any) bool {
			for _, x := range list {
				if reflect.DeepEqual(norm(x), norm(r)) {
					return true
				}
			}
			return false
		}
		for _, r := range nextRules {
			if m, _ := r.(map[string]any); m != nil {
				if a, _ := m["action"].(map[string]any); a != nil && a["allow"] == true && !contains(curRules, r) {
					return true
				}
			}
		}
		for _, r := range curRules {
			if m, _ := r.(map[string]any); m != nil {
				if a, _ := m["action"].(map[string]any); a != nil && a["allow"] == false && !contains(nextRules, r) {
					return true
				}
			}
		}
	}
	if lm, has := next["limits"].(map[string]any); has {
		curL, _ := cur["limits"].(map[string]any)
		for k, v := range lm {
			nv, cv := numOf(v), numOf(curL[k])
			if cv > 0 && (nv == 0 || nv > cv) {
				return true
			}
		}
	}
	return false
}

// norm round-trips through JSON so numbers and nesting compare alike.
func norm(v any) any {
	b, _ := json.Marshal(v)
	var out any
	json.Unmarshal(b, &out)
	return out
}

type Requests struct{ db *db.DB }

func NewRequests(d *db.DB) *Requests { return &Requests{db: d} }

var ErrNoRequest = errors.New("no such request")

func (q *Requests) Add(sandbox, project, by string, body map[string]any) (Request, error) {
	r := Request{ID: "nr-" + registry.NewToken()[:10], Sandbox: sandbox, Project: project, By: by, Body: body, Created: now(), Status: "pending"}
	b, _ := json.Marshal(body)
	_, err := q.db.Exec(`INSERT INTO network_requests(id, sandbox, project, by_user, body, created, status, decided_by, decided) VALUES ($1, $2, $3, $4, $5, $6, 'pending', '', 0)`,
		r.ID, sandbox, project, by, string(b), r.Created)
	return r, err
}

func (q *Requests) scan(rows interface {
	Scan(dest ...any) error
}) (Request, error) {
	var r Request
	var body string
	if err := rows.Scan(&r.ID, &r.Sandbox, &r.Project, &r.By, &body, &r.Created, &r.Status, &r.DecidedBy, &r.Decided); err != nil {
		return r, err
	}
	r.Body = map[string]any{}
	json.Unmarshal([]byte(body), &r.Body)
	return r, nil
}

const cols = `id, sandbox, project, by_user, body, created, status, decided_by, decided`

func (q *Requests) Get(id string) (Request, error) {
	r, err := q.scan(q.db.QueryRow(`SELECT `+cols+` FROM network_requests WHERE id = $1`, id))
	if err == sql.ErrNoRows {
		return r, ErrNoRequest
	}
	return r, err
}

// List: the requests of the given projects (nil = all), pending ones only unless all is set; newest first.
func (q *Requests) List(projects map[string]bool, all bool) ([]Request, error) {
	rows, err := q.db.Query(`SELECT ` + cols + ` FROM network_requests ORDER BY created DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Request{}
	for rows.Next() {
		r, err := q.scan(rows)
		if err != nil {
			return nil, err
		}
		if (projects == nil || projects[r.Project]) && (all || r.Status == "pending") {
			out = append(out, r)
		}
	}
	return out, nil
}

func (q *Requests) Decide(id, by, status string) (Request, error) {
	r, err := q.Get(id)
	if err != nil {
		return r, err
	}
	if r.Status != "pending" {
		return r, errors.New("already " + r.Status)
	}
	r.Status, r.DecidedBy, r.Decided = status, by, now()
	_, err = q.db.Exec(`UPDATE network_requests SET status = $1, decided_by = $2, decided = $3 WHERE id = $4`, status, by, r.Decided, id)
	return r, err
}

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }
