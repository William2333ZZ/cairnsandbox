// Package registry is the control plane's shared store (docs/003 控制面, D31): routes (sandbox id -> node), registered nodes, users and leases.
// It is infrastructure, not a context: lifecycle owns routes, fleet owns nodes and leases, tenancy owns users (docs/002 沙箱侧的领域);
// each of them names the slice it needs as its own interface, and Registry satisfies all of them.
// Two backends behind one interface: files under CP_STATE for one replica, Redis (CP_REGISTRY=redis://host:port[/db]) for any number.
// Health polling, audit and between-poll feedback stay per replica; reconcile and evacuate take a lease so one replica does them at a time.
// The node agents remain the authority on what runs: everything here can be rebuilt from them.
package registry

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

// ErrNotFound: the thing asked for does not exist, as opposed to the store being unreachable. tenancy.ErrNotFound is this same value.
var ErrNotFound = errors.New("not found")

// Route is what the control plane must agree on across replicas about one sandbox.
type Route struct {
	Node     string            `json:"node"`
	Class    string            `json:"class,omitempty"`
	Durable  bool              `json:"durable"`
	Created  float64           `json:"created,omitempty"`
	Project  string            `json:"project,omitempty"`  // tenancy: whose sandbox (empty on routes adopted from a node = the default project)
	Meta     map[string]string `json:"meta,omitempty"`     // the caller's labels (E2B metadata); the node never sees them
	DiskMiB  float64           `json:"disk_mib,omitempty"` // the writable layer's allocated size: what the storage quota counts (tenancy)
	Priority string            `json:"priority,omitempty"` // fleet: interactive|batch|background; what an interactive request may preempt (pause) when no node has room
}

// NodeSpec is how a node was registered (env NODES or POST /nodes).
type NodeSpec struct {
	Name      string `json:"name"`
	URL       string `json:"url"`
	Token     string `json:"token,omitempty"`
	TokenFile string `json:"token_file,omitempty"`
	Cordoned  bool   `json:"cordoned,omitempty"` // fleet: takes no new sandboxes (drain, maintenance); shared so every replica places alike
	// fleet: what batch / background work must leave free on this node (0 = the fleet-wide default); a node that serves people gets more headroom.
	ReserveVMs    float64 `json:"reserve_vms,omitempty"`
	ReserveMemMiB float64 `json:"reserve_mem_mib,omitempty"`
}

// User is a workbench user (docs/009): the token is kept hashed.
type User struct {
	User        string  `json:"user"`
	Name        string  `json:"name"`
	TokenSHA256 string  `json:"token_sha256"`
	Created     float64 `json:"created"`
}

type Registry interface {
	Kind() string
	Where() string
	Ping() error
	RouteGet(sid string) (*Route, error)
	RouteSet(sid string, r Route) error
	RouteDel(sid string) error
	Routes() (map[string]Route, error)
	Users() ([]User, error)
	UsersSave([]User) error
	Nodes() ([]NodeSpec, error)
	NodePut(NodeSpec) error
	// Acquire returns a lease token, or "" when another holder has it.
	Acquire(name string, ttl time.Duration) (string, error)
	Renew(name, token string, ttl time.Duration) (bool, error)
	Release(name, token string) error
}

func Open(spec, stateDir string) (Registry, error) {
	if spec == "" {
		return NewFileRegistry(stateDir)
	}
	if strings.HasPrefix(spec, "redis://") {
		u, err := url.Parse(spec)
		if err != nil {
			return nil, err
		}
		db := 0
		if p := strings.Trim(u.Path, "/"); p != "" {
			if db, err = strconv.Atoi(p); err != nil {
				return nil, fmt.Errorf("CP_REGISTRY: bad db %q", p)
			}
		}
		host := u.Hostname()
		port := u.Port()
		if port == "" {
			port = "6379"
		}
		prefix := os.Getenv("CP_REGISTRY_PREFIX")
		if prefix == "" {
			prefix = "cp:"
		}
		return NewRedisRegistry(host+":"+port, db, prefix), nil
	}
	return nil, fmt.Errorf("CP_REGISTRY: unsupported %q (empty or redis://host:port[/db])", spec)
}

