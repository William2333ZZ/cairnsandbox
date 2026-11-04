package abuse

import (
	"fmt"
	"strings"

	"fcnode/cp/internal/lifecycle"
)

// LifeSource reads the running sandboxes and their node-side metrics through the lifecycle context.
type LifeSource struct{ Life *lifecycle.Lifecycle }

func (s LifeSource) Running() []Sandbox {
	var out []Sandbox
	for _, sb := range s.Life.List(nil) {
		if st, _ := sb["status"].(string); st != "running" {
			continue
		}
		id, _ := sb["id"].(string)
		proj, _ := sb["project"].(string)
		vcpu, _ := sb["vcpu"].(float64)
		if id != "" {
			out = append(out, Sandbox{ID: id, Project: proj, VCPU: vcpu})
		}
	}
	return out
}

func (s LifeSource) Metrics(id string, since float64) ([]Point, error) {
	code, body := s.Life.NodeCall(id, "GET", fmt.Sprintf("/metrics?since=%.1f", since), nil)
	if code != 200 {
		return nil, fmt.Errorf("metrics %d", code)
	}
	arr, _ := body["points"].([]any)
	out := make([]Point, 0, len(arr))
	for _, x := range arr {
		m, ok := x.(map[string]any)
		if !ok {
			continue
		}
		p := Point{}
		p.T, _ = m["t"].(float64)
		if v, ok := m["cpu_pct"].(float64); ok {
			p.CPUPct = &v
		}
		p.EgrOut, _ = m["egress_out"].(float64)
		out = append(out, p)
	}
	return out, nil
}

var _ = strings.TrimSpace
