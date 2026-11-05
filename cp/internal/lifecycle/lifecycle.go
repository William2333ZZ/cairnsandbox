// Package lifecycle is context 2 (docs/002 沙箱侧的领域 沙箱生命周期): where each sandbox is (routes), creating with placement, migrating,
// re-homing a dead node's sandboxes, and reconciling routes against what nodes actually hold. Nodes stay the authority on state.
package lifecycle

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/nodeclient"
	"fcnode/cp/internal/registry"
	"fcnode/cp/internal/state"
)

type Route = registry.Route

// Routes is the slice of the registry lifecycle owns.
type Routes interface {
	RouteGet(sid string) (*Route, error)
	RouteSet(sid string, r Route) error
	RouteDel(sid string) error
	Routes() (map[string]Route, error)
}

type Auditor interface{ Write(map[string]any) }

// Quota is what lifecycle asks tenancy before creating: 0 = unlimited. An anonymous struct so tenancy can return it without importing this package.
type Quota = struct {
	MaxSandboxes  int64
	DailyUSD      float64
	MaxStorageGiB float64
	Account       string
	Prepaid       bool
}

// VolumeStorage: MiB of volumes a project holds (state), for the storage quota.
type VolumeStorage interface{ VolumeMiB(project string) float64 }

// Quotas and Spend are the slices of tenancy and metering that creation needs (consumer-side interfaces: no import either way).
type Quotas interface {
	QuotaOf(project string) (Quota, error)
}
type Spend interface{ SpentToday(project string) float64 }

// Credit is the prepaid balance of an account (metering): creation is refused at zero (D59).
type Credit interface{ BalanceUSD(account string) float64 }

// VolumeMover carries a sandbox's volumes along with a migration (state.Volumes).
type VolumeMover interface {
	Move(id string, from, to *fleet.Node, onlyFor string) (int, map[string]any)
	DropAt(id string, n *fleet.Node)
}

// Defaults answers what a project's sandboxes get when the request does not say (today: the network policy; tenancy keeps it).
type Defaults interface {
	NetworkDefaultOf(project string) (map[string]any, bool)
}

type Lifecycle struct {
	routes   Routes
	fleet    *fleet.Fleet
	nc       nodeclient.Client
	store    state.Store
	audit    Auditor
	quotas   Quotas
	spend    Spend
	credit   Credit
	vols     VolumeStorage
	defaults Defaults
	mover    VolumeMover
}

func New(routes Routes, f *fleet.Fleet, st state.Store, audit Auditor) *Lifecycle {
	return &Lifecycle{routes: routes, fleet: f, store: st, audit: audit}
}

// WithQuotas turns on quota checks at creation (tenancy for the limits, metering for today's spend, state for the volumes' size).
func (l *Lifecycle) WithQuotas(q Quotas, s Spend, v VolumeStorage) *Lifecycle {
	l.quotas, l.spend, l.vols = q, s, v
	return l
}

// WithCredit turns on the prepaid check at creation.
func (l *Lifecycle) WithCredit(c Credit) *Lifecycle {
	l.credit = c
	return l
}

// WithDefaults: a request without a network policy gets the project's default one (D36 补).
func (l *Lifecycle) WithDefaults(d Defaults) *Lifecycle {
	l.defaults = d
	return l
}

// WithVolumeMover: migrations take the sandbox's volumes along (D35 补).
func (l *Lifecycle) WithVolumeMover(m VolumeMover) *Lifecycle {
	l.mover = m
	return l
}

// Count: how many sandboxes a project has routes for, and the MiB of writable layers they hold.
func (l *Lifecycle) Count(project string) (n int, diskMiB float64) {
	for _, r := range l.Routes() {
		if r.Project == project || (r.Project == "" && project == "default") {
			n++
			diskMiB += r.DiskMiB
		}
	}
	return n, diskMiB
}

