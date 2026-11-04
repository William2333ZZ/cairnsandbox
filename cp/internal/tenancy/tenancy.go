// Package tenancy is context 1 (docs/002 沙箱侧的领域 租户与访问): Account ⊃ Project ⊃ Member (with a token), Quota per Project. Other contexts
// see an Identity (user, account, role) and a project id, never a token. The deployer (CP_TOKEN) is the admin of everything. Lives in SQL (D32).
package tenancy

import (
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"fcnode/cp/internal/db"
	"fcnode/cp/internal/registry"
)

const (
	Admin          = "default" // the deployer: CP_TOKEN
	DefaultAccount = "default"
	DefaultProject = "default"
	RoleOwner      = "owner"
	RoleMember     = "member"
)

var ErrNotFound = registry.ErrNotFound // the same value as the registry's: lifecycle tells "no such project" from "the database is down" by it
var ErrForbidden = errors.New("forbidden")
var nameRe = regexp.MustCompile(`^[a-z][a-z0-9_-]{1,31}$`)

type Account struct {
	ID      string  `json:"id"`
	Name    string  `json:"name"`
	Created float64 `json:"created"`
}

type Project struct {
	ID      string  `json:"id"`
	Account string  `json:"account"`
	Name    string  `json:"name"`
	Created float64 `json:"created"`
}

type Member struct {
	ID      string  `json:"id"`
	Account string  `json:"account"`
	User    string  `json:"user"`
	Name    string  `json:"name"`
	Role    string  `json:"role"`
	Created float64 `json:"created"`
}

// Quota: 0 = unlimited.
type Quota struct {
	Project       string  `json:"project"`
	MaxSandboxes  int64   `json:"max_sandboxes"`
	MaxStorageGiB float64 `json:"max_storage_gib"`
	DailyUSD      float64 `json:"daily_usd"`
}

// Identity is who a request is: the admin, or a member of one account.
type Identity struct {
	User    string `json:"user"`
	Account string `json:"account"`
	Role    string `json:"role"`              // admin | owner | member | viewer
	Project string `json:"project,omitempty"` // set for an API key: the one project it can reach
}

func (i Identity) Admin() bool { return i.Role == "admin" }

type Tenancy struct {
	db    *db.DB
	token string
	susp  suspCache
}

// LegacyUsers is the registry's users.json slice (docs/009), imported once into the default account.
type LegacyUsers interface {
	Users() ([]registry.User, error)
}

func New(d *db.DB, cpToken string) *Tenancy { return &Tenancy{db: d, token: cpToken} }

