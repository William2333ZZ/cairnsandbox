package tenancy

import (
	"errors"
	"sync"
	"time"

	"fcnode/cp/internal/registry"
)

// RoleViewer: read-only. Keys (below) can be viewers; a viewer's request is refused at the edge unless it is a GET / HEAD.
const RoleViewer = "viewer"

// Key is a named credential scoped to one project (D56): what a CI job, an agent or a dashboard holds instead of a person's token.
type Key struct {
	ID        string  `json:"id"`
	Account   string  `json:"account"`
	Project   string  `json:"project"`
	Name      string  `json:"name"`
	Role      string  `json:"role"` // viewer | member
	Created   float64 `json:"created"`
	Expires   float64 `json:"expires"` // 0 = never
	LastUsed  float64 `json:"last_used"`
	CreatedBy string  `json:"created_by"`
}

var errKeyRole = errors.New("role: viewer or member (a key never gets more than member)")

// KeyAdd issues a key and returns its secret once; only the hash is kept. ttlS 0 = no expiry.
func (t *Tenancy) KeyAdd(project, name, role string, ttlS float64, by string) (map[string]any, error) {
	p, err := t.Project(project)
	if err != nil {
		return nil, err
	}
	if role == "" {
		role = RoleMember
	}
	if role != RoleViewer && role != RoleMember {
		return nil, errKeyRole
	}
	if name == "" || len(name) > 64 {
		return nil, errors.New("name: 1-64 characters")
	}
	secret := "cairn_" + registry.NewToken() + registry.NewToken()[:16]
	k := Key{ID: "key-" + registry.NewToken()[:10], Account: p.Account, Project: p.ID, Name: name, Role: role, Created: now(), CreatedBy: by}
	if ttlS > 0 {
		k.Expires = k.Created + ttlS
	}
	if _, err := t.db.Exec(`INSERT INTO api_keys(id, account, project, name, role, token_sha256, created, expires, last_used, created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,0,$9)`,
		k.ID, k.Account, k.Project, k.Name, k.Role, sha(secret), k.Created, k.Expires, k.CreatedBy); err != nil {
		return nil, err
	}
	return map[string]any{"key": k, "secret": secret}, nil
}

func (t *Tenancy) Keys(project string) ([]Key, error) {
	rows, err := t.db.Query(`SELECT id, account, project, name, role, created, expires, last_used, created_by FROM api_keys WHERE project = $1 ORDER BY created`, project)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Key{}
	for rows.Next() {
		var k Key
		if err := rows.Scan(&k.ID, &k.Account, &k.Project, &k.Name, &k.Role, &k.Created, &k.Expires, &k.LastUsed, &k.CreatedBy); err != nil {
			return nil, err
		}
		out = append(out, k)
	}
	return out, nil
}

// KeyDel revokes at once; ErrNotFound when the project has no such key.
func (t *Tenancy) KeyDel(project, id string) error {
	res, err := t.db.Exec(`DELETE FROM api_keys WHERE id = $1 AND project = $2`, id, project)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return nil
}

var touched struct {
	sync.Mutex
	at map[string]time.Time
}

// resolveKey: the identity behind an API key, or false. last_used is written at most once a minute per key (every request would be a write).
func (t *Tenancy) resolveKey(bearer string) (Identity, bool) {
	var k Key
	err := t.db.QueryRow(`SELECT id, account, project, role, expires FROM api_keys WHERE token_sha256 = $1`, sha(bearer)).Scan(&k.ID, &k.Account, &k.Project, &k.Role, &k.Expires)
	if err != nil || (k.Expires > 0 && now() > k.Expires) {
		return Identity{}, false
	}
	touched.Lock()
	if touched.at == nil {
		touched.at = map[string]time.Time{}
	}
	stale := time.Since(touched.at[k.ID]) > time.Minute
	if stale {
		touched.at[k.ID] = time.Now()
	}
	touched.Unlock()
	if stale {
		_, _ = t.db.Exec(`UPDATE api_keys SET last_used = $1 WHERE id = $2`, now(), k.ID)
	}
	return Identity{User: "key:" + k.ID, Account: k.Account, Role: k.Role, Project: k.Project}, true
}