// admit: the project's quota, before a node is asked. 429 quota: sandboxes / storage; 402 quota: daily budget.
func (l *Lifecycle) admit(project string, diskMiB float64) *Result {
	if l.quotas == nil {
		return nil
	}
	q, err := l.quotas.QuotaOf(project)
	if err != nil {
		if errors.Is(err, registry.ErrNotFound) {
			return &Result{404, map[string]any{"error": "no project " + project}}
		}
		return &Result{503, map[string]any{"error": "tenancy store unavailable: " + err.Error()}} // the database is down or slow: say so, do not send the operator looking for a project
	}
	if q.Prepaid && l.credit != nil {
		if bal := l.credit.BalanceUSD(q.Account); bal <= 0 {
			return &Result{402, map[string]any{"error": "quota: out of credit", "account": q.Account, "balance_usd": bal}}
		}
	}
	n, used := l.Count(project)
	if q.MaxSandboxes > 0 && int64(n) >= q.MaxSandboxes {
		return &Result{429, map[string]any{"error": "quota: sandboxes", "project": project, "max_sandboxes": q.MaxSandboxes}}
	}
	if q.MaxStorageGiB > 0 {
		if l.vols != nil {
			used += l.vols.VolumeMiB(project)
		}
		if (used+diskMiB)/1024 > q.MaxStorageGiB {
			return &Result{429, map[string]any{"error": "quota: storage", "project": project, "max_storage_gib": q.MaxStorageGiB, "used_mib": used, "asked_mib": diskMiB}}
		}
	}
	if q.DailyUSD > 0 && l.spend != nil {
		if spent := l.spend.SpentToday(project); spent >= q.DailyUSD {
			return &Result{402, map[string]any{"error": "quota: daily budget", "project": project, "daily_usd": q.DailyUSD, "spent_usd": spent}}
		}
	}
	return nil
}

// WatchFleet reconciles a node when it comes (back) up and once a minute after that (clones made through the forward). Asynchronous: main calls it, tests drive Reconcile themselves.
func (l *Lifecycle) WatchFleet() {
	l.fleet.OnUp(func(n *fleet.Node, wasAlive bool, polls int) {
		if !wasAlive || polls%30 == 0 {
			go l.Reconcile(n)
		}
	})
}

// AutoEvacuate (D54, opt-in): a node that has been silent for `after` is evacuated once per outage. `after` must exceed the nodes' FENCE_AFTER_S:
// the control plane cannot power a node off, it relies on the node stopping its own sandboxes first, otherwise a cut network would run a sandbox twice.
func (l *Lifecycle) AutoEvacuate(after time.Duration, every time.Duration) {
	done := map[string]float64{} // node -> LastOK of the outage already handled
	for {
		l.AutoEvacuateOnce(after, done)
		time.Sleep(every)
	}
}

func (l *Lifecycle) AutoEvacuateOnce(after time.Duration, done map[string]float64) {
	for _, n := range l.fleet.All() {
		if n.Alive() || n.DownFor() < after.Seconds() || done[n.Name] == n.LastOK() {
			continue
		}
		done[n.Name] = n.LastOK()
		res := l.Evacuate(n.Name)
		l.audit.Write(map[string]any{"op": "auto_evacuate", "node": n.Name, "down_s": int(n.DownFor()), "code": res.Code, "result": res.Body})
	}
}

func (l *Lifecycle) Routes() map[string]Route {
	r, _ := l.routes.Routes()
	return r
}

func (l *Lifecycle) Route(sid string) *Route {
	r, _ := l.routes.RouteGet(sid)
	return r
}

// RouteErr is Route that tells "no such sandbox" (nil, nil) from "the registry cannot be reached" (nil, err): the first is a 404, the second a 503.
func (l *Lifecycle) RouteErr(sid string) (*Route, error) { return l.routes.RouteGet(sid) }

func (l *Lifecycle) SetRoute(sid string, r Route) { l.routes.RouteSet(sid, r) }

// Forget drops a route after a confirmed delete and adjusts the node's counts until its next poll.
func (l *Lifecycle) Forget(sid string, n *fleet.Node) {
	l.routes.RouteDel(sid)
	if n != nil {
		n.AddNum("vms_total", -1)
		n.AddNum("vms_running", -1)
	}
}

type Result struct {
	Code int
	Body any
}

