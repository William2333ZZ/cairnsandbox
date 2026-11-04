package fleet

import "testing"

func TestLoadIsTheScarcerOfSlotsAndMemory(t *testing.T) {
	if l := loadOf(map[string]any{"max_vms": 10.0, "vms_total": 2.0, "mem_budget_mib": 1000.0, "mem_mib_running": 900.0}); l != 0.9 {
		t.Fatalf("memory-bound node: %v", l)
	}
	if l := loadOf(map[string]any{"max_vms": 10.0, "vms_total": 8.0, "mem_budget_mib": 1000.0, "mem_mib_running": 100.0}); l != 0.8 {
		t.Fatalf("slot-bound node: %v", l)
	}
	if l := loadOf(nil); l != 0 {
		t.Fatalf("no health: %v", l)
	}
}

func TestReservesAreTheNodesOwnWhenSetElseTheFleetsDefault(t *testing.T) {
	n := &Node{Name: "a"}
	if n.reserveVMs() != ReserveVMs || n.reserveMem() != ReserveMemMiB {
		t.Fatal("defaults")
	}
	n = &Node{Name: "b", ReserveVMs: 5, ReserveMemMiB: 4096}
	if n.reserveVMs() != 5 || n.reserveMem() != 4096 {
		t.Fatal("own values")
	}
	// a batch request fits the default node but not one that keeps 5 slots for people
	f := &Fleet{byName: map[string]*Node{}}
	for _, x := range []*Node{{Name: "a"}, {Name: "b", ReserveVMs: 5, ReserveMemMiB: 4096}} {
		x.alive = true
		x.health = map[string]any{"max_vms": 8.0, "vms_total": 3.0, "mem_budget_mib": 16000.0, "mem_mib_running": 2000.0}
		f.byName[x.Name] = x
	}
	names := func(ns []*Node) map[string]bool {
		out := map[string]bool{}
		for _, n := range ns {
			out[n.Name] = true
		}
		return out
	}
	got := names(f.Candidates(map[string]any{"mem": 512.0, "priority": PriorityBatch}, nil))
	if !got["a"] || got["b"] {
		t.Fatalf("batch candidates %v", got)
	}
	got = names(f.Candidates(map[string]any{"mem": 512.0, "priority": PriorityInteractive}, nil))
	if !got["a"] || !got["b"] {
		t.Fatalf("interactive candidates %v", got)
	}
}
