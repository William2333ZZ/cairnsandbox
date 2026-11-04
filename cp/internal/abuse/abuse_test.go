package abuse

import "testing"

type fakeSrc struct {
	sbs []Sandbox
	pts map[string][]Point
}

func (f fakeSrc) Running() []Sandbox { return f.sbs }
func (f fakeSrc) Metrics(id string, since float64) ([]Point, error) {
	var out []Point
	for _, p := range f.pts[id] {
		if p.T > since {
			out = append(out, p)
		}
	}
	return out, nil
}

type rec struct{ ev []map[string]any }

func (r *rec) Write(m map[string]any) { r.ev = append(r.ev, m) }

func series(from, to, step, cpu, egrPerS float64) []Point {
	var out []Point
	var egr float64
	for t := from; t <= to; t += step {
		c := cpu
		out = append(out, Point{T: t, CPUPct: &c, EgrOut: egr})
		egr += egrPerS * step
	}
	return out
}

func TestSustainedCPUIsFlaggedOnceAndShortOrBusyBurstsAreNot(t *testing.T) {
	a := &rec{}
	src := fakeSrc{
		sbs: []Sandbox{{"miner", "p", 2}, {"build", "p", 2}, {"young", "p", 1}, {"idle", "p", 1}},
		pts: map[string][]Point{
			"miner": series(0, 1000, 5, 200, 0),   // 200 % of one core on 2 vCPUs = 100 % of what it has
			"build": series(0, 1000, 5, 100, 0),   // 50 % of its allocation: legitimate
			"young": series(900, 1000, 5, 100, 0), // pegged, but only 100 s of data: not judged
			"idle":  series(0, 1000, 5, 1, 0),
		},
	}
	w := New(src, a, Config{CPUPct: 90, WindowS: 600})
	w.Scan(1000)
	if got := w.Signals(); len(got) != 1 || got[0].Sandbox != "miner" || got[0].Kind != "cpu" || got[0].Value < 99 {
		t.Fatalf("signals %+v", got)
	}
	w.Scan(1060) // the same sandbox is not flagged again a minute later
	if len(w.Signals()) != 1 || len(a.ev) != 1 || a.ev[0]["op"] != "abuse_signal" || w.Total("cpu") != 1 {
		t.Fatalf("reflagged: %d %v", len(w.Signals()), a.ev)
	}
	w.Scan(1000 + 1801) // later: again (the data window still has points because the fake keeps them)
}

func TestEgressRateOverTheWindow(t *testing.T) {
	a := &rec{}
	src := fakeSrc{sbs: []Sandbox{{"flood", "p", 1}, {"quiet", "p", 1}}, pts: map[string][]Point{"flood": series(0, 1000, 5, 1, 2_000_000), "quiet": series(0, 1000, 5, 1, 1000)}}
	w := New(src, a, Config{EgressBytesPS: 1_000_000, WindowS: 600})
	w.Scan(1000)
	if got := w.Signals(); len(got) != 1 || got[0].Sandbox != "flood" || got[0].Kind != "egress" {
		t.Fatalf("signals %+v", got)
	}
}