// Create: admit against the project's quota, place, ask the node, record the route; a node's own 429 marks it full and tries another.
func (l *Lifecycle) Create(body map[string]any, project string) Result {
	if project == "" {
		project = "default"
	}
	diskMiB := 512.0
	if d, ok := body["disk"].(float64); ok && d > 0 {
		diskMiB = d
	}
	if res := l.admit(project, diskMiB); res != nil {
		l.audit.Write(map[string]any{"op": "create", "status": res.Code, "project": project, "reason": res.Body.(map[string]any)["error"]})
		return *res
	}
	spec := map[string]any{}
	for k, v := range body {
		spec[k] = v
	}
	startCmd, _ := spec["start_cmd"].(string) // D71: ours, not the node's: started once after the sandbox exists
	delete(spec, "start_cmd")
	if _, set := spec["network"]; !set && l.defaults != nil {
		if net, ok := l.defaults.NetworkDefaultOf(project); ok {
			spec["network"] = net
		}
	}
	nodeName, _ := spec["node"].(string)
	key, _ := spec["template_key"].(string)
	prio, _ := spec["priority"].(string)
	switch prio {
	case "":
		prio = fleet.PriorityInteractive
	case fleet.PriorityInteractive, fleet.PriorityBatch, fleet.PriorityBackground:
	default:
		return Result{400, map[string]any{"error": "priority must be interactive|batch|background"}}
	}
	spec["priority"] = prio
	var meta map[string]string
	if md, ok := spec["metadata"].(map[string]string); ok {
		meta = md
	} else if md, ok := spec["metadata"].(map[string]any); ok {
		meta = map[string]string{}
		for k, v := range md {
			meta[k] = fmt.Sprint(v)
		}
	}
	delete(spec, "metadata")
	if _, set := spec["qos"]; !set { // the class picks the node's scheduling: a person waits on interactive work, the rest yields
		if prio == fleet.PriorityInteractive {
			spec["qos"] = "normal"
		} else {
			spec["qos"] = "be"
		}
	}
	delete(spec, "node")
	delete(spec, "project")
	cls, _ := spec["class"].(string)
	if (cls == "" || cls == "persistent") && l.store.Enabled() {
		if _, set := spec["durable"]; !set {
			spec["durable"] = true
		}
	}
	tried := map[string]bool{}
	attempts := max(1, len(l.fleet.All()))
	preempted := 0
	var noTemplate *Result // the last "no template" answer: if every node we try lacks the template, that is the honest error, not "no capacity"
	for i := 0; i < attempts; i++ {
		var n *fleet.Node
		if nodeName != "" {
			n = l.fleet.Get(nodeName)
		} else {
			n = l.fleet.Place(spec, key, tried)
		}
		if (n == nil || !n.Alive()) && nodeName == "" && prio == fleet.PriorityInteractive && preempted < 2 {
			// a person is waiting: first pause one background / batch sandbox (frees memory), then, if still no room, end one ephemeral one (frees a slot)
			if victim := l.preempt(spec, project, preempted == 1); victim != "" {
				preempted++
				attempts++
				continue
			}
			preempted = 2
		}
		if (n == nil || !n.Alive()) && noTemplate != nil {
			return *noTemplate
		}
		if n == nil || !n.Alive() {
			l.audit.Write(map[string]any{"op": "create", "status": 429, "reason": "no node with capacity", "tried": keys(tried)})
			alive := map[string]bool{}
			for _, x := range l.fleet.All() {
				alive[x.Name] = x.Alive()
			}
			return Result{429, map[string]any{"error": "no node with capacity", "nodes": alive}}
		}
		toNode := map[string]any{}
		for k, v := range spec {
			if k != "priority" {
				toNode[k] = v
			}
		}
		labels := map[string]any{"project": project, "priority": prio} // what a lost registry needs to be rebuilt from the nodes (Reconcile adopts from these)
		for mk, mv := range meta {
			labels["meta."+mk] = mv
		}
		toNode["labels"] = labels
		st, info, err := l.nc.JSON(n, "POST", "/sandboxes", toNode, 300*time.Second)
		if err != nil {
			l.audit.Write(map[string]any{"op": "create", "node": n.Name, "status": 502, "error": err.Error()})
			return Result{502, map[string]any{"error": "node unreachable: " + err.Error()}}
		}
		switch {
		case st == 201:
			id, _ := info["id"].(string)
			icls, _ := info["class"].(string)
			dur, _ := info["durable"].(bool)
			created, _ := info["created"].(float64)
			if d, ok := info["disk"].(float64); ok && d > 0 {
				diskMiB = d
			}
			if err := l.routes.RouteSet(id, Route{Node: n.Name, Class: icls, Durable: dur, Created: created, Project: project, Meta: meta, DiskMiB: diskMiB, Priority: prio}); err != nil {
				// the sandbox exists on the node: say so and carry on; the next reconcile adopts it from its labels (project, priority, metadata included)
				l.audit.Write(map[string]any{"op": "registry_error", "where": "route_set", "sandbox": id, "error": err.Error()})
			}
			info["node"] = n.Name
			info["project"] = project
			info["priority"] = prio
			if meta != nil {
				info["metadata"] = meta
			}
			if strings.TrimSpace(startCmd) != "" {
				info["start"] = l.startCommand(id, startCmd)
			}
			mem, _ := info["mem"].(float64)
			n.AddNum("vms_total", 1)
			n.AddNum("vms_running", 1)
			n.AddNum("mem_mib_running", mem) // feedback until the next poll
		case st == 400 && nodeName == "" && key != "" && strings.HasPrefix(fmt.Sprint(info["error"]), "no template"):
			// placement goes by the health it last polled, which can lag a commit by a poll: this node does not have the template, another may
			tried[n.Name] = true
			noTemplate = &Result{st, info}
			l.audit.Write(map[string]any{"op": "create_retry", "node": n.Name, "error": info["error"]})
			continue
		case st == 429 && nodeName == "":
			for _, k := range []string{"vms_total", "vms_running", "mem_mib_running"} {
				if v, ok := info[k].(float64); ok {
					n.SetNum(k, v)
				}
			}
			tried[n.Name] = true
			l.audit.Write(map[string]any{"op": "create_retry", "node": n.Name, "error": info["error"]})
			continue
		}
		l.audit.Write(map[string]any{"op": "create", "node": n.Name, "status": st, "sandbox": info["id"], "project": project, "boot_mode": info["boot_mode"], "error": info["error"]})
		return Result{st, info}
	}
	if noTemplate != nil {
		return *noTemplate
	}
	return Result{429, map[string]any{"error": "no node with capacity"}}
}