func (t *Tenancy) AuthEnabled() bool { return t.token != "" }
func (t *Tenancy) DB() *db.DB        { return t.db }
func (t *Tenancy) Token() string     { return t.token }

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func sha(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

// Bootstrap makes the default account and project exist and imports legacy users as members of the default account (once, when there are none).
func (t *Tenancy) Bootstrap(legacy LegacyUsers) error {
	if _, err := t.db.Exec(`INSERT INTO accounts(id, name, created) VALUES ($1, $2, $3) ON CONFLICT(id) DO NOTHING`, DefaultAccount, "default", now()); err != nil {
		return err
	}
	if _, err := t.db.Exec(`INSERT INTO projects(id, account, name, created) VALUES ($1, $2, $3, $4) ON CONFLICT(id) DO NOTHING`, DefaultProject, DefaultAccount, "default", now()); err != nil {
		return err
	}
	var n int
	if err := t.db.QueryRow(`SELECT count(*) FROM members`).Scan(&n); err != nil {
		return err
	}
	if n > 0 || legacy == nil {
		return nil
	}
	us, _ := legacy.Users()
	for _, u := range us {
		if _, err := t.db.Exec(`INSERT INTO members(id, account, usr, name, role, token_sha256, created) VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT(id) DO NOTHING`,
			"m-"+registry.NewToken()[:10], DefaultAccount, u.User, u.Name, RoleMember, u.TokenSHA256, u.Created); err != nil {
			return err
		}
	}
	return nil
}

// Resolve: who holds this bearer. With no CP_TOKEN everyone is the admin.
func (t *Tenancy) Resolve(bearer string) (Identity, bool) {
	if t.token == "" {
		return Identity{User: Admin, Account: DefaultAccount, Role: "admin"}, true
	}
	if bearer == "" {
		return Identity{}, false
	}
	if subtle.ConstantTimeCompare([]byte(bearer), []byte(t.token)) == 1 {
		return Identity{User: Admin, Account: DefaultAccount, Role: "admin"}, true
	}
	var m Member
	var expires float64
	err := t.db.QueryRow(`SELECT usr, account, role, expires FROM members WHERE token_sha256 = $1`, sha(bearer)).Scan(&m.User, &m.Account, &m.Role, &expires)
	if err != nil {
		return t.resolveKey(bearer)
	}
	if expires > 0 && now() > expires { // an expired token is as good as none (013: long-lived tokens)
		return Identity{}, false
	}
	return Identity{User: m.User, Account: m.Account, Role: m.Role}, true
}

// ---- accounts ----

func (t *Tenancy) Accounts() ([]Account, error) {
	rows, err := t.db.Query(`SELECT id, name, created FROM accounts ORDER BY created`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Account{}
	for rows.Next() {
		var a Account
		if err := rows.Scan(&a.ID, &a.Name, &a.Created); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, nil
}

func (t *Tenancy) AccountAdd(id, name string) (Account, error) {
	if !nameRe.MatchString(id) {
		return Account{}, errors.New("account id: lowercase letters, digits, _ or -, 2-32 chars")
	}
	if name == "" {
		name = id
	}
	var tomb int
	if t.db.QueryRow(`SELECT count(*) FROM account_tombstones WHERE id = $1`, id).Scan(&tomb) == nil && tomb > 0 {
		return Account{}, errors.New("account id was used by a deleted account and stays reserved")
	}
	a := Account{ID: id, Name: name, Created: now()}
	if _, err := t.db.Exec(`INSERT INTO accounts(id, name, created) VALUES ($1, $2, $3)`, a.ID, a.Name, a.Created); err != nil {
		return Account{}, errors.New("account exists")
	}
	if _, err := t.ProjectAdd(id, id, "default"); err != nil {
		return Account{}, err
	}
	return a, nil
}

func (t *Tenancy) Account(id string) (Account, error) {
	var a Account
	err := t.db.QueryRow(`SELECT id, name, created FROM accounts WHERE id = $1`, id).Scan(&a.ID, &a.Name, &a.Created)
	if err == sql.ErrNoRows {
		return a, ErrNotFound
	}
	return a, err
}

// ---- projects ----

func (t *Tenancy) Projects(account string) ([]Project, error) {
	rows, err := t.db.Query(`SELECT id, account, name, created FROM projects WHERE account = $1 ORDER BY created`, account)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Project{}
	for rows.Next() {
		var p Project
		if err := rows.Scan(&p.ID, &p.Account, &p.Name, &p.Created); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, nil
}

func (t *Tenancy) Project(id string) (Project, error) {
	var p Project
	err := t.db.QueryRow(`SELECT id, account, name, created FROM projects WHERE id = $1`, id).Scan(&p.ID, &p.Account, &p.Name, &p.Created)
	if err == sql.ErrNoRows {
		return p, ErrNotFound
	}
	return p, err
}

// ProjectAdd: id "" means <account>-<name>; the default project of an account is named "default".
func (t *Tenancy) ProjectAdd(account, id, name string) (Project, error) {
	if _, err := t.Account(account); err != nil {
		return Project{}, ErrNotFound
	}
	if name == "" {
		name = id
	}
	if id == "" {
		id = account + "-" + name
	}
	if !nameRe.MatchString(id) {
		return Project{}, errors.New("project id: lowercase letters, digits, _ or -, 2-32 chars")
	}
	p := Project{ID: id, Account: account, Name: name, Created: now()}
	if _, err := t.db.Exec(`INSERT INTO projects(id, account, name, created) VALUES ($1, $2, $3, $4)`, p.ID, p.Account, p.Name, p.Created); err != nil {
		return Project{}, errors.New("project exists")
	}
	return p, nil
}

// DefaultProjectOf: the account's project named "default", else its first.
func (t *Tenancy) DefaultProjectOf(account string) (string, error) {
	ps, err := t.Projects(account)
	if err != nil {
		return "", err // the database failing is not "no such project"
	}
	if len(ps) == 0 {
		return "", ErrNotFound
	}
	for _, p := range ps {
		if p.Name == "default" {
			return p.ID, nil
		}
	}
	return ps[0].ID, nil
}

// ResolveProject: the project a request names (or the identity's default), checked against who asks.
func (t *Tenancy) ResolveProject(id Identity, named string) (string, error) {
	if id.Project != "" { // an API key reaches one project
		if named != "" && named != id.Project {
			return "", ErrForbidden
		}
		return id.Project, nil
	}
	if named == "" {
		return t.DefaultProjectOf(id.Account)
	}
	p, err := t.Project(named)
	if err != nil {
		return "", ErrNotFound
	}
	if !id.Admin() && p.Account != id.Account {
		return "", ErrForbidden
	}
	return p.ID, nil
}

// ---- members ----

func (t *Tenancy) Members(account string) ([]Member, error) {
	rows, err := t.db.Query(`SELECT id, account, usr, name, role, created FROM members WHERE account = $1 ORDER BY created`, account)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Member{}
	for rows.Next() {
		var m Member
		if err := rows.Scan(&m.ID, &m.Account, &m.User, &m.Name, &m.Role, &m.Created); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, nil
}

// MemberAdd creates a member and returns the token once; only its hash is kept. A user name is unique across accounts.
func (t *Tenancy) MemberAdd(account, user, name, role string) (map[string]any, error) {
	if !nameRe.MatchString(user) {
		return nil, errors.New("user: lowercase letters, digits, _ or -, 2-32 chars")
	}
	if user == Admin {
		return nil, errors.New("user exists")
	}
	if role == "" {
		role = RoleMember
	}
	if role != RoleOwner && role != RoleMember {
		return nil, errors.New("role: owner or member")
	}
	if _, err := t.Account(account); err != nil {
		return nil, ErrNotFound
	}
	if name == "" {
		name = user
	}
	tok := registry.NewToken() + registry.NewToken()[:16] // 48 hex chars
	m := Member{ID: "m-" + registry.NewToken()[:10], Account: account, User: user, Name: name, Role: role, Created: now()}
	if _, err := t.db.Exec(`INSERT INTO members(id, account, usr, name, role, token_sha256, created) VALUES ($1, $2, $3, $4, $5, $6, $7)`,
		m.ID, m.Account, m.User, m.Name, m.Role, sha(tok), m.Created); err != nil {
		return nil, errors.New("user exists")
	}
	return map[string]any{"id": m.ID, "account": account, "user": user, "name": name, "role": role, "token": tok}, nil
}

func (t *Tenancy) MemberDelete(account, user string) error {
	res, err := t.db.Exec(`DELETE FROM members WHERE account = $1 AND usr = $2`, account, user)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return nil
}

// ---- quotas ----

func (t *Tenancy) Quota(project string) (Quota, error) {
	q := Quota{Project: project}
	err := t.db.QueryRow(`SELECT max_sandboxes, max_storage_gib, daily_usd FROM quotas WHERE project = $1`, project).Scan(&q.MaxSandboxes, &q.MaxStorageGiB, &q.DailyUSD)
	if err == sql.ErrNoRows {
		return q, nil // no row = unlimited
	}
	return q, err
}

func (t *Tenancy) QuotaSet(project string, q Quota) (Quota, error) {
	if _, err := t.Project(project); err != nil {
		return q, ErrNotFound
	}
	if q.MaxSandboxes < 0 || q.MaxStorageGiB < 0 || q.DailyUSD < 0 {
		return q, errors.New("quota values are 0 (unlimited) or positive")
	}
	q.Project = project
	_, err := t.db.Exec(`INSERT INTO quotas(project, max_sandboxes, max_storage_gib, daily_usd, updated) VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT(project) DO UPDATE SET max_sandboxes = $2, max_storage_gib = $3, daily_usd = $4, updated = $5`, project, q.MaxSandboxes, q.MaxStorageGiB, q.DailyUSD, now())
	return q, err
}

// ---- the project's default network policy (docs/002 沙箱侧 6; D36 补): what a sandbox gets when its request says nothing about network ----

func (t *Tenancy) NetworkDefault(project string) (map[string]any, error) {
	if _, err := t.Project(project); err != nil {
		return nil, ErrNotFound
	}
	var body string
	err := t.db.QueryRow(`SELECT body FROM project_network WHERE project = $1`, project).Scan(&body)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	out := map[string]any{}
	if err := json.Unmarshal([]byte(body), &out); err != nil {
		return nil, err
	}
	return out, nil
}

// NetworkDefaultSet stores the policy (already validated by the caller); nil or empty removes it.
func (t *Tenancy) NetworkDefaultSet(project string, net map[string]any) error {
	if _, err := t.Project(project); err != nil {
		return ErrNotFound
	}
	if len(net) == 0 {
		_, err := t.db.Exec(`DELETE FROM project_network WHERE project = $1`, project)
		return err
	}
	b, _ := json.Marshal(net)
	_, err := t.db.Exec(`INSERT INTO project_network(project, body, updated) VALUES ($1, $2, $3) ON CONFLICT(project) DO UPDATE SET body = $2, updated = $3`, project, string(b), now())
	return err
}

// NetworkDefaultOf satisfies lifecycle.Defaults.
func (t *Tenancy) NetworkDefaultOf(project string) (map[string]any, bool) {
	net, err := t.NetworkDefault(project)
	if err != nil || len(net) == 0 {
		return nil, false
	}
	return net, true
}

// ---- the legacy /users surface (the workbench's settings page): members of the default account ----

func (t *Tenancy) UsersList() ([]map[string]any, error) {
	ms, err := t.Members(DefaultAccount)
	out := []map[string]any{}
	for _, m := range ms {
		out = append(out, map[string]any{"user": m.User, "name": m.Name, "role": m.Role, "created": m.Created})
	}
	return out, err
}

func (t *Tenancy) UserAdd(user, name string) (map[string]any, error) {
	r, err := t.MemberAdd(DefaultAccount, user, name, RoleMember)
	if err != nil {
		return nil, err
	}
	return map[string]any{"user": r["user"], "name": r["name"], "token": r["token"]}, nil
}

func (t *Tenancy) UserDelete(user string) error { return t.MemberDelete(DefaultAccount, user) }

// Describe is what /me says about an identity.
func (t *Tenancy) Describe(id Identity) map[string]any {
	out := map[string]any{"user": id.User, "admin": id.Admin(), "auth": t.AuthEnabled(), "account": id.Account, "role": id.Role}
	if sp, ok := t.SuspensionOf(id.Account); ok && !id.Admin() {
		out["suspended"] = map[string]any{"reason": sp.Reason, "at": sp.At}
	}
	if id.Project != "" {
		out["projects"] = []string{id.Project}
		out["key"] = true
	} else if ps, err := t.Projects(id.Account); err == nil {
		ids := []string{}
		for _, p := range ps {
			ids = append(ids, p.ID)
		}
		out["projects"] = ids
	}
	return out
}

func (q Quota) String() string {
	return fmt.Sprintf("sandboxes=%d storage=%.0fGiB daily=$%.2f", q.MaxSandboxes, q.MaxStorageGiB, q.DailyUSD)
}

var _ = strings.TrimSpace

// QuotaOf satisfies lifecycle.Quotas.
func (t *Tenancy) QuotaOf(project string) (struct {
	MaxSandboxes  int64
	DailyUSD      float64
	MaxStorageGiB float64
	Account       string
	Prepaid       bool
}, error) {
	var out struct {
		MaxSandboxes  int64
		DailyUSD      float64
		MaxStorageGiB float64
		Account       string
		Prepaid       bool
	}
	p, err := t.Project(project)
	if err != nil {
		return out, err
	}
	out.Account, out.Prepaid = p.Account, t.BillingMode(p.Account) == Prepaid
	q, err := t.Quota(project)
	out.MaxSandboxes, out.DailyUSD, out.MaxStorageGiB = q.MaxSandboxes, q.DailyUSD, q.MaxStorageGiB
	return out, err
}

// Rotate issues a member a new token (the old one stops working at once) with an optional lifetime; the token is returned once.
func (t *Tenancy) Rotate(account, user string, ttlS float64) (map[string]any, error) {
	tok := registry.NewToken() + registry.NewToken()[:16]
	exp := 0.0
	if ttlS > 0 {
		exp = now() + ttlS
	}
	res, err := t.db.Exec(`UPDATE members SET token_sha256 = $1, expires = $2 WHERE account = $3 AND usr = $4`, sha(tok), exp, account, user)
	if err != nil {
		return nil, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return nil, ErrNotFound
	}
	return map[string]any{"account": account, "user": user, "token": tok, "expires": exp}, nil
}

// SetExpiry puts a lifetime on an existing token (0 = none) without changing it.
func (t *Tenancy) SetExpiry(account, user string, ttlS float64) error {
	exp := 0.0
	if ttlS > 0 {
		exp = now() + ttlS
	}
	res, err := t.db.Exec(`UPDATE members SET expires = $1 WHERE account = $2 AND usr = $3`, exp, account, user)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return nil
}