// ---------------- files: one replica ----------------

type FileRegistry struct {
	dir    string
	mu     sync.Mutex
	routes map[string]Route
	leases map[string]fileLease
}

type fileLease struct {
	token string
	until time.Time
}

func NewFileRegistry(dir string) (*FileRegistry, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	f := &FileRegistry{dir: dir, routes: map[string]Route{}, leases: map[string]fileLease{}}
	if b, err := os.ReadFile(filepath.Join(dir, "routes.json")); err == nil {
		_ = json.Unmarshal(b, &f.routes) // a corrupt file is an empty table; the nodes refill it on reconcile
	}
	return f, nil
}

func (f *FileRegistry) Kind() string  { return "file" }
func (f *FileRegistry) Where() string { return f.dir }
func (f *FileRegistry) Ping() error   { return nil }

func (f *FileRegistry) save(name string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	tmp := filepath.Join(f.dir, name+".tmp")
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(f.dir, name))
}

func (f *FileRegistry) RouteGet(sid string) (*Route, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r, ok := f.routes[sid]
	if !ok {
		return nil, nil
	}
	return &r, nil
}

func (f *FileRegistry) RouteSet(sid string, r Route) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.routes[sid] = r
	return f.save("routes.json", f.routes)
}

func (f *FileRegistry) RouteDel(sid string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.routes[sid]; !ok {
		return nil
	}
	delete(f.routes, sid)
	return f.save("routes.json", f.routes)
}

func (f *FileRegistry) Routes() (map[string]Route, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make(map[string]Route, len(f.routes))
	for k, v := range f.routes {
		out[k] = v
	}
	return out, nil
}

func (f *FileRegistry) Users() ([]User, error) {
	var doc struct {
		Users []User `json:"users"`
	}
	b, err := os.ReadFile(filepath.Join(f.dir, "users.json"))
	if err != nil {
		return nil, nil
	}
	_ = json.Unmarshal(b, &doc)
	return doc.Users, nil
}

func (f *FileRegistry) UsersSave(us []User) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if us == nil {
		us = []User{}
	}
	return f.save("users.json", map[string]any{"users": us})
}

func (f *FileRegistry) Nodes() ([]NodeSpec, error) {
	var doc struct {
		Nodes []NodeSpec `json:"nodes"`
	}
	b, err := os.ReadFile(filepath.Join(f.dir, "nodes.json"))
	if err != nil {
		return nil, nil
	}
	_ = json.Unmarshal(b, &doc)
	return doc.Nodes, nil
}

func (f *FileRegistry) NodePut(n NodeSpec) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	cur, _ := f.Nodes()
	out := []NodeSpec{}
	for _, c := range cur {
		if c.Name != n.Name {
			out = append(out, c)
		}
	}
	out = append(out, n)
	return f.save("nodes.json", map[string]any{"nodes": out})
}

func (f *FileRegistry) Acquire(name string, ttl time.Duration) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if l, ok := f.leases[name]; ok && l.until.After(time.Now()) {
		return "", nil
	}
	tok := NewToken()
	f.leases[name] = fileLease{tok, time.Now().Add(ttl)}
	return tok, nil
}

func (f *FileRegistry) Renew(name, token string, ttl time.Duration) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	l, ok := f.leases[name]
	if !ok || l.token != token || !l.until.After(time.Now()) {
		return false, nil
	}
	f.leases[name] = fileLease{token, time.Now().Add(ttl)}
	return true, nil
}

func (f *FileRegistry) Release(name, token string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if l, ok := f.leases[name]; ok && l.token == token {
		delete(f.leases, name)
	}
	return nil
}

// ---------------- Redis: any number of replicas ----------------

const leaseRenewLua = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
const leaseDropLua = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

type RedisRegistry struct {
	c      *redis.Client
	prefix string
	where  string
}

func NewRedisRegistry(addr string, db int, prefix string) *RedisRegistry {
	c := redis.NewClient(&redis.Options{Addr: addr, DB: db, DialTimeout: 5 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second, MaxRetries: 2})
	return &RedisRegistry{c: c, prefix: prefix, where: fmt.Sprintf("redis://%s/%d", addr, db)}
}