// List: what every alive node holds, plus routes to unreachable ones; `projects` non-nil keeps only those (an account's view).
func (l *Lifecycle) List(projects map[string]bool) []map[string]any {
	out := []map[string]any{}
	seen := map[string]bool{}
	routes := l.Routes()
	projectOf := func(sid string) string {
		if p := routes[sid].Project; p != "" {
			return p
		}
		return "default"
	}
	keep := func(sid string) bool { return projects == nil || projects[projectOf(sid)] }
	for _, n := range l.fleet.Alive() {
		st, lst, err := l.nc.JSON(n, "GET", "/sandboxes", nil, 10*time.Second)
		if err != nil || st != 200 {
			continue
		}
		if arr, ok := lst["sandboxes"].([]any); ok {
			for _, x := range arr {
				if sb, ok := x.(map[string]any); ok {
					id, _ := sb["id"].(string)
					if id != "" {
						seen[id] = true
					}
					if !keep(id) {
						continue
					}
					sb["node"] = n.Name
					sb["project"] = projectOf(id)
					if m := routes[id].Meta; m != nil {
						sb["metadata"] = m
					}
					out = append(out, sb)
				}
			}
		}
	}
	ids := []string{}
	for sid := range routes {
		if !seen[sid] && keep(sid) {
			ids = append(ids, sid)
		}
	}
	sort.Strings(ids)
	for _, sid := range ids {
		r := routes[sid]
		out = append(out, map[string]any{"id": sid, "node": r.Node, "status": "unreachable", "class": r.Class, "durable": r.Durable, "project": projectOf(sid)})
	}
	return out
}

// NodeCall: one JSON call to the owning node for /sandboxes/<sid><sub>; a successful DELETE of the sandbox itself drops the route.
func (l *Lifecycle) NodeCall(sid, method, sub string, body any) (int, map[string]any) {
	r := l.Route(sid)
	if r == nil {
		return 404, map[string]any{"error": "no sandbox " + sid}
	}
	n := l.fleet.Get(r.Node)
	if n == nil || !n.Alive() {
		return 503, map[string]any{"error": "node " + r.Node + " is down", "sandbox": sid}
	}
	st, out, err := l.nc.JSON(n, method, "/sandboxes/"+sid+sub, body, 300*time.Second)
	if err != nil {
		return 502, map[string]any{"error": "node unreachable: " + err.Error()}
	}
	if method == "DELETE" && sub == "" && st == 200 {
		l.Forget(sid, n)
	}
	return st, out
}

// NodeOf: the node a routed sandbox is on ("" when unknown).
func (l *Lifecycle) NodeOf(sid string) string {
	if r := l.Route(sid); r != nil {
		return r.Node
	}
	return ""
}

// ProjectOf: the project a routed sandbox belongs to ("" when unknown).
func (l *Lifecycle) ProjectOf(sid string) string {
	r := l.Route(sid)
	if r == nil {
		return ""
	}
	if r.Project == "" {
		return "default"
	}
	return r.Project
}

