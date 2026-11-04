// Package images is context 4 (docs/002 沙箱侧的领域 镜像与模板): template snapshots built on one node and distributed to the others, Builds that pull an
// OCI image into a layer or commit a running sandbox into a named template (builds.go), and the catalogue of what the nodes hold.
package images

import (
	"fmt"
	"sort"
	"time"

	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/lifecycle"
	"fcnode/cp/internal/nodeclient"
)

type Auditor interface{ Write(map[string]any) }

type Images struct {
	fleet *fleet.Fleet
	nc    nodeclient.Client
	audit Auditor
}

func New(f *fleet.Fleet, audit Auditor) *Images { return &Images{fleet: f, audit: audit} }

// Everywhere: build the template snapshot on the least loaded node, publish it, import on the others.
func (im *Images) Everywhere(body map[string]any) lifecycle.Result {
	alive := im.fleet.Alive()
	if len(alive) == 0 {
		return lifecycle.Result{Code: 429, Body: map[string]any{"error": "no node alive"}}
	}
	sort.SliceStable(alive, func(i, j int) bool { return alive[i].Num("vms_running") < alive[j].Num("vms_running") })
	first := alive[0]
	t0 := time.Now()
	st, meta, err := im.nc.JSON(first, "POST", "/templates/snapshot", body, 600*time.Second)
	if err != nil || (st != 200 && st != 201) {
		return lifecycle.Result{Code: nz(st), Body: meta}
	}
	key, _ := meta["key"].(string)
	st, ex, err := im.nc.JSON(first, "POST", "/templates/"+key+"/export", nil, 600*time.Second)
	if err != nil || st != 200 {
		return lifecycle.Result{Code: nz(st), Body: map[string]any{"error": "export failed", "detail": ex}}
	}
	results := map[string]any{first.Name: "built"}
	for _, n := range alive {
		if n == first {
			continue
		}
		if fleet.Warmth(n, key) == 1 {
			results[n.Name] = "present"
			continue
		}
		st, r, err := im.nc.JSON(n, "POST", "/templates/import", map[string]any{"bundle": ex["name"]}, 600*time.Second)
		if err == nil && st == 201 {
			results[n.Name] = "imported"
		} else {
			results[n.Name] = fmt.Sprintf("error %d: %v", st, r)
		}
	}
	out := map[string]any{"key": key, "nodes": results, "s": lifecycle.Round3(time.Since(t0).Seconds()), "mem_mib": meta["mem_mib"]}
	ev := map[string]any{"op": "template_everywhere"}
	for k, v := range out {
		ev[k] = v
	}
	im.audit.Write(ev)
	return lifecycle.Result{Code: 200, Body: out}
}

func nz(st int) int {
	if st == 0 {
		return 502
	}
	return st
}
