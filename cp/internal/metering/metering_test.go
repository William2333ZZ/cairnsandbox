package metering

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"fcnode/cp/internal/audit"
	"fcnode/cp/internal/db"
	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/lifecycle"
	"fcnode/cp/internal/nodeclient"
	"fcnode/cp/internal/registry"
	"fcnode/cp/internal/state"
)

func meter(t *testing.T) (*Metering, *lifecycle.Lifecycle) {
	d := db.OpenForTest(t)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	log := audit.Discard("t")
	fl := fleet.New(reg, log)
	life := lifecycle.New(reg, fl, state.Store{}, log)
	return New(d, fl, life, Proxy{URL: "http://proxy"}, log), life
}

func TestModelUsageDeltasFollowTheProxyCursorAndLandOnTheRoutesProject(t *testing.T) {
	m, life := meter(t)
	life.SetRoute("sb1", lifecycle.Route{Node: "n", Project: "acme"})
	history := map[string]any{"usage": map[string]any{"old": map[string]any{"cost_usd": 9, "tokens_in": 99999}}}
	m.get = func(url, token string) ([]byte, error) { return json.Marshal(history) }
	if n, _ := m.Tick(); n != 0 || m.SpentToday("default") != 0 {
		t.Fatal("the meter's first tick must only set baselines, got", n)
	}
	snapshot := map[string]any{"usage": map[string]any{"sb1": map[string]any{"cost_usd": 0.5, "tokens_in": 1000, "tokens_out": 200, "tokens_cached": 300}}}
	m.get = func(url, token string) ([]byte, error) { return json.Marshal(snapshot) }
	if n, err := m.Tick(); err != nil || n != 3 {
		t.Fatal("first delta", n, err)
	}
	if n, _ := m.Tick(); n != 0 {
		t.Fatal("no change must record nothing, got", n)
	}
	snapshot["usage"].(map[string]any)["sb1"] = map[string]any{"cost_usd": 0.8, "tokens_in": 1500, "tokens_out": 200, "tokens_cached": 300}
	if n, _ := m.Tick(); n != 1 {
		t.Fatal("only the input line changed, got", n)
	}
	st, _ := m.StatementFor("acme", 0, 0)
	if st.TotalUSD != 0.8 || st.Sandboxes != 1 {
		t.Fatalf("statement %+v", st)
	}
	lines, _ := m.SandboxUsage("sb1")
	byKind := map[string]Line{}
	for _, l := range lines {
		byKind[l.Kind] = l
	}
	if byKind[KindModelTokensIn].Amount != 1500 || byKind[KindModelTokensOut].Amount != 200 || byKind[KindModelTokensHit].Amount != 300 || byKind[KindModelTokensIn].CostUSD != 0.8 {
		t.Fatalf("lines %+v", byKind)
	}
	if m.SpentToday("acme") != 0.8 || m.SpentToday("default") != 0 {
		t.Fatal("spent today", m.SpentToday("acme"))
	}
	// an unknown sandbox's usage lands on the default project; a dead proxy records nothing and does not fail the tick
	snapshot["usage"].(map[string]any)["ghost"] = map[string]any{"cost_usd": 0.1, "tokens_in": 10}
	m.Tick()
	if m.SpentToday("default") != 0.1 {
		t.Fatal("ghost usage", m.SpentToday("default"))
	}
	m.get = func(url, token string) ([]byte, error) { return nil, errTest }
	if _, err := m.Tick(); err != nil {
		t.Fatal("a proxy error must not fail the tick:", err)
	}
}

func TestPricesAreVersionedAndMerged(t *testing.T) {
	m, _ := meter(t)
	p, _ := m.Prices()
	if p.Version != 0 || p.PerUnit[KindVCPUSeconds] != DefaultPrices[KindVCPUSeconds] {
		t.Fatalf("defaults %+v", p)
	}
	p, err := m.PricesSet(map[string]float64{KindVCPUSeconds: 0.5})
	if err != nil || p.Version != 1 || p.PerUnit[KindVCPUSeconds] != 0.5 || p.PerUnit[KindMemMiBSeconds] != DefaultPrices[KindMemMiBSeconds] {
		t.Fatalf("set %+v %v", p, err)
	}
	if _, err := m.PricesSet(map[string]float64{KindVCPUSeconds: -1}); err == nil {
		t.Fatal("negative price")
	}
	ev, _ := m.Record(Event{Project: "p", Sandbox: "s", Kind: KindVCPUSeconds, Amount: 4, Source: SourceTick})
	if ev.CostUSD != 2 || ev.ID == "" || ev.Unit != KindVCPUSeconds {
		t.Fatalf("priced event %+v", ev)
	}
}

type testErr struct{}

func (testErr) Error() string { return "boom" }

var errTest = testErr{}