func (l *Lifecycle) Migrate(sid, to string) Result {
	r := l.Route(sid)
	if r == nil {
		return Result{404, map[string]any{"error": "no sandbox " + sid}}
	}
	src := l.fleet.Get(r.Node)
	if src == nil {
		return Result{404, map[string]any{"error": "no node " + r.Node}}
	}
	dst := l.fleet.Get(to)
	if dst == nil {
		dst = l.fleet.LeastLoaded(map[string]any{"mem": 512.0}, map[string]bool{r.Node: true})
	}
	if dst == nil {
		return Result{429, map[string]any{"error": "no target node"}}
	}
	t0 := time.Now()
	// volumes first: the target's import needs them present, and a volume shared with another sandbox cannot leave (409 before anything moved)
	vols := []string{}
	if st, info, err := l.nc.JSON(src, "GET", "/sandboxes/"+sid, nil, 30*time.Second); err == nil && st == 200 {
		if arr, ok := info["volumes"].([]any); ok {
			for _, x := range arr {
				if vm, ok := x.(map[string]any); ok {
					if id, _ := vm["id"].(string); id != "" {
						vols = append(vols, id)
					}
				}
			}
		}
	}
	if len(vols) > 0 && l.mover == nil {
		return Result{409, map[string]any{"error": "sandbox has volumes and this replica cannot move them"}}
	}
	moved := []string{}
	for _, id := range vols {
		if st, body := l.mover.Move(id, src, dst, sid); st != 200 {
			for _, m := range moved { // undo: the copies on the target are dropped, the records go back
				l.mover.DropAt(m, dst)
				l.mover.(interface{ SetNode(string, string) }).SetNode(m, src.Name)
			}
			return Result{st, body}
		}
		moved = append(moved, id)
	}
	st, ex, err := l.nc.JSON(src, "POST", "/sandboxes/"+sid+"/export", nil, 600*time.Second)
	if err != nil || st != 200 {
		return Result{nz(st, 502), map[string]any{"error": "export failed", "detail": ex}}
	}
	tExp := time.Since(t0).Seconds()
	st, im, err := l.nc.JSON(dst, "POST", "/sandboxes/import", map[string]any{"bundle": ex["name"]}, 600*time.Second)
	if err != nil || st != 201 {
		return Result{nz(st, 502), map[string]any{"error": "import failed", "detail": im}}
	}
	tImp := time.Since(t0).Seconds() - tExp
	l.nc.JSON(src, "DELETE", "/sandboxes/"+sid, nil, 120*time.Second)
	for _, id := range moved { // the source copies go once the sandbox that held them is gone there
		l.mover.DropAt(id, src)
	}
	l.routes.RouteSet(sid, Route{Node: dst.Name, Class: r.Class, Durable: r.Durable, Created: r.Created, Project: r.Project, Meta: r.Meta, DiskMiB: r.DiskMiB, Priority: r.Priority})
	out := map[string]any{"sandbox": sid, "from": src.Name, "to": dst.Name, "bundle": ex["name"], "export_s": Round3(tExp), "sent_mib": ex["sent_mib"],
		"import_s": Round3(tImp), "fetched_mib": fetched(im), "total_s": Round3(time.Since(t0).Seconds()), "volumes": moved}
	ev := map[string]any{"op": "migrate"}
	for k, v := range out {
		ev[k] = v
	}
	l.audit.Write(ev)
	return Result{200, out}
}

// Evacuate: a dead node's durable sandboxes are re-homed from their last published bundle; the rest are lost. One replica at a time (fleet lease).
func (l *Lifecycle) Evacuate(nodeName string) Result {
	n := l.fleet.Get(nodeName)
	if n == nil {
		return Result{404, map[string]any{"error": "no node " + nodeName}}
	}
	if n.Alive() {
		return Result{409, map[string]any{"error": "node is alive; use migrate"}}
	}
	moved, lost := []map[string]any{}, []map[string]any{}
	ran := l.fleet.Lease("evacuate:"+nodeName, 10*time.Minute, func() {
		routes := l.Routes()
		sids := []string{}
		for sid, r := range routes {
			if r.Node == nodeName {
				sids = append(sids, sid)
			}
		}
		sort.Strings(sids)
		for _, sid := range sids {
			r := routes[sid]
			b := l.store.LatestBundle(sid)
			var dst *fleet.Node
			if b != "" {
				dst = l.fleet.LeastLoaded(map[string]any{"mem": 512.0}, map[string]bool{nodeName: true})
			}
			if b != "" && dst != nil {
				st, im, err := l.nc.JSON(dst, "POST", "/sandboxes/import", map[string]any{"bundle": b}, 600*time.Second)
				if err == nil && st == 201 {
					l.routes.RouteSet(sid, Route{Node: dst.Name, Class: r.Class, Durable: r.Durable, Created: r.Created, Project: r.Project, Meta: r.Meta, DiskMiB: r.DiskMiB})
					moved = append(moved, map[string]any{"sandbox": sid, "to": dst.Name, "bundle": b, "fetched_mib": fetched(im)})
					continue
				}
				lost = append(lost, map[string]any{"sandbox": sid, "reason": fmt.Sprintf("import failed: %v", im)})
				continue
			}
			reason := "no target node"
			if b == "" {
				reason = "no bundle in store"
			}
			lost = append(lost, map[string]any{"sandbox": sid, "reason": reason})
			l.routes.RouteDel(sid)
		}
	})
	if !ran {
		return Result{409, map[string]any{"error": "another replica is evacuating this node"}}
	}
	l.audit.Write(map[string]any{"op": "evacuate", "node": nodeName, "moved": pluck(moved, "sandbox"), "lost": pluck(lost, "sandbox")})
	return Result{200, map[string]any{"node": nodeName, "moved": moved, "lost": lost}}
}

