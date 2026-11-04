package fleet

import (
	"math/rand"
)

func specMem(spec map[string]any) float64 {
	if v, ok := spec["mem"].(float64); ok {
		return v
	}
	return 512
}

// Priority classes (docs/002 沙箱侧 3): interactive work (a person waiting) is placed anywhere with room; batch and background work leave `ReserveVMs`
// slots and `ReserveMemMiB` free on every node for it, so an evaluation burst never blocks a person. The class also picks the node's QoS.
const (
	PriorityInteractive = "interactive"
	PriorityBatch       = "batch"
	PriorityBackground  = "background"
)

var ReserveVMs, ReserveMemMiB = 2.0, 1024.0

func (n *Node) reserveVMs() float64 {
	if n.ReserveVMs > 0 {
		return n.ReserveVMs
	}
	return ReserveVMs
}

func (n *Node) reserveMem() float64 {
	if n.ReserveMemMiB > 0 {
		return n.ReserveMemMiB
	}
	return ReserveMemMiB
}

// Candidates: alive, uncordoned nodes with room for `spec` by their own last counts (admission is the node's; this is the pre-check).
func (f *Fleet) Candidates(spec map[string]any, exclude map[string]bool) []*Node {
	out := []*Node{}
	prio, _ := spec["priority"].(string)
	for _, n := range f.All() {
		h := n.Health()
		if !n.Alive() || n.Cordoned() || h == nil || exclude[n.Name] {
			continue
		}
		freeVMs := NumOf(h, "max_vms") - NumOf(h, "vms_total") - 1
		freeMem := NumOf(h, "mem_budget_mib") - NumOf(h, "mem_mib_running") - specMem(spec)
		if freeVMs < 0 || freeMem < 0 {
			continue
		}
		if prio == PriorityBatch || prio == PriorityBackground {
			if freeVMs < n.reserveVMs() || freeMem < n.reserveMem() {
				continue
			}
		}
		out = append(out, n)
	}
	return out
}

// Score: how good a node is for this request (higher is better): a warm pool or template snapshot first, then the free share of VM slots and memory.
func Score(n *Node, wantKey string) float64 {
	h := n.Health()
	s := float64(Warmth(n, wantKey)) * 10
	if m := NumOf(h, "max_vms"); m > 0 {
		s += (m - NumOf(h, "vms_total")) / m
	}
	if b := NumOf(h, "mem_budget_mib"); b > 0 {
		s += (b - NumOf(h, "mem_mib_running")) / b
	}
	return s
}

// Warmth: 2 when the node has a warm pool for the template key, 1 when it has the template snapshot, else 0.
func Warmth(n *Node, key string) int {
	if key == "" {
		return 0
	}
	h := n.Health()
	if pools, ok := h["pools"].(map[string]any); ok {
		if v, ok := pools[key]; ok && v != nil {
			if f, isNum := v.(float64); !isNum || f > 0 {
				return 2
			}
		}
	}
	if snaps, ok := h["template_snapshots"].([]any); ok {
		for _, x := range snaps {
			if x == key {
				return 1
			}
		}
	}
	return 0
}

// Place: sample two nodes with room, take the one with fewer running VMs; a warm pool or a template snapshot for this request beats both.
func (f *Fleet) Place(spec map[string]any, wantKey string, exclude map[string]bool) *Node {
	cands := f.Candidates(spec, exclude)
	if len(cands) == 0 {
		return nil
	}
	rand.Shuffle(len(cands), func(i, j int) { cands[i], cands[j] = cands[j], cands[i] })
	sample := cands[:min(2, len(cands))]
	best := sample[0]
	for _, n := range sample[1:] {
		if Score(n, wantKey) > Score(best, wantKey) {
			best = n
		}
	}
	return best
}

// Load: the node's used share (0..1) of whichever is scarcer, VM slots or memory budget; balancing and drain reports go by it (D38 补: not slots alone).
func Load(n *Node) float64 { return loadOf(n.Health()) }

func loadOf(h map[string]any) float64 {
	l := 0.0
	if m := NumOf(h, "max_vms"); m > 0 {
		l = NumOf(h, "vms_total") / m
	}
	if b := NumOf(h, "mem_budget_mib"); b > 0 {
		if mem := NumOf(h, "mem_mib_running") / b; mem > l {
			l = mem
		}
	}
	return l
}

// LeastLoaded: the least loaded node with room (migration, evacuation).
func (f *Fleet) LeastLoaded(spec map[string]any, exclude map[string]bool) *Node {
	cands := f.Candidates(spec, exclude)
	if len(cands) == 0 {
		return nil
	}
	best := cands[0]
	for _, n := range cands[1:] {
		if Load(n) < Load(best) {
			best = n
		}
	}
	return best
}
