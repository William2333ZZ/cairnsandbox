// Package abuse watches for sandboxes that look like mining or flooding (D58). It only observes and flags: a signal goes to the audit log, a metric and
// GET /abuse/signals for the deployer; stopping an account is the deployer's call (POST /accounts/<a>/suspend). A build that pegs the CPU for ten minutes is
// legitimate, so nothing here acts on its own.
package abuse

import (
	"sort"
	"sync"
	"time"
)

// Sandbox is what the watcher needs to know about a running sandbox.
type Sandbox struct {
	ID      string
	Project string
	VCPU    float64
}

// Point is one metrics sample (the node's GET /sandboxes/<id>/metrics).
type Point struct {
	T      float64
	CPUPct *float64 // % of one core; nil for the first sample
	EgrOut float64  // cumulative bytes
}

// Source is the control plane as the watcher sees it.
type Source interface {
	Running() []Sandbox
	Metrics(sandbox string, since float64) ([]Point, error)
}

type Auditor interface{ Write(map[string]any) }

// Signal is one flagged sandbox.
type Signal struct {
	Sandbox string  `json:"sandbox"`
	Project string  `json:"project"`
	Kind    string  `json:"kind"` // cpu | egress
	Value   float64 `json:"value"`
	Limit   float64 `json:"limit"`
	WindowS float64 `json:"window_s"`
	At      float64 `json:"at"`
}

// Config: thresholds. A zero value turns that check off.
type Config struct {
	CPUPct        float64 // average CPU over the window as % of the sandbox's allocated vCPUs (default 90)
	EgressBytesPS float64 // average outbound bytes per second over the window (default 0 = off)
	WindowS       float64 // default 600
	MinCoverage   float64 // the window must be at least this fraction covered by samples (default 0.8), so a young sandbox is not judged on two points
	ReflagAfterS  float64 // a sandbox is flagged again only after this long (default 1800)
}

func (c *Config) defaults() {
	if c.WindowS <= 0 {
		c.WindowS = 600
	}
	if c.MinCoverage <= 0 {
		c.MinCoverage = 0.8
	}
	if c.ReflagAfterS <= 0 {
		c.ReflagAfterS = 1800
	}
}

type Watcher struct {
	src   Source
	audit Auditor
	cfg   Config
	mu    sync.Mutex
	last  map[string]float64 // sandbox+kind -> when flagged
	sigs  []Signal
	total map[string]int
}

func New(src Source, a Auditor, cfg Config) *Watcher {
	cfg.defaults()
	return &Watcher{src: src, audit: a, cfg: cfg, last: map[string]float64{}, total: map[string]int{}}
}

func (w *Watcher) Loop(every time.Duration) {
	for {
		time.Sleep(every)
		w.Scan(float64(time.Now().UnixNano()) / 1e9)
	}
}

// Scan: one pass over the running sandboxes.
func (w *Watcher) Scan(now float64) {
	for _, sb := range w.src.Running() {
		pts, err := w.src.Metrics(sb.ID, now-w.cfg.WindowS)
		if err != nil || len(pts) < 2 {
			continue
		}
		span := pts[len(pts)-1].T - pts[0].T
		if span < w.cfg.WindowS*w.cfg.MinCoverage {
			continue
		}
		if w.cfg.CPUPct > 0 {
			var sum float64
			var n int
			for _, p := range pts {
				if p.CPUPct != nil {
					sum += *p.CPUPct
					n++
				}
			}
			vcpu := sb.VCPU
			if vcpu <= 0 {
				vcpu = 1
			}
			if n > 0 {
				if avg := sum / float64(n) / vcpu; avg >= w.cfg.CPUPct {
					w.flag(Signal{Sandbox: sb.ID, Project: sb.Project, Kind: "cpu", Value: avg, Limit: w.cfg.CPUPct, WindowS: span, At: now})
				}
			}
		}
		if w.cfg.EgressBytesPS > 0 && pts[len(pts)-1].EgrOut >= pts[0].EgrOut {
			if rate := (pts[len(pts)-1].EgrOut - pts[0].EgrOut) / span; rate >= w.cfg.EgressBytesPS {
				w.flag(Signal{Sandbox: sb.ID, Project: sb.Project, Kind: "egress", Value: rate, Limit: w.cfg.EgressBytesPS, WindowS: span, At: now})
			}
		}
	}
}

func (w *Watcher) flag(s Signal) {
	w.mu.Lock()
	k := s.Sandbox + "/" + s.Kind
	if at, ok := w.last[k]; ok && s.At-at < w.cfg.ReflagAfterS {
		w.mu.Unlock()
		return
	}
	w.last[k] = s.At
	w.sigs = append(w.sigs, s)
	if len(w.sigs) > 200 {
		w.sigs = w.sigs[len(w.sigs)-200:]
	}
	w.total[s.Kind]++
	w.mu.Unlock()
	w.audit.Write(map[string]any{"op": "abuse_signal", "sandbox": s.Sandbox, "project": s.Project, "kind": s.Kind, "value": s.Value, "limit": s.Limit, "window_s": s.WindowS})
}

// Signals: the most recent flags, newest first.
func (w *Watcher) Signals() []Signal {
	w.mu.Lock()
	defer w.mu.Unlock()
	out := append([]Signal(nil), w.sigs...)
	sort.Slice(out, func(i, j int) bool { return out[i].At > out[j].At })
	return out
}

// Total: signals raised since start, by kind (a counter for /metrics).
func (w *Watcher) Total(kind string) int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.total[kind]
}