// Drain: cordon the node, then move every persistent sandbox it holds to another node (migrate pauses and resumes it); ephemeral ones are left to expire.
func (l *Lifecycle) Drain(nodeName string) Result {
	n := l.fleet.Get(nodeName)
	if n == nil {
		return Result{404, map[string]any{"error": "no node " + nodeName}}
	}
	if err := l.fleet.Cordon(nodeName, true); err != nil {
		return Result{500, map[string]any{"error": err.Error()}}
	}
	moved, left, failed := []string{}, []string{}, []map[string]any{}
	ran := l.fleet.Lease("drain:"+nodeName, 30*time.Minute, func() {
		routes := l.Routes()
		sids := []string{}
		for sid, r := range routes {
			if r.Node == nodeName {
				sids = append(sids, sid)
			}
		}
		sort.Strings(sids)
		for _, sid := range sids {
			if routes[sid].Class == "ephemeral" || !n.Alive() {
				left = append(left, sid)
				continue
			}
			res := l.Migrate(sid, "")
			if res.Code == 200 {
				moved = append(moved, sid)
			} else {
				failed = append(failed, map[string]any{"sandbox": sid, "error": res.Body})
			}
		}
	})
	if !ran {
		return Result{409, map[string]any{"error": "another replica is draining this node"}}
	}
	l.audit.Write(map[string]any{"op": "drain", "node": nodeName, "moved": moved, "left": left, "failed": len(failed)})
	return Result{200, map[string]any{"node": nodeName, "cordoned": true, "moved": moved, "left": left, "failed": failed}}
}

// Balance: move up to `max` persistent sandboxes from the most loaded node to the least loaded one while their load differs by more than `gap`.
func (l *Lifecycle) Balance(max int, gap float64) Result {
	if max <= 0 {
		max = 1
	}
	if gap <= 0 {
		gap = 0.25
	}
	moves := []map[string]any{}
	ran := l.fleet.Lease("balance", 30*time.Minute, func() {
		for i := 0; i < max; i++ {
			alive := []*fleet.Node{}
			for _, n := range l.fleet.Alive() {
				if !n.Cordoned() {
					alive = append(alive, n)
				}
			}
			if len(alive) < 2 {
				return
			}
			sort.Slice(alive, func(a, b int) bool { return fleet.Load(alive[a]) > fleet.Load(alive[b]) })
			hi, lo := alive[0], alive[len(alive)-1]
			if fleet.Load(hi)-fleet.Load(lo) <= gap {
				return
			}
			var victim string
			sids := []string{}
			for sid, r := range l.Routes() {
				if r.Node == hi.Name && r.Class != "ephemeral" {
					sids = append(sids, sid)
				}
			}
			sort.Strings(sids)
			if len(sids) > 0 {
				victim = sids[0]
			}
			if victim == "" {
				return
			}
			res := l.Migrate(victim, lo.Name)
			moves = append(moves, map[string]any{"sandbox": victim, "from": hi.Name, "to": lo.Name, "ok": res.Code == 200})
			if res.Code != 200 {
				return
			}
			hi.AddNum("vms_total", -1)
			lo.AddNum("vms_total", 1) // feedback until the next poll
		}
	})
	if !ran {
		return Result{409, map[string]any{"error": "another replica is balancing"}}
	}
	l.audit.Write(map[string]any{"op": "balance", "moves": moves})
	return Result{200, map[string]any{"moves": moves}}
}

