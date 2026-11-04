package network

import "testing"

func TestRelaxesSeesLooserModesHostsRulesAndLimits(t *testing.T) {
	cur := map[string]any{"mode": "allowlist", "allow_hosts": []any{"pypi.org"}, "allow_direct": []any{}, "rules": []any{map[string]any{"name": "d", "match": map[string]any{"host": "x"}, "action": map[string]any{"allow": false}}}, "limits": map[string]any{"requests_per_min": 10.0, "max_bytes": 0.0}}
	cases := []struct {
		next  map[string]any
		relax bool
	}{
		{map[string]any{"allow_hosts": []any{}}, false},                        // tighter
		{map[string]any{"allow_hosts": []any{"pypi.org"}}, false},              // same
		{map[string]any{"allow_hosts": []any{"pypi.org", "github.com"}}, true}, // one more host
		{map[string]any{"mode": "closed"}, false},                              // tighter mode
		{map[string]any{"mode": "audited"}, true},                              // looser mode
		{map[string]any{"allow_direct": []any{"10.0.0.1"}}, true},              // a direct address
		{map[string]any{"rules": []any{}}, true},                               // the deny rule is dropped
		{map[string]any{"rules": []any{cur["rules"].([]any)[0]}}, false},       // same rule
		{map[string]any{"rules": []any{cur["rules"].([]any)[0], map[string]any{"name": "a", "match": map[string]any{"host": "y"}, "action": map[string]any{"allow": true}}}}, true}, // a new allow rule
		{map[string]any{"limits": map[string]any{"requests_per_min": 5.0}}, false},                                                                                                  // lower limit
		{map[string]any{"limits": map[string]any{"requests_per_min": 20.0}}, true},                                                                                                  // higher
		{map[string]any{"limits": map[string]any{"requests_per_min": 0.0}}, true},                                                                                                   // removed
		{map[string]any{"limits": map[string]any{"max_bytes": 100.0}}, false},                                                                                                       // a limit where there was none
	}
	for i, c := range cases {
		if got := Relaxes(cur, c.next); got != c.relax {
			t.Errorf("case %d: %v -> %v, want %v", i, c.next, got, c.relax)
		}
	}
	if !Relaxes(nil, map[string]any{"allow_hosts": []any{"a"}}) {
		t.Error("no current policy: anything allowed is a relaxation")
	}
}