func TestBreakdownGroupsBySandboxAndByDayAndFoldsTheTail(t *testing.T) {
	m, _ := meter(t)
	rec := func(ts float64, sb, kind string, cost float64) {
		if _, err := m.Record(Event{TS: ts, Project: "p", Sandbox: sb, Kind: kind, Amount: 1, CostUSD: cost}); err != nil {
			t.Fatal(err)
		}
	}
	d0 := float64(1767225600) // a UTC midnight
	rec(d0+10, "sb-a", "vcpu_s", 1)
	rec(d0+20, "sb-a", "mem_mib_s", 0.5)
	rec(d0+86400+5, "sb-b", "vcpu_s", 3)
	rec(d0+86400+9, "sb-c", "vcpu_s", 0.25)
	rec(d0+2*86400+1, "sb-c", "vcpu_s", 0.25)
	m.Record(Event{TS: d0 + 7, Project: "other", Sandbox: "sb-x", Kind: "vcpu_s", Amount: 1, CostUSD: 99}) // another project never shows
	rows, err := m.Breakdown("p", "sandbox", d0, d0+3*86400, 2)
	if err != nil || len(rows) != 3 || rows[0].Key != "sb-b" || rows[0].CostUSD != 3 || rows[1].Key != "sb-a" || rows[1].Kinds["mem_mib_s"] != 0.5 || rows[2].Key != "other" || rows[2].CostUSD != 0.5 {
		t.Fatalf("by sandbox: %v %+v", err, rows)
	}
	days, err := m.Breakdown("p", "day", d0, d0+3*86400, 0)
	if err != nil || len(days) != 3 || days[0].Key != fmt.Sprint(int64(d0)) || days[0].CostUSD != 1.5 || days[1].CostUSD != 3.25 || days[2].CostUSD != 0.25 {
		t.Fatalf("by day: %v %+v", err, days)
	}
	if _, err := m.Breakdown("p", "label", 0, 0, 0); err == nil {
		t.Fatal("an unknown grouping was accepted")
	}
}

func TestActiveCPUIsBilledFromTheGuestsOwnCounterOnlyWhenItHasAPrice(t *testing.T) {
	var cpu, oldCPU float64
	created := float64(time.Now().Unix())
	node := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"sandboxes": []map[string]any{
			{"id": "sb-new", "status": "running", "vcpu": 1, "mem": 128, "cpu_usec": cpu, "created": created},
			{"id": "sb-old", "status": "running", "vcpu": 1, "mem": 128, "cpu_usec": oldCPU, "created": created - 5000},
		}})
	}))
	defer node.Close()
	d := db.OpenForTest(t)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	log := audit.Discard("t")
	fl := fleet.New(reg, log)
	fl.Add(fleet.Spec{Name: "n", URL: node.URL, Token: "t"}, false)
	fl.PollOnce(nodeclient.Client{}) // the node answers any path with JSON: alive
	life := lifecycle.New(reg, fl, state.Store{}, log)
	life.SetRoute("sb-new", lifecycle.Route{Node: "n", Project: "p"})
	life.SetRoute("sb-old", lifecycle.Route{Node: "n", Project: "p"})
	m := New(d, fl, life, Proxy{}, log)
	active := func() float64 {
		var v sql.NullFloat64
		d.QueryRow(`SELECT sum(amount) FROM usage_events WHERE kind = 'vcpu_active_s'`).Scan(&v)
		return v.Float64
	}
	tick := func() {
		d.Exec(`INSERT INTO meter_state(k, v) VALUES ('last_tick', $1) ON CONFLICT(k) DO UPDATE SET v = $1`, float64(time.Now().Unix())-60)
		if _, err := m.Tick(); err != nil {
			t.Fatal(err)
		}
	}
	cpu, oldCPU = 5e6, 9e9
	tick()
	if active() != 0 {
		t.Fatalf("recorded with no price: %v", active())
	}
	if _, err := m.PricesSet(map[string]float64{KindVCPUActive: 0.0001}); err != nil {
		t.Fatal(err)
	}
	cpu = 8e6 // the cursor was not kept while the price was 0: a young sandbox counts everything it used so far
	tick()
	if got := active(); got != 8 { // sb-new: 8 s from zero; sb-old (older than 5 min, first seen): baseline only
		t.Fatalf("first priced tick: %v", got)
	}
	cpu, oldCPU = 9.5e6, 9e9+2e6
	tick()
	if got := active(); got != 8+1.5+2 {
		t.Fatalf("second tick: %v", got)
	}
	cpu = 1e6 // a resume recreated the cgroup: the counter restarted
	tick()
	if got := active(); got != 8+1.5+2+1 {
		t.Fatalf("after a counter reset: %v", got)
	}
	if c := m.SpentToday("p"); c <= 0 {
		t.Fatal("no cost recorded")
	}
}