// Reconcile: a node (re)appeared. What it holds that we route elsewhere is a stale copy; what we route to it that it lacks is gone;
// what it has that we do not know is adopted. One replica per node at a time.
func (l *Lifecycle) Reconcile(n *fleet.Node) {
	l.fleet.Lease("reconcile:"+n.Name, 30*time.Second, func() {
		st, lst, err := l.nc.JSON(n, "GET", "/sandboxes", nil, 30*time.Second)
		if err != nil || st != 200 {
			return
		}
		have := map[string]map[string]any{}
		if arr, ok := lst["sandboxes"].([]any); ok {
			for _, x := range arr {
				if m, ok := x.(map[string]any); ok {
					if id, _ := m["id"].(string); id != "" {
						have[id] = m
					}
				}
			}
		}
		routes, err := l.routes.Routes()
		if err != nil {
			l.audit.Write(map[string]any{"op": "reconcile_error", "node": n.Name, "error": err.Error()})
			return
		}
		for sid, sb := range have {
			r, known := routes[sid]
			switch {
			case known && r.Node != n.Name:
				l.nc.JSON(n, "DELETE", "/sandboxes/"+sid, nil, 60*time.Second)
				l.audit.Write(map[string]any{"op": "reconcile_delete_stale", "node": n.Name, "sandbox": sid, "owner": r.Node})
			case !known:
				cls, _ := sb["class"].(string)
				dur, _ := sb["durable"].(bool)
				created, _ := sb["created"].(float64)
				disk, _ := sb["disk"].(float64)
				rt := Route{Node: n.Name, Class: cls, Durable: dur, Created: created, DiskMiB: disk}
				if lb, ok := sb["labels"].(map[string]any); ok { // what the control plane told the node at create: the project and priority come back, so billing and quota stay right
					rt.Project, _ = lb["project"].(string)
					rt.Priority, _ = lb["priority"].(string)
					for k, v := range lb {
						if strings.HasPrefix(k, "meta.") {
							if rt.Meta == nil {
								rt.Meta = map[string]string{}
							}
							rt.Meta[strings.TrimPrefix(k, "meta.")], _ = v.(string)
						}
					}
				}
				l.routes.RouteSet(sid, rt)
				l.audit.Write(map[string]any{"op": "reconcile_adopt", "node": n.Name, "sandbox": sid, "project": rt.Project, "labelled": rt.Project != ""})
			}
		}
		for sid, r := range routes {
			if r.Node == n.Name {
				if _, ok := have[sid]; !ok {
					l.routes.RouteDel(sid)
					l.audit.Write(map[string]any{"op": "reconcile_route_dropped", "node": n.Name, "sandbox": sid})
				}
			}
		}
	})
}

func fetched(im map[string]any) any {
	if imp, ok := im["import"].(map[string]any); ok {
		return imp["fetched_mib"]
	}
	return nil
}

func nz(st, dflt int) int {
	if st == 0 {
		return dflt
	}
	return st
}

func Round3(f float64) float64 { return float64(int64(f*1000+0.5)) / 1000 }