func (r *RedisRegistry) Kind() string         { return "redis" }
func (r *RedisRegistry) Where() string        { return r.where }
func (r *RedisRegistry) k(p ...string) string { return r.prefix + strings.Join(p, ":") }
func (r *RedisRegistry) ctx() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), 5*time.Second)
}

func (r *RedisRegistry) Ping() error {
	ctx, cancel := r.ctx()
	defer cancel()
	return r.c.Ping(ctx).Err()
}

func (r *RedisRegistry) RouteGet(sid string) (*Route, error) {
	ctx, cancel := r.ctx()
	defer cancel()
	v, err := r.c.HGet(ctx, r.k("routes"), sid).Bytes()
	if err == redis.Nil {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var rt Route
	if err := json.Unmarshal(v, &rt); err != nil {
		return nil, err
	}
	return &rt, nil
}

func (r *RedisRegistry) RouteSet(sid string, rt Route) error {
	ctx, cancel := r.ctx()
	defer cancel()
	b, _ := json.Marshal(rt)
	return r.c.HSet(ctx, r.k("routes"), sid, b).Err()
}

func (r *RedisRegistry) RouteDel(sid string) error {
	ctx, cancel := r.ctx()
	defer cancel()
	return r.c.HDel(ctx, r.k("routes"), sid).Err()
}

func (r *RedisRegistry) Routes() (map[string]Route, error) {
	ctx, cancel := r.ctx()
	defer cancel()
	flat, err := r.c.HGetAll(ctx, r.k("routes")).Result()
	if err != nil {
		return nil, err
	}
	out := make(map[string]Route, len(flat))
	for k, v := range flat {
		var rt Route
		if err := json.Unmarshal([]byte(v), &rt); err == nil {
			out[k] = rt
		}
	}
	return out, nil
}

func (r *RedisRegistry) Users() ([]User, error) {
	ctx, cancel := r.ctx()
	defer cancel()
	v, err := r.c.Get(ctx, r.k("users")).Bytes()
	if err == redis.Nil {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var doc struct {
		Users []User `json:"users"`
	}
	_ = json.Unmarshal(v, &doc)
	return doc.Users, nil
}

func (r *RedisRegistry) UsersSave(us []User) error {
	ctx, cancel := r.ctx()
	defer cancel()
	if us == nil {
		us = []User{}
	}
	b, _ := json.Marshal(map[string]any{"users": us})
	return r.c.Set(ctx, r.k("users"), b, 0).Err()
}

func (r *RedisRegistry) Nodes() ([]NodeSpec, error) {
	ctx, cancel := r.ctx()
	defer cancel()
	flat, err := r.c.HGetAll(ctx, r.k("nodes")).Result()
	if err != nil {
		return nil, err
	}
	out := []NodeSpec{}
	for _, v := range flat {
		var n NodeSpec
		if err := json.Unmarshal([]byte(v), &n); err == nil {
			out = append(out, n)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

func (r *RedisRegistry) NodePut(n NodeSpec) error {
	ctx, cancel := r.ctx()
	defer cancel()
	b, _ := json.Marshal(n)
	return r.c.HSet(ctx, r.k("nodes"), n.Name, b).Err()
}

func (r *RedisRegistry) Acquire(name string, ttl time.Duration) (string, error) {
	ctx, cancel := r.ctx()
	defer cancel()
	tok := NewToken()
	ok, err := r.c.SetNX(ctx, r.k("lease", name), tok, ttl).Result()
	if err != nil {
		return "", err
	}
	if !ok {
		return "", nil
	}
	return tok, nil
}

func (r *RedisRegistry) Renew(name, token string, ttl time.Duration) (bool, error) {
	ctx, cancel := r.ctx()
	defer cancel()
	n, err := r.c.Eval(ctx, leaseRenewLua, []string{r.k("lease", name)}, token, ttl.Milliseconds()).Int64()
	if err != nil {
		return false, err
	}
	return n == 1, nil
}

func (r *RedisRegistry) Release(name, token string) error {
	ctx, cancel := r.ctx()
	defer cancel()
	return r.c.Eval(ctx, leaseDropLua, []string{r.k("lease", name)}, token).Err()
}

func NewToken() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
