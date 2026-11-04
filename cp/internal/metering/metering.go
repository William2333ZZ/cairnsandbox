// Package metering is context 7 (docs/002 沙箱侧的领域 计量与账单): UsageEvent (append-only), PriceList (versioned), Budget (the project's daily
// quota), Statement (sums over a period). Usage comes from two meters: the tick (every minute, one replica under a lease) charges every running
// sandbox for vcpu, memory and disk from what the nodes report, and reads the model proxy's cumulative per-sandbox usage to emit the deltas.
// Other contexts never compute money; they report amounts and this one prices them.
package metering

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"

	"fcnode/cp/internal/db"
	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/lifecycle"
	"fcnode/cp/internal/nodeclient"
	"fcnode/cp/internal/registry"
)

// Kinds of usage and their units.
const (
	KindVCPUSeconds    = "vcpu_s"        // vcpu × seconds running
	KindMemMiBSeconds  = "mem_mib_s"     // MiB × seconds running
	KindDiskMiBSeconds = "disk_mib_s"    // writable layer MiB × seconds (allocated, running or paused)
	KindVCPUActive     = "vcpu_active_s" // CPU-seconds the guest really used (cgroup); priced separately, default price 0 = not recorded (D67)
	KindModelTokensIn  = "model_tokens_in"
	KindModelTokensOut = "model_tokens_out"
	KindModelTokensHit = "model_tokens_cached"
	KindEgressBytes    = "egress_bytes" // bytes through the egress proxy, both directions
	SourceTick         = "tick"
	SourceModelProxy   = "model_proxy"
)

// Prices in USD per unit of each kind; model tokens are priced by the proxy (it knows the model), so the event carries the cost.
type Prices struct {
	Version int64              `json:"version"`
	PerUnit map[string]float64 `json:"per_unit"`
	Created float64            `json:"created"`
}

var DefaultPrices = map[string]float64{KindVCPUSeconds: 0.000014, KindMemMiBSeconds: 0.0000000044, KindDiskMiBSeconds: 0.00000000003, KindEgressBytes: 0.00000000004} // D65: at parity with E2B / Daytona list prices: $0.0504/vcpu-h, $0.0162/GiB-h memory, ~$0.00011/GiB-h disk, $0.04/GiB egress

type Event struct {
	ID      string  `json:"id"`
	TS      float64 `json:"ts"`
	Project string  `json:"project"`
	Sandbox string  `json:"sandbox"`
	Kind    string  `json:"kind"`
	Amount  float64 `json:"amount"`
	Unit    string  `json:"unit"`
	CostUSD float64 `json:"cost_usd"`
	Source  string  `json:"source"`
	Meta    string  `json:"meta,omitempty"`
}

type Auditor interface{ Write(map[string]any) }

// Proxy is where model usage is read from (the model proxy's GET /usage, with its admin token).
type Proxy struct {
	URL   string
	Token string
}

type Metering struct {
	db    *db.DB
	fleet *fleet.Fleet
	life  *lifecycle.Lifecycle
	nc    nodeclient.Client
	proxy Proxy
	audit Auditor
	get   func(url, token string) ([]byte, error) // overridable in tests
}

func New(d *db.DB, f *fleet.Fleet, l *lifecycle.Lifecycle, proxy Proxy, audit Auditor) *Metering {
	return &Metering{db: d, fleet: f, life: l, proxy: proxy, audit: audit, get: httpGet}
}

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func (m *Metering) DB() *db.DB { return m.db }

// ---- prices ----

func (m *Metering) Prices() (Prices, error) {
	var p Prices
	var body string
	err := m.db.QueryRow(`SELECT version, body, created FROM prices ORDER BY version DESC LIMIT 1`).Scan(&p.Version, &body, &p.Created)
	if err == sql.ErrNoRows {
		return Prices{Version: 0, PerUnit: copyMap(DefaultPrices)}, nil
	}
	if err != nil {
		return p, err
	}
	_ = json.Unmarshal([]byte(body), &p.PerUnit)
	return p, nil
}