func keys(m map[string]bool) []string {
	out := []string{}
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func pluck(rows []map[string]any, k string) []any {
	out := []any{}
	for _, r := range rows {
		out = append(out, r[k])
	}
	return out
}

// preempt pauses one batch or background sandbox so an interactive request finds memory (D38 补, the pause half of preemption: a paused sandbox
// keeps its VM slot, so this helps when memory is the scarce thing, which it is on every node we run). Background goes before batch, the
// youngest first (least work lost). Returns the victim's id, or "" when there was nothing to pause.
func (l *Lifecycle) preempt(spec map[string]any, forProject string, destroy bool) string {
	type cand struct {
		sid  string
		r    Route
		rank int
	}
	cands := []cand{}
	for sid, r := range l.Routes() {
		rank := 0
		switch r.Priority {
		case fleet.PriorityBackground:
			rank = 2
		case fleet.PriorityBatch:
			rank = 1
		default:
			continue
		}
		n := l.fleet.Get(r.Node)
		if n == nil || !n.Alive() {
			continue
		}
		cands = append(cands, cand{sid, r, rank})
	}
	sort.Slice(cands, func(i, j int) bool {
		if cands[i].rank != cands[j].rank {
			return cands[i].rank > cands[j].rank
		}
		return cands[i].r.Created > cands[j].r.Created
	})
	for _, c := range cands {
		n := l.fleet.Get(c.r.Node)
		st, info, err := l.nc.JSON(n, "GET", "/sandboxes/"+c.sid, nil, 10*time.Second)
		if err != nil || st != 200 {
			continue
		}
		mem, _ := info["mem"].(float64)
		if destroy { // the slot half: only ephemeral work, which its owner (the evaluator) re-runs
			if c.r.Class != "ephemeral" {
				continue
			}
			if st, _, err := l.nc.JSON(n, "DELETE", "/sandboxes/"+c.sid, nil, 120*time.Second); err != nil || (st != 200 && st != 404) {
				continue
			}
			l.Forget(c.sid, n)
			if info["status"] == "running" {
				n.AddNum("vms_running", -1)
				n.AddNum("mem_mib_running", -mem)
			}
			n.AddNum("vms_total", -1)
			l.audit.Write(map[string]any{"op": "preempt_destroy", "sandbox": c.sid, "priority": c.r.Priority, "node": n.Name, "project": c.r.Project, "for_project": forProject})
			return c.sid
		}
		if info["status"] != "running" {
			continue
		}
		st, _, err = l.nc.JSON(n, "POST", "/sandboxes/"+c.sid+"/pause", nil, 120*time.Second)
		if err != nil || st != 200 {
			continue
		}
		n.AddNum("vms_running", -1)
		n.AddNum("mem_mib_running", -mem)
		l.audit.Write(map[string]any{"op": "preempt", "sandbox": c.sid, "priority": c.r.Priority, "node": n.Name, "project": c.r.Project, "for_project": forProject, "mem": mem})
		return c.sid
	}
	return ""
}

// Import re-homes a sandbox from a published bundle by hand (a durable one whose node is gone and that nobody evacuated, or one archived on purpose):
// the named node or the least loaded one imports it paused; the route is written for the given project.
func (l *Lifecycle) Import(bundle, nodeName, project string) Result {
	if bundle == "" {
		return Result{400, map[string]any{"error": "bundle required"}}
	}
	var dst *fleet.Node
	if nodeName != "" {
		dst = l.fleet.Get(nodeName)
	} else {
		dst = l.fleet.LeastLoaded(map[string]any{"mem": 512.0}, nil)
	}
	if dst == nil || !dst.Alive() {
		return Result{429, map[string]any{"error": "no target node"}}
	}
	st, im, err := l.nc.JSON(dst, "POST", "/sandboxes/import", map[string]any{"bundle": bundle}, 600*time.Second)
	if err != nil {
		return Result{502, map[string]any{"error": "node unreachable: " + err.Error()}}
	}
	if st != 201 {
		return Result{nz(st, 502), im}
	}
	id, _ := im["id"].(string)
	cls, _ := im["class"].(string)
	dur, _ := im["durable"].(bool)
	created, _ := im["created"].(float64)
	disk, _ := im["disk"].(float64)
	if project == "" {
		project = "default"
	}
	l.routes.RouteSet(id, Route{Node: dst.Name, Class: cls, Durable: dur, Created: created, Project: project, DiskMiB: disk})
	im["node"], im["project"] = dst.Name, project
	l.audit.Write(map[string]any{"op": "import", "sandbox": id, "bundle": bundle, "node": dst.Name, "project": project})
	return Result{201, im}
}

// startCommand runs `cmd` once, in the background, as the sandbox's agent user (D71): output goes to /home/agent/start.log, the answer says the pid or why not. The sandbox
// exists whether or not this works (a failed start is the caller's to read, not a failed create). It is not re-run on resume or after a migration: the process
// lives in the sandbox's memory, which a pause keeps; a checkpoint rollback restores it as it was.
func (l *Lifecycle) startCommand(sid, cmd string) map[string]any {
	wrapped := "nohup sh -c '" + strings.ReplaceAll(cmd, "'", `'\''`) + "' >/home/agent/start.log 2>&1 </dev/null & echo $!"
	code, out := l.NodeCall(sid, "POST", "/exec", map[string]any{"cmd": wrapped, "timeout": 15})
	if code != 200 {
		l.audit.Write(map[string]any{"op": "start_cmd_failed", "sandbox": sid, "status": code, "error": out["error"]})
		return map[string]any{"started": false, "error": fmt.Sprint(out["error"]), "status": code}
	}
	pid := strings.TrimSpace(fmt.Sprint(out["stdout"]))
	l.audit.Write(map[string]any{"op": "start_cmd", "sandbox": sid, "pid": pid})
	return map[string]any{"started": true, "pid": pid, "log": "/home/agent/start.log"}
}
