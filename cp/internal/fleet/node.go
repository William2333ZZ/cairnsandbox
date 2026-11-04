// Package fleet is context 3 (docs/002 沙箱侧的领域 节点与容量): the nodes this replica talks to, their health and capacity, placement, leases.
// It does not know routes: lifecycle registers callbacks for "a node came up" and "a node went down".
package fleet

import (
	"encoding/json"
	"sort"
	"sync"
	"time"

	"fcnode/cp/internal/registry"
)

// Spec is how a node was registered (env NODES or POST /nodes). Stored in the registry so every replica sees it.
type Spec = registry.NodeSpec

// Node is one node agent as this replica sees it. Health fields are written by the poll loop and read by every request: all access goes through the lock.
type Node struct {
	Name                      string
	URL                       string
	TokenFile                 string
	Added                     float64
	ReserveVMs, ReserveMemMiB float64 // this node's headroom for interactive work (0 = fleet default); fixed at registration
	mu                        sync.RWMutex
	token                     string
	cordoned                  bool
	alive                     bool
	health                    map[string]any
	lastOK                    float64
	lastErr                   *string
	polls                     int
}

func (n *Node) MarshalJSON() ([]byte, error) {
	n.mu.RLock()
	defer n.mu.RUnlock()
	return json.Marshal(map[string]any{"name": n.Name, "url": n.URL, "token_file": n.TokenFile, "alive": n.alive, "cordoned": n.cordoned, "health": n.health, "last_ok": n.lastOK, "last_err": n.lastErr, "added": n.Added,
		"reserve_vms": n.reserveVMs(), "reserve_mem_mib": n.reserveMem(), "load": loadOf(n.health)})
}

func (n *Node) Alive() bool {
	n.mu.RLock()
	defer n.mu.RUnlock()
	return n.alive
}

func (n *Node) Cordoned() bool {
	n.mu.RLock()
	defer n.mu.RUnlock()
	return n.cordoned
}

func (n *Node) setCordoned(v bool) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.cordoned = v
}

func (n *Node) LastErr() *string {
	n.mu.RLock()
	defer n.mu.RUnlock()
	return n.lastErr
}

// Token returns the bearer, reading the token file again when it is still empty (a file another host writes later).
func (n *Node) Token() string {
	n.mu.RLock()
	t := n.token
	n.mu.RUnlock()
	if t != "" || n.TokenFile == "" {
		return t
	}
	if b, err := readFile(n.TokenFile); err == nil {
		n.mu.Lock()
		n.token = trim(b)
		t = n.token
		n.mu.Unlock()
	}
	return t
}

// Health is a shallow copy of the last health report, or nil before the first.
func (n *Node) Health() map[string]any {
	n.mu.RLock()
	defer n.mu.RUnlock()
	if n.health == nil {
		return nil
	}
	out := make(map[string]any, len(n.health))
	for k, v := range n.health {
		out[k] = v
	}
	return out
}

func (n *Node) SetUp(h map[string]any) (wasAlive bool, polls int) {
	n.mu.Lock()
	defer n.mu.Unlock()
	wasAlive = n.alive
	n.alive, n.health, n.lastOK, n.lastErr = true, h, now(), nil
	n.polls++
	return wasAlive, n.polls
}

// DownFor: seconds since the node last answered a poll; 0 when it is alive or has never been seen up (a node that never came up has nothing to take over).
func (n *Node) DownFor() float64 {
	n.mu.RLock()
	defer n.mu.RUnlock()
	if n.alive || n.lastOK == 0 {
		return 0
	}
	return now() - n.lastOK
}

// LastOK: when the node last answered (unix seconds; 0 = never). Identifies one outage: a node that comes back and goes down again has a new LastOK.
func (n *Node) LastOK() float64 {
	n.mu.RLock()
	defer n.mu.RUnlock()
	return n.lastOK
}

func (n *Node) SetDown(msg string) (wasAlive bool) {
	n.mu.Lock()
	defer n.mu.Unlock()
	wasAlive = n.alive
	n.alive, n.lastErr = false, &msg
	return wasAlive
}

func (n *Node) Num(key string) float64 {
	n.mu.RLock()
	defer n.mu.RUnlock()
	return NumOf(n.health, key)
}

func NumOf(h map[string]any, key string) float64 {
	if h == nil {
		return 0
	}
	switch v := h[key].(type) {
	case float64:
		return v
	case int:
		return float64(v)
	}
	return 0
}

// SetNum / AddNum adjust the counts between polls (feedback from a create, a delete, or a node's own 429).
func (n *Node) SetNum(key string, v float64) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.health != nil {
		n.health[key] = v
	}
}

