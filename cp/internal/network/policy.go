// Package network is context 6 (docs/002 沙箱侧 6 网络与出口): the shape of a sandbox's NetworkPolicy (mode, allow_hosts, allow_direct, L7 rules) is
// checked here before it reaches a node, and the nodes' credential vaults are fed from here (names travel through the control plane, values are
// pushed straight to every node and never stored). The enforcement (nftables, the egress proxy, the audit ring) is on the node.
package network

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/nodeclient"
)

var Modes = []string{"closed", "allowlist", "audited"}
var nameRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]{0,39}$`)
var headerRe = regexp.MustCompile(`^[A-Za-z0-9-]+$`)

// Validate checks a `network` object from a create or update body; nil = fine.
func Validate(net map[string]any) error {
	if net == nil {
		return nil
	}
	if m, ok := net["mode"]; ok {
		s, _ := m.(string)
		found := false
		for _, x := range Modes {
			if x == s {
				found = true
			}
		}
		if !found {
			return errors.New("network.mode must be closed|allowlist|audited")
		}
	}
	for _, k := range []string{"allow_hosts", "allow_direct"} {
		if v, ok := net[k]; ok {
			arr, ok := v.([]any)
			if !ok {
				return fmt.Errorf("network.%s must be a list", k)
			}
			for _, x := range arr {
				if _, ok := x.(string); !ok {
					return fmt.Errorf("network.%s: entries are strings", k)
				}
			}
		}
	}
	if lim, ok := net["limits"]; ok && lim != nil {
		lm, ok := lim.(map[string]any)
		if !ok {
			return errors.New("network.limits must be an object")
		}
		for k, v := range lm {
			if k != "requests_per_min" && k != "max_bytes" {
				return fmt.Errorf("network.limits: unknown key %q", k)
			}
			f, ok := v.(float64)
			if !ok || f < 0 || f != float64(int64(f)) {
				return fmt.Errorf("network.limits.%s: a non-negative integer", k)
			}
		}
	}
	rules, _ := net["rules"].([]any)
	if net["rules"] != nil && rules == nil {
		return errors.New("network.rules must be a list")
	}
	for i, r := range rules {
		rule, ok := r.(map[string]any)
		if !ok {
			return fmt.Errorf("rule %d: must be an object", i)
		}
		name, _ := rule["name"].(string)
		if name == "" {
			name = fmt.Sprintf("rule%d", i)
		}
		if !nameRe.MatchString(name) {
			return fmt.Errorf("rule %d: bad name %q", i, name)
		}
		m, _ := rule["match"].(map[string]any)
		a, _ := rule["action"].(map[string]any)
		if a == nil {
			return fmt.Errorf("rule %s: action is required", name)
		}
		if s, ok := m["scheme"]; ok && s != "http" && s != "https" {
			return fmt.Errorf("rule %s: scheme must be http or https", name)
		}
		if p, ok := m["path"]; ok {
			ps, _ := p.(string)
			if !strings.HasPrefix(ps, "/") {
				return fmt.Errorf("rule %s: path must start with /", name)
			}
		}
		if mm, ok := m["method"]; ok {
			if _, ok := mm.([]any); !ok {
				return fmt.Errorf("rule %s: method must be a list", name)
			}
		}
		if _, ok := a["allow"].(bool); !ok {
			return fmt.Errorf("rule %s: action.allow is required", name)
		}
		if inj, ok := a["inject"]; ok && inj != nil {
			s, _ := inj.(string)
			if !nameRe.MatchString(s) {
				return fmt.Errorf("rule %s: bad credential name", name)
			}
			if !a["allow"].(bool) {
				return fmt.Errorf("rule %s: inject on a deny rule", name)
			}
		}
	}
	return nil
}

type Auditor interface{ Write(map[string]any) }

// Credentials pushes a named header value to every alive node's vault; the control plane keeps nothing but the audit line (with the name only).
type Credentials struct {
	fleet *fleet.Fleet
	nc    nodeclient.Client
	audit Auditor
}

func NewCredentials(f *fleet.Fleet, audit Auditor) *Credentials {
	return &Credentials{fleet: f, audit: audit}
}

func (c *Credentials) Set(name, header, value string) (map[string]any, error) {
	if !nameRe.MatchString(name) {
		return nil, errors.New("bad credential name")
	}
	if !headerRe.MatchString(header) {
		return nil, errors.New("bad header name")
	}
	if value == "" {
		return nil, errors.New("value required")
	}
	results := map[string]any{}
	for _, n := range c.fleet.Alive() {
		st, info, err := c.nc.JSON(n, "PUT", "/egress/credentials/"+name, map[string]any{"header": header, "value": value}, 10*time.Second)
		switch {
		case err != nil:
			results[n.Name] = "unreachable: " + err.Error()
		case st != 200:
			results[n.Name] = fmt.Sprintf("error %d: %v", st, info["error"])
		default:
			results[n.Name] = "ok"
		}
	}
	c.audit.Write(map[string]any{"op": "credential_set", "name": name, "header": header, "nodes": results})
	return map[string]any{"name": name, "header": header, "nodes": results}, nil
}

func (c *Credentials) Drop(name string) map[string]any {
	results := map[string]any{}
	for _, n := range c.fleet.Alive() {
		st, _, err := c.nc.JSON(n, "DELETE", "/egress/credentials/"+name, nil, 10*time.Second)
		if err != nil || st != 200 {
			results[n.Name] = "failed"
		} else {
			results[n.Name] = "ok"
		}
	}
	c.audit.Write(map[string]any{"op": "credential_drop", "name": name, "nodes": results})
	return map[string]any{"name": name, "nodes": results}
}

// Names: the union of what the nodes hold (a node that joined later may lack some: the result says which has what).
func (c *Credentials) Names() map[string][]string {
	out := map[string][]string{}
	for _, n := range c.fleet.Alive() {
		st, info, err := c.nc.JSON(n, "GET", "/egress/credentials", nil, 10*time.Second)
		if err != nil || st != 200 {
			out[n.Name] = nil
			continue
		}
		names := []string{}
		if arr, ok := info["credentials"].([]any); ok {
			for _, x := range arr {
				if s, ok := x.(string); ok {
					names = append(names, s)
				}
			}
		}
		out[n.Name] = names
	}
	return out
}