func (m *Metering) PricesSet(perUnit map[string]float64) (Prices, error) {
	for k, v := range perUnit {
		if v < 0 {
			return Prices{}, fmt.Errorf("price for %s is negative", k)
		}
	}
	cur, err := m.Prices()
	if err != nil {
		return Prices{}, err
	}
	merged := copyMap(cur.PerUnit)
	for k, v := range perUnit {
		merged[k] = v
	}
	b, _ := json.Marshal(merged)
	p := Prices{Version: cur.Version + 1, PerUnit: merged, Created: now()}
	_, err = m.db.Exec(`INSERT INTO prices(version, body, created) VALUES ($1, $2, $3)`, p.Version, string(b), p.Created)
	return p, err
}

func copyMap(in map[string]float64) map[string]float64 {
	out := make(map[string]float64, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// ---- events ----

// Record appends one usage event; cost 0 with a price list means "price it here", else the cost given (model tokens) is kept.
func (m *Metering) Record(ev Event) (Event, error) {
	if ev.ID == "" {
		ev.ID = "u-" + registry.NewToken()[:12]
	}
	if ev.TS == 0 {
		ev.TS = now()
	}
	if ev.Unit == "" {
		ev.Unit = ev.Kind
	}
	if ev.CostUSD == 0 && ev.Source != SourceModelProxy {
		p, err := m.Prices()
		if err != nil {
			return ev, err
		}
		ev.CostUSD = ev.Amount * p.PerUnit[ev.Kind]
	}
	_, err := m.db.Exec(`INSERT INTO usage_events(id, ts, project, sandbox, kind, amount, unit, cost_usd, source, meta) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
		ev.ID, ev.TS, ev.Project, ev.Sandbox, ev.Kind, ev.Amount, ev.Unit, ev.CostUSD, ev.Source, ev.Meta)
	return ev, err
}

type Line struct {
	Kind    string  `json:"kind"`
	Amount  float64 `json:"amount"`
	Unit    string  `json:"unit"`
	CostUSD float64 `json:"cost_usd"`
	Events  int64   `json:"events"`
}

type Statement struct {
	Project   string  `json:"project"`
	From      float64 `json:"from"`
	To        float64 `json:"to"`
	Lines     []Line  `json:"lines"`
	TotalUSD  float64 `json:"total_usd"`
	Sandboxes int64   `json:"sandboxes"`
}

// StatementFor sums a project's usage between two times (unix seconds; to = 0 means now).
func (m *Metering) StatementFor(project string, from, to float64) (Statement, error) {
	if to == 0 {
		to = now()
	}
	st := Statement{Project: project, From: from, To: to, Lines: []Line{}}
	rows, err := m.db.Query(`SELECT kind, unit, sum(amount), sum(cost_usd), count(*) FROM usage_events WHERE project = $1 AND ts >= $2 AND ts < $3 GROUP BY kind, unit`, project, from, to)
	if err != nil {
		return st, err
	}
	defer rows.Close()
	for rows.Next() {
		var l Line
		if err := rows.Scan(&l.Kind, &l.Unit, &l.Amount, &l.CostUSD, &l.Events); err != nil {
			return st, err
		}
		st.Lines = append(st.Lines, l)
		st.TotalUSD += l.CostUSD
	}
	sort.Slice(st.Lines, func(i, j int) bool { return st.Lines[i].Kind < st.Lines[j].Kind })
	m.db.QueryRow(`SELECT count(DISTINCT sandbox) FROM usage_events WHERE project = $1 AND ts >= $2 AND ts < $3`, project, from, to).Scan(&st.Sandboxes)
	st.TotalUSD = round6(st.TotalUSD)
	return st, nil
}

// Settled is a Statement frozen into the ledger (docs/002 沙箱侧 7 Statement): what was billed for a period, kept as written even if prices or events change later.
type Settled struct {
	ID        string  `json:"id"`
	Project   string  `json:"project"`
	From      float64 `json:"from"`
	To        float64 `json:"to"`
	TotalUSD  float64 `json:"total_usd"`
	Sandboxes int64   `json:"sandboxes"`
	Lines     []Line  `json:"lines"`
	Created   float64 `json:"created"`
}

// Settle computes the statement for [from, to) and writes it down; a period that overlaps one already settled is refused.
func (m *Metering) Settle(project string, from, to float64) (Settled, error) {
	if to == 0 {
		to = now()
	}
	if from < 0 || to <= from {
		return Settled{}, errors.New("from must be before to")
	}
	var n int
	m.db.QueryRow(`SELECT count(*) FROM meter_statements WHERE project = $1 AND from_ts < $2 AND to_ts > $3`, project, to, from).Scan(&n)
	if n > 0 {
		return Settled{}, errors.New("period overlaps a settled statement")
	}
	st, err := m.StatementFor(project, from, to)
	if err != nil {
		return Settled{}, err
	}
	out := Settled{ID: "st-" + registry.NewToken()[:10], Project: project, From: from, To: to, TotalUSD: st.TotalUSD, Sandboxes: st.Sandboxes, Lines: st.Lines, Created: now()}
	lines, _ := json.Marshal(st.Lines)
	if _, err := m.db.Exec(`INSERT INTO meter_statements(id, project, from_ts, to_ts, total_usd, sandboxes, lines, created) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
		out.ID, out.Project, out.From, out.To, out.TotalUSD, out.Sandboxes, string(lines), out.Created); err != nil {
		return Settled{}, err
	}
	m.audit.Write(map[string]any{"op": "statement_settled", "id": out.ID, "project": project, "from": from, "to": to, "total_usd": out.TotalUSD})
	return out, nil
}

// Statements: the settled ones of a project, newest first.
func (m *Metering) Statements(project string) ([]Settled, error) {
	rows, err := m.db.Query(`SELECT id, project, from_ts, to_ts, total_usd, sandboxes, lines, created FROM meter_statements WHERE project = $1 ORDER BY from_ts DESC`, project)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Settled{}
	for rows.Next() {
		var s Settled
		var lines string
		if err := rows.Scan(&s.ID, &s.Project, &s.From, &s.To, &s.TotalUSD, &s.Sandboxes, &lines, &s.Created); err != nil {
			return nil, err
		}
		s.Lines = []Line{}
		json.Unmarshal([]byte(lines), &s.Lines)
		out = append(out, s)
	}
	return out, nil
}

// CSV renders a statement for a spreadsheet: one row per kind, then the total.
func CSV(st Statement) string {
	var b strings.Builder
	b.WriteString("project,from,to,kind,unit,amount,cost_usd,events\n")
	for _, l := range st.Lines {
		fmt.Fprintf(&b, "%s,%.0f,%.0f,%s,%s,%g,%.6f,%d\n", st.Project, st.From, st.To, l.Kind, l.Unit, l.Amount, l.CostUSD, l.Events)
	}
	fmt.Fprintf(&b, "%s,%.0f,%.0f,total,usd,,%.6f,%d\n", st.Project, st.From, st.To, st.TotalUSD, st.Sandboxes)
	return b.String()
}

// SpentToday: the project's cost since local midnight (the daily budget's window). Satisfies lifecycle.Spend.
func (m *Metering) SpentToday(project string) float64 {
	y, mo, d := time.Now().Date()
	midnight := float64(time.Date(y, mo, d, 0, 0, 0, 0, time.Local).Unix())
	var v sql.NullFloat64
	m.db.QueryRow(`SELECT sum(cost_usd) FROM usage_events WHERE project = $1 AND ts >= $2`, project, midnight).Scan(&v)
	return v.Float64
}

// SandboxUsage: everything recorded for one sandbox, summed by kind.
func (m *Metering) SandboxUsage(sandbox string) ([]Line, error) {
	rows, err := m.db.Query(`SELECT kind, unit, sum(amount), sum(cost_usd), count(*) FROM usage_events WHERE sandbox = $1 GROUP BY kind, unit ORDER BY kind`, sandbox)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Line{}
	for rows.Next() {
		var l Line
		if err := rows.Scan(&l.Kind, &l.Unit, &l.Amount, &l.CostUSD, &l.Events); err != nil {
			return nil, err
		}
		out = append(out, l)
	}
	return out, nil
}

// ---- the tick ----

// Tick charges every sandbox the nodes report for the time since the last tick, and emits model-usage deltas from the proxy. Returns how many events it wrote.
// One replica at a time: the caller wraps it in fleet.Lease("metering:tick").
func (m *Metering) Tick() (int, error) {
	var last float64
	m.db.QueryRow(`SELECT v FROM meter_state WHERE k = 'last_tick'`).Scan(&last)
	t := now()
	dt := t - last
	if last == 0 || dt > 3600 || dt < 0 { // first tick, or a long gap (a replica was down): do not back-charge an unknown period
		dt = 0
	}
	n := 0
	routes := m.life.Routes()
	activePrice := 0.0
	if p, err := m.Prices(); err == nil {
		activePrice = p.PerUnit[KindVCPUActive]
	}
	first := last == 0 // the meter's first tick ever: establish baselines (time and proxy cursors), charge nothing
	if dt > 0 {
		for _, node := range m.fleet.Alive() {
			st, lst, err := m.nc.JSON(node, "GET", "/sandboxes", nil, 10*time.Second)
			if err != nil || st != 200 {
				continue
			}
			arr, _ := lst["sandboxes"].([]any)
			for _, x := range arr {
				sb, _ := x.(map[string]any)
				id, _ := sb["id"].(string)
				if id == "" {
					continue
				}
				project := routes[id].Project
				if project == "" {
					project = "default"
				}
				status, _ := sb["status"].(string)
				vcpu, _ := sb["vcpu"].(float64)
				mem, _ := sb["mem"].(float64)
				disk, _ := sb["disk"].(float64)
				if used, ok := sb["disk_used_mib"].(float64); ok && used > 0 {
					disk = used // what the writable layer really holds beats its allocated size
				}
				if eg, ok := sb["egress"].(map[string]any); ok { // cumulative bytes through the proxy: record the delta since the cursor
					total := numOf(eg, "out") + numOf(eg, "in")
					var last float64
					m.db.QueryRow(`SELECT bytes FROM meter_egress WHERE sandbox = $1`, id).Scan(&last)
					if total > last {
						if _, err := m.Record(Event{TS: t, Project: project, Sandbox: id, Kind: KindEgressBytes, Amount: total - last, Source: SourceTick}); err == nil {
							n++
						}
					}
					if total != last {
						m.db.Exec(`INSERT INTO meter_egress(sandbox, bytes, seen) VALUES ($1, $2, $3) ON CONFLICT(sandbox) DO UPDATE SET bytes = $2, seen = $3`, id, int64(total), t)
					}
				}
				if cpu, ok := sb["cpu_usec"].(float64); ok && activePrice > 0 { // D67: bill the CPU time the guest used, not the vCPUs it holds
					var lastCPU sql.NullFloat64
					seen := m.db.QueryRow(`SELECT usec FROM meter_cpu WHERE sandbox = $1`, id).Scan(&lastCPU) == nil
					delta := cpu - lastCPU.Float64
					if cpu < lastCPU.Float64 { // the VM's cgroup was recreated (resume after a pause): the counter restarted from zero
						delta = cpu
					}
					if !seen && t-numOf(sb, "created") > 300 { // a sandbox that already ran before the meter looked at it: baseline only, no back-charge
						delta = 0
					}
					if delta > 0 {
						if _, err := m.Record(Event{TS: t, Project: project, Sandbox: id, Kind: KindVCPUActive, Amount: delta / 1e6, Source: SourceTick}); err == nil {
							n++
						}
					}
					if !seen || cpu != lastCPU.Float64 {
						m.db.Exec(`INSERT INTO meter_cpu(sandbox, usec, seen) VALUES ($1, $2, $3) ON CONFLICT(sandbox) DO UPDATE SET usec = $2, seen = $3`, id, int64(cpu), t)
					}
				}
				if status == "running" {
					if _, err := m.Record(Event{TS: t, Project: project, Sandbox: id, Kind: KindVCPUSeconds, Amount: vcpu * dt, Source: SourceTick}); err == nil {
						n++
					}
					if _, err := m.Record(Event{TS: t, Project: project, Sandbox: id, Kind: KindMemMiBSeconds, Amount: mem * dt, Source: SourceTick}); err == nil {
						n++
					}
				}
				if disk > 0 && (status == "running" || status == "paused") {
					if _, err := m.Record(Event{TS: t, Project: project, Sandbox: id, Kind: KindDiskMiBSeconds, Amount: disk * dt, Source: SourceTick}); err == nil {
						n++
					}
				}
			}
		}
	}
	n += m.modelDeltas(t, routes, first)
	if _, err := m.db.Exec(`INSERT INTO meter_state(k, v) VALUES ('last_tick', $1) ON CONFLICT(k) DO UPDATE SET v = $1`, t); err != nil {
		return n, err
	}
	return n, nil
}

// modelDeltas reads the proxy's cumulative per-sandbox usage and records what is new since the cursor. On the meter's first tick it only sets the
// cursors: the proxy's history predates the meter and must not land on today.
func (m *Metering) modelDeltas(t float64, routes map[string]lifecycle.Route, baselineOnly bool) int {
	if m.proxy.URL == "" {
		return 0
	}
	data, err := m.get(m.proxy.URL+"/usage", m.proxy.Token)
	if err != nil {
		m.audit.Write(map[string]any{"op": "metering_proxy_error", "error": err.Error()})
		return 0
	}
	var doc struct {
		Usage map[string]map[string]any `json:"usage"`
	}
	if json.Unmarshal(data, &doc) != nil {
		return 0
	}
	n := 0
	for sid, u := range doc.Usage {
		var cur struct {
			cost         float64
			in, out, hit int64
		}
		m.db.QueryRow(`SELECT cost_usd, tokens_in, tokens_out, tokens_cached FROM meter_cursor WHERE sandbox = $1`, sid).Scan(&cur.cost, &cur.in, &cur.out, &cur.hit)
		cost, _ := u["cost_usd"].(float64)
		in, _ := u["tokens_in"].(float64)
		out, _ := u["tokens_out"].(float64)
		hit, _ := u["tokens_cached"].(float64)
		dIn, dOut, dHit, dCost := int64(in)-cur.in, int64(out)-cur.out, int64(hit)-cur.hit, cost-cur.cost
		if dIn <= 0 && dOut <= 0 && dHit <= 0 && dCost <= 0 {
			continue
		}
		if baselineOnly {
			m.db.Exec(`INSERT INTO meter_cursor(sandbox, cost_usd, tokens_in, tokens_out, tokens_cached, seen) VALUES ($1, $2, $3, $4, $5, $6)
				ON CONFLICT(sandbox) DO UPDATE SET cost_usd = $2, tokens_in = $3, tokens_out = $4, tokens_cached = $5, seen = $6`, sid, cost, int64(in), int64(out), int64(hit), t)
			continue
		}
		project := routes[sid].Project
		if project == "" {
			project = "default"
		}
		// the cost goes on the input-token line (the proxy prices the whole call); the other lines carry amounts only
		if dIn > 0 || dCost > 0 {
			if _, err := m.Record(Event{TS: t, Project: project, Sandbox: sid, Kind: KindModelTokensIn, Amount: float64(max(dIn, 0)), CostUSD: round6(max(dCost, 0)), Source: SourceModelProxy}); err == nil {
				n++
			}
		}
		if dOut > 0 {
			if _, err := m.Record(Event{TS: t, Project: project, Sandbox: sid, Kind: KindModelTokensOut, Amount: float64(dOut), Source: SourceModelProxy}); err == nil {
				n++
			}
		}
		if dHit > 0 {
			if _, err := m.Record(Event{TS: t, Project: project, Sandbox: sid, Kind: KindModelTokensHit, Amount: float64(dHit), Source: SourceModelProxy}); err == nil {
				n++
			}
		}
		m.db.Exec(`INSERT INTO meter_cursor(sandbox, cost_usd, tokens_in, tokens_out, tokens_cached, seen) VALUES ($1, $2, $3, $4, $5, $6)
			ON CONFLICT(sandbox) DO UPDATE SET cost_usd = $2, tokens_in = $3, tokens_out = $4, tokens_cached = $5, seen = $6`, sid, cost, int64(in), int64(out), int64(hit), t)
	}
	return n
}

// Loop runs the tick every `every` on one replica (the lease), forever.
func (m *Metering) Loop(every time.Duration) {
	for {
		m.fleet.Lease("metering:tick", every, func() {
			if n, err := m.Tick(); err != nil {
				m.audit.Write(map[string]any{"op": "metering_error", "error": err.Error()})
			} else if n > 0 {
				m.audit.Write(map[string]any{"op": "metering_tick", "events": n})
			}
		})
		time.Sleep(every)
	}
}

func httpGet(url, token string) ([]byte, error) {
	req, _ := http.NewRequest("GET", url, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("proxy /usage: %d", resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

func round6(f float64) float64 { return float64(int64(f*1e6+0.5)) / 1e6 }

func numOf(m map[string]any, k string) float64 {
	f, _ := m[k].(float64)
	return f
}