func (n *Node) AddNum(key string, d float64) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.health != nil {
		v := NumOf(n.health, key) + d
		if v < 0 {
			v = 0
		}
		n.health[key] = v
	}
}

// Fleet is this replica's view of the nodes plus the shared registration and leases.
type Fleet struct {
	store  Store
	audit  Auditor
	mu     sync.RWMutex
	byName map[string]*Node
	onUp   []func(*Node, bool, int)
}

// Store is the slice of the registry fleet owns: registered nodes and leases.
type Store interface {
	Nodes() ([]Spec, error)
	NodePut(Spec) error
	Acquire(name string, ttl time.Duration) (string, error)
	Release(name, token string) error
}

type Auditor interface{ Write(map[string]any) }

func New(store Store, audit Auditor) *Fleet {
	return &Fleet{store: store, audit: audit, byName: map[string]*Node{}}
}

// OnUp registers a callback for every successful poll: (node, wasAlive, polls). Lifecycle hangs its reconciliation here.
func (f *Fleet) OnUp(cb func(*Node, bool, int)) { f.onUp = append(f.onUp, cb) }

func (f *Fleet) Get(name string) *Node {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.byName[name]
}

func (f *Fleet) All() []*Node {
	f.mu.RLock()
	defer f.mu.RUnlock()
	out := make([]*Node, 0, len(f.byName))
	for _, n := range f.byName {
		out = append(out, n)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func (f *Fleet) Names() []string {
	out := []string{}
	for _, n := range f.All() {
		out = append(out, n.Name)
	}
	return out
}

func (f *Fleet) Alive() []*Node {
	out := []*Node{}
	for _, n := range f.All() {
		if n.Alive() {
			out = append(out, n)
		}
	}
	return out
}

// Add registers a node with this replica; register=true (POST /nodes) also writes the spec to the registry for the other replicas.
func (f *Fleet) Add(spec Spec, register bool) {
	url := trimRight(spec.URL, "/")
	f.mu.Lock()
	if cur, ok := f.byName[spec.Name]; ok && cur.URL == url {
		f.mu.Unlock()
		cur.setCordoned(spec.Cordoned) // the shared flag may have changed on another replica
		return
	}
	n := &Node{Name: spec.Name, URL: url, TokenFile: spec.TokenFile, Added: now(), token: spec.Token, cordoned: spec.Cordoned, ReserveVMs: spec.ReserveVMs, ReserveMemMiB: spec.ReserveMemMiB}
	f.byName[spec.Name] = n
	f.mu.Unlock()
	n.Token()
	if register {
		if err := f.store.NodePut(spec); err != nil {
			f.audit.Write(map[string]any{"op": "registry_error", "where": "node_put", "error": err.Error()})
		}
	}
	f.audit.Write(map[string]any{"op": "node_add", "node": spec.Name, "url": url})
}

// Cordon marks a node as taking no new sandboxes (or lifts it), for every replica.
func (f *Fleet) Cordon(name string, on bool) error {
	n := f.Get(name)
	if n == nil {
		return errNoNode
	}
	n.setCordoned(on)
	spec := Spec{Name: n.Name, URL: n.URL, TokenFile: n.TokenFile, Cordoned: on, ReserveVMs: n.ReserveVMs, ReserveMemMiB: n.ReserveMemMiB}
	if n.TokenFile == "" {
		spec.Token = n.Token()
	}
	if err := f.store.NodePut(spec); err != nil {
		return err
	}
	op := "uncordon"
	if on {
		op = "cordon"
	}
	f.audit.Write(map[string]any{"op": op, "node": name})
	return nil
}

// Sync picks up nodes another replica registered.
func (f *Fleet) Sync() {
	specs, err := f.store.Nodes()
	if err != nil {
		f.audit.Write(map[string]any{"op": "registry_error", "where": "nodes", "error": err.Error()})
		return
	}
	for _, sp := range specs {
		f.Add(sp, false)
	}
}

// Lease runs fn only when no other replica holds `name`; returns false when it did not run.
func (f *Fleet) Lease(name string, ttl time.Duration, fn func()) bool {
	tok, err := f.store.Acquire(name, ttl)
	if err != nil {
		f.audit.Write(map[string]any{"op": "registry_error", "where": "lease", "error": err.Error()})
		return false
	}
	if tok == "" {
		return false
	}
	defer f.store.Release(name, tok)
	fn()
	return true
}

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

var errNoNode = &noNode{}

type noNode struct{}

func (*noNode) Error() string { return "no such node" }
