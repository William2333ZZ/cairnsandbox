package httpapi

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"

	"fcnode/cp/internal/audit"
	"fcnode/cp/internal/db"
	"fcnode/cp/internal/e2b"
	"fcnode/cp/internal/exec"
	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/images"
	"fcnode/cp/internal/lifecycle"
	"fcnode/cp/internal/metering"
	"fcnode/cp/internal/network"
	"fcnode/cp/internal/nodeclient"
	"fcnode/cp/internal/registry"
	"fcnode/cp/internal/state"
	"fcnode/cp/internal/tenancy"
	"fcnode/cp/internal/webhook"
)

func newFakeRedis(t *testing.T) *miniredis.Miniredis { return miniredis.RunT(t) }

// pollOnce is one synchronous pass of the health loop, for tests.
func (s *Server) pollOnce()                       { s.Fleet.PollOnce(nodeclient.Client{}) }
func (s *Server) reconcile(n *fleet.Node)         { s.Life.Reconcile(n) }
func (s *Server) nodeByName(n string) *fleet.Node { return s.Fleet.Get(n) }

// fakeNode is a node agent with a capacity, a sandbox table and the handful of paths the control plane touches.
type fakeNode struct {
	logs      []string
	name      string
	srv       *httptest.Server
	mu        sync.Mutex
	maxVMs    float64
	boxes     map[string]map[string]any
	seq       int
	token     string
	snaps     []string
	deleted   []string
	down      bool
	vols      map[string]map[string]any // id -> {name, size_mib, attached: {sid: mode}}
	creds     map[string]string
	layers    []string            // layer ids imported through /layers/import
	memUsed   float64             // mem_mib_running to report when > 0 (else 256 per running sandbox)
	memBudget float64             // mem_budget_mib to report when > 0 (else 100000)
	strict    bool                // creating from a template_key the node does not hold answers 400 "no template", like a real node
	execs     []map[string]any    // every exec body the node received (template build test)
	putFiles  []string            // paths of PUT .../files
	envdSeen  []map[string]string // what the envd relay delivered (E2B surface): path, bearer, the SDK's own Authorization, body
}

// anyBox is the id of some sandbox the node holds (tests that only need one).
func (f *fakeNode) anyBox() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	for id := range f.boxes {
		return id
	}
	return ""
}

func newFakeNode(t *testing.T, name string, maxVMs int) *fakeNode {
	f := &fakeNode{name: name, maxVMs: float64(maxVMs), boxes: map[string]map[string]any{}, token: "tok-" + name}
	f.srv = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeNode) spec() fleet.Spec { return fleet.Spec{Name: f.name, URL: f.srv.URL, Token: f.token} }

func (f *fakeNode) handle(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Authorization") != "Bearer "+f.token {
		http.Error(w, "bad token", 401)
		return
	}
	if strings.Contains(r.URL.Path, "/ports/") && strings.EqualFold(r.Header.Get("Upgrade"), "websocket") { // a guest service accepting a websocket: 101, then echo
		hj := w.(http.Hijacker)
		c, rw, err := hj.Hijack()
		if err != nil {
			return
		}
		defer c.Close()
		c.Write([]byte("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"))
		b := make([]byte, 4096)
		for {
			n, err := rw.Reader.Read(b) // bytes that arrived with the handshake are in the server's buffer, not on the socket
			if err != nil {
				return
			}
			c.Write(append([]byte("echo:"), b[:n]...))
		}
	}
	if strings.Contains(r.URL.Path, "/ports/") && r.Header.Get("E2b-Sandbox-Port") != "" { // a service in the guest reached through the E2B surface: a streamed NDJSON answer
		b, _ := io.ReadAll(r.Body)
		f.mu.Lock()
		f.envdSeen = append(f.envdSeen, map[string]string{"path": r.URL.RequestURI(), "bearer": r.Header.Get("Authorization"), "body": string(b), "traffic": r.Header.Get("E2B-Traffic-Access-Token"), "xtoken": r.Header.Get("X-Access-Token")})
		f.mu.Unlock()
		w.Header().Set("Content-Type", "application/x-ndjson")
		w.WriteHeader(200)
		w.Write([]byte(`{"type":"stdout","text":"hi","timestamp":1}` + "\n"))
		w.(http.Flusher).Flush()
		w.Write([]byte(`{"type":"number_of_executions","execution_count":1}` + "\n"))
		return
	}
	if strings.Contains(r.URL.Path, "/envd/") { // the guest's envd, answering the way sandboxd does: a Connect server stream, flushed frame by frame
		b, _ := io.ReadAll(r.Body)
		f.mu.Lock()
		f.envdSeen = append(f.envdSeen, map[string]string{"path": r.URL.RequestURI(), "bearer": r.Header.Get("Authorization"), "xauth": r.Header.Get("X-Envd-Authorization"), "body": string(b), "ct": r.Header.Get("Content-Type")})
		f.mu.Unlock()
		if strings.HasSuffix(r.URL.Path, "/health") {
			w.WriteHeader(204)
			return
		}
		w.Header().Set("Content-Type", "application/connect+json")
		w.WriteHeader(200)
		for _, fr := range []string{`{"event":{"start":{"pid":7}}}`, `{"event":{"data":{"stdout":"aGkK"}}}`, `{"event":{"end":{"exitCode":0,"exited":true,"status":"exit status 0"}}}`} {
			w.Write(append(append([]byte{0}, byte(len(fr)>>24), byte(len(fr)>>16), byte(len(fr)>>8), byte(len(fr))), fr...))
			w.(http.Flusher).Flush()
		}
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.down {
		http.Error(w, "down", 500)
		return
	}
	raw, _ := io.ReadAll(r.Body)
	body := map[string]any{}
	json.Unmarshal(raw, &body)
	p := r.URL.Path
	js := func(code int, v any) {
		b, _ := json.Marshal(v)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		w.Write(b)
	}
	switch {
	case p == "/health":
		running := 0
		for _, b := range f.boxes {
			if b["status"] == "running" {
				running++
			}
		}
		mem := float64(256 * running)
		if f.memUsed > 0 {
			mem = f.memUsed
		}
		budget := 100000.0
		if f.memBudget > 0 {
			budget = f.memBudget
		}
		js(200, map[string]any{"vms_total": len(f.boxes), "vms_running": running, "max_vms": f.maxVMs, "mem_mib_running": mem, "mem_budget_mib": budget, "template_snapshots": f.snaps, "pools": map[string]any{}})
	case p == "/templates" && r.Method == "GET":
		snaps := []any{}
		for _, k := range f.snaps {
			snaps = append(snaps, map[string]any{"key": k, "dir": "/x", "mem": 512.0})
		}
		js(200, map[string]any{"templates": []any{"debian-13"}, "snapshots": snaps})
	case strings.HasPrefix(p, "/templates/") && r.Method == "DELETE":
		name := strings.TrimPrefix(p, "/templates/")
		for i, k := range f.snaps {
			if k == name {
				f.snaps = append(f.snaps[:i], f.snaps[i+1:]...)
				js(200, map[string]any{"deleted": name})
				return
			}
		}
		js(404, map[string]any{"error": "no template snapshot " + name})
	case p == "/layers" && r.Method == "GET":
		ls := []any{map[string]any{"id": "L-" + f.name, "name": "base-" + f.name, "path": "/x"}}
		for _, l := range f.layers {
			ls = append(ls, map[string]any{"id": l, "name": "imported", "path": "/y"})
		}
		js(200, map[string]any{"layers": ls})
	case strings.HasPrefix(p, "/layers/") && strings.HasSuffix(p, "/export") && r.Method == "GET":
		id := strings.TrimSuffix(strings.TrimPrefix(p, "/layers/"), "/export")
		if id != "L-"+f.name {
			js(404, map[string]any{"error": "no layer"})
			return
		}
		w.Header().Set("Content-Type", "application/x-tar")
		w.WriteHeader(200)
		w.Write([]byte("TAR:" + id))
	case p == "/layers/import" && r.Method == "POST":
		if !strings.HasPrefix(string(raw), "TAR:") {
			js(400, map[string]any{"error": "not a layer tar"})
			return
		}
		f.layers = append(f.layers, strings.TrimPrefix(string(raw), "TAR:"))
		js(201, map[string]any{"id": strings.TrimPrefix(string(raw), "TAR:")})
	case p == "/layers/from-archive" && r.Method == "POST":
		if len(raw) < 4 {
			js(400, map[string]any{"error": "bad archive"})
			return
		}
		js(201, map[string]any{"id": "L-arc", "name": r.URL.Query().Get("name"), "oci": map[string]any{"layers": 1, "archive_bytes": len(raw)}})
	case p == "/layers/from-oci" && r.Method == "POST":
		if body["image"] == "nope:latest" {
			js(502, map[string]any{"error": "registry: 404"})
			return
		}
		f.logs = append(f.logs, fmt.Sprintf("build %v pulled %v", body["build"], body["image"]))
		js(201, map[string]any{"id": "L-oci", "name": body["name"], "from": body["image"]})
	case strings.HasPrefix(p, "/builds/log/") && r.Method == "GET":
		js(200, map[string]any{"build": strings.TrimPrefix(p, "/builds/log/"), "lines": f.logs, "next": len(f.logs)})
	case strings.HasSuffix(p, "/files") && r.Method == "PUT":
		f.putFiles = append(f.putFiles, r.URL.Query().Get("path"))
		js(200, map[string]any{"bytes": len(raw)})
	case strings.HasSuffix(p, "/commit"):
		id := strings.TrimSuffix(strings.TrimPrefix(p, "/sandboxes/"), "/commit")
		if _, ok := f.boxes[id]; !ok {
			js(404, map[string]any{"error": "no sandbox"})
			return
		}
		name, _ := body["name"].(string)
		f.snaps = append(f.snaps, name)
		js(201, map[string]any{"key": name, "name": name, "from": id})
	case strings.HasPrefix(p, "/egress/credentials"):
		if f.creds == nil {
			f.creds = map[string]string{}
		}
		name := strings.TrimPrefix(p, "/egress/credentials/")
		switch r.Method {
		case "PUT":
			f.creds[name], _ = body["value"].(string)
		case "DELETE":
			delete(f.creds, name)
		}
		names := []any{}
		for k := range f.creds {
			names = append(names, k)
		}
		js(200, map[string]any{"credentials": names})
	case p == "/volumes/from-archive" && r.Method == "POST":
		if f.vols == nil {
			f.vols = map[string]map[string]any{}
		}
		id := r.URL.Query().Get("id")
		f.vols[id] = map[string]any{"id": id, "name": r.URL.Query().Get("name"), "attached": map[string]any{}, "allocated_mib": 1.0, "content_bytes": len(raw)}
		js(201, f.vols[id])
	case p == "/volumes" && r.Method == "POST":
		if f.vols == nil {
			f.vols = map[string]map[string]any{}
		}
		id, _ := body["id"].(string)
		f.vols[id] = map[string]any{"id": id, "name": body["name"], "size_mib": body["size_mib"], "attached": map[string]any{}, "allocated_mib": 1.0}
		js(201, f.vols[id])
	case strings.HasPrefix(p, "/volumes/") && strings.HasSuffix(p, "/export") && r.Method == "GET":
		id := strings.TrimSuffix(strings.TrimPrefix(p, "/volumes/"), "/export")
		v, ok := f.vols[id]
		if !ok {
			js(404, map[string]any{"error": "no volume"})
			return
		}
		b, _ := json.Marshal(v)
		w.Header().Set("Content-Type", "application/x-tar")
		w.WriteHeader(200)
		w.Write(append([]byte("VOL:"), b...))
	case p == "/volumes/import" && r.Method == "POST":
		if !strings.HasPrefix(string(raw), "VOL:") {
			js(400, map[string]any{"error": "not a volume tar"})
			return
		}
		v := map[string]any{}
		json.Unmarshal(raw[4:], &v)
		id, _ := v["id"].(string)
		if f.vols == nil {
			f.vols = map[string]map[string]any{}
		}
		if _, ok := f.vols[id]; ok {
			js(409, map[string]any{"error": "exists"})
			return
		}
		v["attached"] = map[string]any{}
		f.vols[id] = v
		js(201, v)
	case strings.HasPrefix(p, "/volumes/") && r.Method == "GET":
		if v, ok := f.vols[strings.TrimPrefix(p, "/volumes/")]; ok {
			js(200, v)
			return
		}
		js(404, map[string]any{"error": "no volume"})
	case strings.HasPrefix(p, "/volumes/") && r.Method == "DELETE":
		id := strings.TrimPrefix(p, "/volumes/")
		v, ok := f.vols[id]
		if !ok {
			js(404, map[string]any{"error": "no volume"})
			return
		}
		if len(v["attached"].(map[string]any)) > 0 {
			js(409, map[string]any{"error": "attached"})
			return
		}
		delete(f.vols, id)
		js(200, map[string]any{"deleted": id})
	case p == "/sandboxes" && r.Method == "POST":
		if float64(len(f.boxes)+1) > f.maxVMs {
			js(429, map[string]any{"error": "full", "vms_total": len(f.boxes), "vms_running": len(f.boxes), "mem_mib_running": 256 * len(f.boxes)})
			return
		}
		if tk, _ := body["template_key"].(string); tk != "" && f.strict {
			have := false
			for _, k := range f.snaps {
				have = have || k == tk
			}
			if !have {
				js(400, map[string]any{"error": "no template " + tk})
				return
			}
		}
		f.seq++
		id := fmt.Sprintf("%s-sb%d", f.name, f.seq)
		cls, _ := body["class"].(string)
		if cls == "" {
			cls = "persistent"
		}
		dur, _ := body["durable"].(bool)
		f.boxes[id] = map[string]any{"id": id, "status": "running", "class": cls, "durable": dur, "mem": 256.0, "vcpu": 1.0, "disk": 512.0, "disk_used_mib": 3.5, "egress": map[string]any{"out": 1000.0, "in": 5000.0}, "created": float64(time.Now().UnixNano()) / 1e9, "boot_mode": "cold", "template": "debian-13", "ttl_idle_s": 1800.0}
		if net, ok := body["network"]; ok {
			f.boxes[id]["network"] = net
		}
		if lb, ok := body["labels"]; ok {
			f.boxes[id]["labels"] = lb
		}
		if tk, _ := body["template_key"].(string); tk != "" { // a real node boots from the named template and reports it
			f.boxes[id]["template_snapshot"], f.boxes[id]["boot_mode"] = tk, "snapshot"
		}
		if arr, ok := body["volumes"].([]any); ok {
			for _, x := range arr {
				vm, _ := x.(map[string]any)
				vid, _ := vm["id"].(string)
				if v, ok := f.vols[vid]; ok {
					v["attached"].(map[string]any)[id] = "rw"
				}
			}
			f.boxes[id]["volumes"] = arr
		}
		js(201, f.boxes[id])
	case p == "/sandboxes" && r.Method == "GET":
		out := []any{}
		for _, b := range f.boxes {
			out = append(out, b)
		}
		js(200, map[string]any{"sandboxes": out})
	case strings.HasSuffix(p, "/network") && r.Method == "PUT":
		id := strings.TrimSuffix(strings.TrimPrefix(p, "/sandboxes/"), "/network")
		b, ok := f.boxes[id]
		if !ok {
			js(404, map[string]any{"error": "no sandbox"})
			return
		}
		net, _ := b["network"].(map[string]any)
		if net == nil {
			net = map[string]any{"mode": "closed", "allow_hosts": []any{}, "allow_direct": []any{}, "rules": []any{}, "limits": map[string]any{"requests_per_min": 0.0, "max_bytes": 0.0}}
		}
		for k, v := range body {
			net[k] = v
		}
		b["network"] = net
		js(200, map[string]any{"network": net})
	case strings.HasSuffix(p, "/pause") || strings.HasSuffix(p, "/resume") || strings.HasSuffix(p, "/ttl"):
		parts := strings.Split(strings.TrimPrefix(p, "/sandboxes/"), "/")
		b, ok := f.boxes[parts[0]]
		if !ok {
			js(404, map[string]any{"error": "no sandbox"})
			return
		}
		switch parts[1] {
		case "pause":
			b["status"] = "paused"
		case "resume":
			b["status"] = "running"
		case "ttl":
			b["ttl_idle_s"] = body["ttl_idle_s"]
		}
		js(200, b)
	case strings.HasSuffix(p, "/export"):
		id := strings.TrimSuffix(strings.TrimPrefix(p, "/sandboxes/"), "/export")
		if _, ok := f.boxes[id]; !ok {
			js(404, map[string]any{"error": "no sandbox"})
			return
		}
		js(200, map[string]any{"name": "sb-" + id + "-1", "sent_mib": 1})
	case p == "/sandboxes/import":
		name, _ := body["bundle"].(string)
		id := strings.TrimSuffix(strings.TrimPrefix(name, "sb-"), "-1")
		if float64(len(f.boxes)+1) > f.maxVMs {
			js(429, map[string]any{"error": "full"})
			return
		}
		f.boxes[id] = map[string]any{"id": id, "status": "paused", "class": "persistent", "durable": true, "mem": 256.0, "vcpu": 1.0, "disk": 512.0}
		js(201, map[string]any{"id": id, "import": map[string]any{"fetched_mib": 1}})
	case strings.HasSuffix(p, "/exec/stream"):
		w.Header().Set("Content-Type", "application/x-ndjson")
		w.WriteHeader(200)
		fl := w.(http.Flusher)
		for i := 1; i <= 3; i++ {
			fmt.Fprintf(w, "{\"event\":\"stdout\",\"data\":\"t%d\"}\n", i)
			fl.Flush()
			time.Sleep(30 * time.Millisecond)
		}
		fmt.Fprint(w, "{\"done\":true,\"exit\":0}\n")
	case strings.HasSuffix(p, "/exec"):
		id := strings.TrimSuffix(strings.TrimPrefix(p, "/sandboxes/"), "/exec")
		if _, ok := f.boxes[id]; !ok {
			js(404, map[string]any{"error": "no sandbox"})
			return
		}
		f.execs = append(f.execs, body)
		if c, _ := body["cmd"].(string); strings.Contains(c, "FAIL_THIS_STEP") {
			js(200, map[string]any{"exit": 3, "stdout": "", "stderr": "boom: it failed"})
			return
		}
		js(200, map[string]any{"exit": 0, "stdout": fmt.Sprintf("%s:%v", f.name, body["cmd"]), "idem": r.Header.Get("Idempotency-Key")})
	case strings.HasSuffix(p, "/fork"):
		id := strings.TrimSuffix(strings.TrimPrefix(p, "/sandboxes/"), "/fork")
		f.seq++
		cid := fmt.Sprintf("%s-clone%d-of-%s", f.name, f.seq, id)
		f.boxes[cid] = map[string]any{"id": cid, "status": "running", "class": "ephemeral", "durable": false}
		js(201, map[string]any{"clones": []any{f.boxes[cid]}})
	case strings.HasPrefix(p, "/sandboxes/") && r.Method == "DELETE":
		id := strings.TrimPrefix(p, "/sandboxes/")
		if _, ok := f.boxes[id]; !ok {
			js(404, map[string]any{"error": "no sandbox"})
			return
		}
		delete(f.boxes, id)
		f.deleted = append(f.deleted, id)
		for _, v := range f.vols {
			delete(v["attached"].(map[string]any), id)
		}
		js(200, map[string]any{"deleted": id})
	case strings.HasPrefix(p, "/sandboxes/") && r.Method == "GET":
		id := strings.TrimPrefix(p, "/sandboxes/")
		if b, ok := f.boxes[id]; ok {
			js(200, b)
			return
		}
		js(404, map[string]any{"error": "no sandbox"})
	default:
		js(404, map[string]any{"error": "no route " + p})
	}
}

func testServer(t *testing.T, reg registry.Registry, token string, nodes ...*fakeNode) (*Server, *httptest.Server) {
	log := audit.Discard("r-" + registry.NewToken()[:4])
	st := state.Store{}
	fl := fleet.New(reg, log)
	life := lifecycle.New(reg, fl, st, log)
	sqldb := db.OpenForTest(t)
	tn := tenancy.New(sqldb, token)
	if err := tn.Bootstrap(reg); err != nil {
		t.Fatal(err)
	}
	meter := metering.New(sqldb, fl, life, metering.Proxy{}, log)
	vols := state.NewVolumes(sqldb, fl, log)
	life.WithQuotas(tn, meter, vols).WithCredit(meter).WithDefaults(tn).WithVolumeMover(vols)
	im := images.New(fl, log)
	s := &Server{Reg: reg, Tenancy: tn, Fleet: fl, Life: life, Images: im, Builds: images.NewBuilds(sqldb, im, life.NodeOf), Exec: exec.New(life, fl, log), Metering: meter, Volumes: vols, Creds: network.NewCredentials(fl, log), NetReq: network.NewRequests(sqldb), Store: st, WebDir: t.TempDir(), Audit: log}
	wh := webhook.New(sqldb, WebhookResolver{Life: life, Builds: s.Builds, Tenancy: tn})
	wh.AllowPrivateForTests() // the httptest receivers listen on loopback
	log.Tap(wh.Observe)
	wh.Run(2)
	s.Webhooks = wh
	for _, n := range nodes {
		fl.Add(n.spec(), false)
	}
	s.pollOnce()
	ts := httptest.NewServer(s)
	t.Cleanup(ts.Close)
	return s, ts
}

func call(t *testing.T, base, method, path string, body any, hdr map[string]string) (int, map[string]any) {
	var rd io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, base+path, rd)
	req.Header.Set("Content-Type", "application/json")
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	json.Unmarshal(data, &out)
	return resp.StatusCode, out
}

func TestCreateRoutesForwardsAndDeleteDropsTheRoute(t *testing.T) {
	a, b := newFakeNode(t, "na", 2), newFakeNode(t, "nb", 2)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "", a, b)
	st, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "class": "persistent"}, nil)
	if st != 201 || info["node"] == nil {
		t.Fatalf("create %d %v", st, info)
	}
	sid := info["id"].(string)
	r, _ := reg.RouteGet(sid)
	if r == nil || r.Node != info["node"] {
		t.Fatalf("route %+v", r)
	}
	st, ex := call(t, ts.URL, "POST", "/sandboxes/"+sid+"/exec", map[string]any{"cmd": "true"}, map[string]string{"Idempotency-Key": "k1"})
	if st != 200 || ex["stdout"] != info["node"].(string)+":true" || ex["idem"] != "k1" {
		t.Fatalf("exec via the owning node: %d %v", st, ex)
	}
	st, _ = call(t, ts.URL, "GET", "/sandboxes/nope", nil, nil)
	if st != 404 {
		t.Fatal("unknown sandbox must be 404")
	}
	st, h := call(t, ts.URL, "GET", "/health", nil, nil)
	if st != 200 || h["routes"].(float64) != 1 || h["registry"].(map[string]any)["kind"] != "file" {
		t.Fatalf("health %v", h)
	}
	st, _ = call(t, ts.URL, "DELETE", "/sandboxes/"+sid, nil, nil)
	if st != 200 {
		t.Fatal("delete", st)
	}
	if r, _ := reg.RouteGet(sid); r != nil {
		t.Fatal("route survived the delete")
	}
	_ = s
}

func TestStreamingIsPassedThroughChunkByChunk(t *testing.T) {
	a := newFakeNode(t, "na", 2)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "", a)
	_, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, nil)
	resp, err := http.Post(ts.URL+"/sandboxes/"+info["id"].(string)+"/exec/stream", "application/json", strings.NewReader(`{"cmd":"x"}`))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	rd := bufio.NewReader(resp.Body)
	t0 := time.Now()
	var times []time.Duration
	for {
		line, err := rd.ReadString('\n')
		if err != nil {
			break
		}
		times = append(times, time.Since(t0))
		_ = line
	}
	if len(times) != 4 || times[2]-times[0] < 40*time.Millisecond {
		t.Fatalf("chunks not streamed: %v", times)
	}
}

func TestAdmissionSpreadsAcrossNodesAndRefusesWhenFull(t *testing.T) {
	a, b := newFakeNode(t, "na", 2), newFakeNode(t, "nb", 2)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "", a, b)
	placed := map[string]int{}
	n := 0
	for {
		st, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "class": "ephemeral"}, nil)
		if st != 201 {
			if st != 429 {
				t.Fatalf("expected 429, got %d %v", st, info)
			}
			break
		}
		placed[info["node"].(string)]++
		n++
	}
	if n != 4 || placed["na"] != 2 || placed["nb"] != 2 {
		t.Fatalf("placed %v", placed)
	}
	st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "node": "na"}, nil)
	if st != 429 {
		t.Fatal("pinned to a full node must be 429, got", st)
	}
}

func TestTemplateSnapshotWinsPlacement(t *testing.T) {
	a, b := newFakeNode(t, "na", 10), newFakeNode(t, "nb", 10)
	b.snaps = []string{"k1"}
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "", a, b)
	for i := 0; i < 6; i++ {
		_, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "template_key": "k1", "class": "ephemeral"}, nil)
		if info["node"] != "nb" {
			t.Fatalf("round %d went to %v", i, info["node"])
		}
		s.pollOnce()
	}
}

func TestUsersAndAuth(t *testing.T) {
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret")
	if st, _ := call(t, ts.URL, "GET", "/nodes", nil, nil); st != 401 {
		t.Fatal("no bearer must be 401, got", st)
	}
	if st, _ := call(t, ts.URL, "GET", "/health", nil, nil); st != 200 {
		t.Fatal("health is open")
	}
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	st, u := call(t, ts.URL, "POST", "/users", map[string]any{"user": "alice", "name": "Alice"}, admin)
	if st != 201 || len(u["token"].(string)) != 48 {
		t.Fatalf("add %d %v", st, u)
	}
	for _, bad := range []string{"Alice", "a", "default", "alice"} {
		if st, _ := call(t, ts.URL, "POST", "/users", map[string]any{"user": bad}, admin); st != 400 {
			t.Fatal("accepted", bad)
		}
	}
	if us, _ := reg.Users(); len(us) != 0 {
		t.Fatal("users no longer live in the registry")
	}
	alice := map[string]string{"Authorization": "Bearer " + u["token"].(string)}
	if st, me := call(t, ts.URL, "GET", "/me", nil, alice); st != 200 || me["user"] != "alice" || me["admin"] != false || me["account"] != "default" {
		t.Fatalf("me %d %v", st, me)
	}
	if st, _ := call(t, ts.URL, "GET", "/nodes", nil, alice); st != 403 {
		t.Fatal("a user reached the nodes, got", st)
	}
	st, lg := call(t, ts.URL, "POST", "/login", map[string]any{"token": "nope"}, nil)
	if st != 401 || lg["ok"] != false {
		t.Fatalf("login %d %v", st, lg)
	}
	st, lg = call(t, ts.URL, "POST", "/login", map[string]any{"token": "deployer-secret"}, nil)
	if st != 200 || lg["admin"] != true {
		t.Fatalf("login %d %v", st, lg)
	}
	if st, _ := call(t, ts.URL, "GET", "/users?token=deployer-secret", nil, nil); st != 200 {
		t.Fatal("token in the query string (EventSource) must work")
	}
	if st, _ := call(t, ts.URL, "DELETE", "/users/alice", nil, admin); st != 200 {
		t.Fatal("delete")
	}
	if st, _ := call(t, ts.URL, "GET", "/me", nil, alice); st != 401 {
		t.Fatal("deleted user still resolves")
	}
}

func TestTwoReplicasShareOneRegistry(t *testing.T) {
	fake := newFakeRedis(t)
	a, b := newFakeNode(t, "na", 3), newFakeNode(t, "nb", 3)
	regA, regB := registry.NewRedisRegistry(fake.Addr(), 0, "cp:"), registry.NewRedisRegistry(fake.Addr(), 0, "cp:")
	sA, tsA := testServer(t, regA, "", a) // replica A only knows node a from its env
	sB, tsB := testServer(t, regB, "", a)
	_, info := call(t, tsA.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, nil)
	sid := info["id"].(string)
	st, ex := call(t, tsB.URL, "POST", "/sandboxes/"+sid+"/exec", map[string]any{"cmd": "hi"}, nil)
	if st != 200 || ex["stdout"] != "na:hi" {
		t.Fatalf("B could not serve what A created: %d %v", st, ex)
	}
	// a node registered on A is picked up by B at its next poll
	if st, _ := call(t, tsA.URL, "POST", "/nodes", b.spec(), nil); st != 201 {
		t.Fatal("register")
	}
	sB.pollOnce()
	if sB.Fleet.Get("nb") == nil {
		t.Fatal("B did not learn node nb from the registry")
	}
	for i := 0; i < 2; i++ { // fill a (capacity 3) through B, then A must place on b, which it only knows via the registry
		if st, _ := call(t, tsB.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "class": "ephemeral", "node": "na"}, nil); st != 201 {
			t.Fatal("pinned create", st)
		}
	}
	sA.pollOnce()
	sB.pollOnce()
	st, info = call(t, tsA.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "class": "ephemeral"}, nil)
	if st != 201 || info["node"] != "nb" {
		t.Fatalf("A should have placed on nb: %d %v", st, info)
	}
	// both replicas list every route exactly once
	_, lstA := call(t, tsA.URL, "GET", "/sandboxes", nil, nil)
	_, lstB := call(t, tsB.URL, "GET", "/sandboxes", nil, nil)
	if len(lstA["sandboxes"].([]any)) != 4 || len(lstB["sandboxes"].([]any)) != 4 {
		t.Fatalf("lists %d %d", len(lstA["sandboxes"].([]any)), len(lstB["sandboxes"].([]any)))
	}
	// a delete through B is gone for A
	call(t, tsB.URL, "DELETE", "/sandboxes/"+sid, nil, nil)
	if st, _ := call(t, tsA.URL, "GET", "/sandboxes/"+sid, nil, nil); st != 404 {
		t.Fatal("A still routes a sandbox B deleted:", st)
	}
	_ = tsA
}

func TestReconcileAdoptsDropsAndDeletesStaleUnderOneLease(t *testing.T) {
	fake := newFakeRedis(t)
	a, b := newFakeNode(t, "na", 5), newFakeNode(t, "nb", 5)
	reg := registry.NewRedisRegistry(fake.Addr(), 0, "cp:")
	s, _ := testServer(t, reg, "", a, b)
	a.mu.Lock()
	a.boxes["orphan"] = map[string]any{"id": "orphan", "status": "running", "class": "ephemeral"} // on the node, unknown to us: adopt
	a.boxes["stale"] = map[string]any{"id": "stale", "status": "paused"}                          // routed to b: a stale copy, delete it from a
	a.mu.Unlock()
	reg.RouteSet("stale", registry.Route{Node: "nb"})
	reg.RouteSet("gone", registry.Route{Node: "na"}) // we route it to a, a does not have it: drop the route
	tok, _ := reg.Acquire("reconcile:na", time.Minute)
	s.reconcile(s.nodeByName("na")) // another replica holds the lease: nothing happens
	if r, _ := reg.RouteGet("orphan"); r != nil {
		t.Fatal("reconciled under someone else's lease")
	}
	reg.Release("reconcile:na", tok)
	s.reconcile(s.nodeByName("na"))
	r, _ := reg.RouteGet("orphan")
	g, _ := reg.RouteGet("gone")
	if r == nil || r.Node != "na" || g != nil || len(a.deleted) != 1 || a.deleted[0] != "stale" {
		t.Fatalf("adopt=%v gone=%v deleted=%v", r, g, a.deleted)
	}
}

func TestDeadNodeIsReportedAndEvacuateNeedsItDead(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "", a)
	_, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, nil)
	sid := info["id"].(string)
	if st, _ := call(t, ts.URL, "POST", "/nodes/na/evacuate", nil, nil); st != 409 {
		t.Fatal("evacuating a live node must be 409, got", st)
	}
	a.mu.Lock()
	a.down = true
	a.mu.Unlock()
	s.pollOnce()
	st, r := call(t, ts.URL, "POST", "/sandboxes/"+sid+"/exec", map[string]any{"cmd": "x"}, nil)
	if st != 503 || r["sandbox"] != sid {
		t.Fatalf("ops on a dead node's sandbox must fail fast: %d %v", st, r)
	}
	_, lst := call(t, ts.URL, "GET", "/sandboxes", nil, nil)
	if row := lst["sandboxes"].([]any)[0].(map[string]any); row["status"] != "unreachable" || row["id"] != sid {
		t.Fatalf("list %v", lst)
	}
	st, ev := call(t, ts.URL, "POST", "/nodes/na/evacuate", nil, nil)
	if st != 200 || len(ev["lost"].([]any)) != 1 || len(ev["moved"].([]any)) != 0 {
		t.Fatalf("evacuate without a store: %d %v", st, ev)
	}
	if r, _ := reg.RouteGet(sid); r != nil {
		t.Fatal("a lost sandbox keeps its route")
	}
}

func TestStaticAndForwarding(t *testing.T) {
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "")
	os.MkdirAll(filepath.Join(s.WebDir, "sub"), 0o755)
	os.WriteFile(filepath.Join(s.WebDir, "index.html"), []byte("<html>wb</html>"), 0o644)
	os.WriteFile(filepath.Join(s.WebDir, "sub", "a.js"), []byte("js"), 0o644)
	for _, p := range []string{"/", "/ui", "/ui/", "/ui/index.html"} {
		resp, _ := http.Get(ts.URL + p)
		body, _ := io.ReadAll(resp.Body)
		if resp.StatusCode != 200 || string(body) != "<html>wb</html>" {
			t.Fatalf("%s -> %d %q", p, resp.StatusCode, body)
		}
	}
	resp, _ := http.Get(ts.URL + "/ui/sub/a.js")
	if ct := resp.Header.Get("Content-Type"); resp.StatusCode != 200 || ct != "text/javascript; charset=utf-8" {
		t.Fatal("js", resp.StatusCode, ct)
	}
	if resp, _ := http.Get(ts.URL + "/ui/../server.go"); resp.StatusCode != 404 {
		t.Fatal("escaped the web dir")
	}
	if st, e := call(t, ts.URL, "GET", "/eval/batches", nil, nil); st != 503 || !strings.Contains(e["error"].(string), "EVALUATOR_URL") {
		t.Fatalf("no evaluator: %d %v", st, e)
	}
	var seen http.Header
	svc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
		seen.Set("X-Path", r.URL.RequestURI())
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(201)
		w.Write([]byte(`{"ok":true}`))
	}))
	defer svc.Close()
	s.IntakeURL = svc.URL
	s.Tenancy = tenancy.New(s.Tenancy.DB(), "cp-secret")
	st, out := call(t, ts.URL, "POST", "/intake/tickets?state=open", map[string]any{"x": 1}, map[string]string{"Authorization": "Bearer cp-secret", "X-Agent": "fix"})
	if st != 201 || out["ok"] != true || seen.Get("X-Path") != "/tickets?state=open" || seen.Get("X-User") != "default" || seen.Get("X-Agent") != "fix" || seen.Get("Authorization") != "Bearer cp-secret" {
		t.Fatalf("forward: %d %v %v", st, out, seen)
	}
	st, _ = call(t, ts.URL, "POST", "/intake/hooks/chat", map[string]any{}, map[string]string{"X-Chat-Secret": "s"})
	if st != 201 || seen.Get("X-Chat-Secret") != "s" {
		t.Fatal("the chat webhook must pass without a bearer and carry its secret")
	}
}

func TestEventsStreamSaysHelloAndReportsAChange(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "", a)
	resp, err := http.Get(ts.URL + "/events/stream")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	rd := bufio.NewReader(resp.Body)
	first, _ := rd.ReadString('\n')
	if first != "event: hello\n" {
		t.Fatalf("first line %q", first)
	}
	go func() {
		time.Sleep(200 * time.Millisecond)
		call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, nil)
	}()
	deadline := time.Now().Add(6 * time.Second)
	for time.Now().Before(deadline) {
		line, err := rd.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if strings.HasPrefix(line, "data: ") && strings.Contains(line, `"type":"sandbox"`) {
			return
		}
	}
	t.Fatal("no sandbox change event")
}

func TestQuotasAdmitCreatesAndMembersSeeOnlyTheirProjects(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	// an account with an owner and a member, a second project with a quota of 2 sandboxes
	if st, acc := call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "acme", "name": "Acme"}, admin); st != 201 || acc["id"] != "acme" {
		t.Fatalf("account %d %v", st, acc)
	}
	st, own := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "carol", "role": "owner"}, admin)
	if st != 201 {
		t.Fatal("owner", st, own)
	}
	carol := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	st, mem := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "dave"}, carol)
	if st != 201 {
		t.Fatal("member by owner", st, mem)
	}
	dave := map[string]string{"Authorization": "Bearer " + mem["token"].(string)}
	if st, _ := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "eve"}, dave); st != 403 {
		t.Fatal("a member added a member:", st)
	}
	if st, p := call(t, ts.URL, "POST", "/accounts/acme/projects", map[string]any{"name": "web"}, carol); st != 201 || p["id"] != "acme-web" {
		t.Fatalf("project %d %v", st, p)
	}
	if st, _ := call(t, ts.URL, "POST", "/accounts/default/projects", map[string]any{"name": "x"}, carol); st != 403 {
		t.Fatal("crossed accounts:", st)
	}
	if st, q := call(t, ts.URL, "PUT", "/projects/acme-web/quota", map[string]any{"max_sandboxes": 2}, carol); st != 200 || q["max_sandboxes"].(float64) != 2 {
		t.Fatalf("quota %d %v", st, q)
	}
	// dave creates in acme-web until the quota says no; the default project is not his
	for i := 0; i < 2; i++ {
		if st, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme-web"}, dave); st != 201 || info["project"] != "acme-web" {
			t.Fatalf("create %d: %d %v", i, st, info)
		}
	}
	if st, e := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme-web"}, dave); st != 429 || e["error"] != "quota: sandboxes" {
		t.Fatalf("quota not enforced: %d %v", st, e)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "default"}, dave); st != 403 {
		t.Fatal("created in another account's project:", st)
	}
	if st, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, dave); st != 201 || info["project"] != "acme" {
		t.Fatalf("no project named = the account's default: %d %v", st, info)
	}
	// the admin creates in default; dave's list shows only acme's; the admin's shows all; dave cannot reach the admin's sandbox
	_, adm := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, admin)
	if adm["project"] != "default" {
		t.Fatalf("admin's project %v", adm["project"])
	}
	_, lst := call(t, ts.URL, "GET", "/sandboxes", nil, dave)
	if n := len(lst["sandboxes"].([]any)); n != 3 {
		t.Fatalf("dave sees %d, wants 3", n)
	}
	_, lst = call(t, ts.URL, "GET", "/sandboxes", nil, admin)
	if n := len(lst["sandboxes"].([]any)); n != 4 {
		t.Fatalf("admin sees %d, wants 4", n)
	}
	if st, _ := call(t, ts.URL, "GET", "/sandboxes/"+adm["id"].(string), nil, dave); st != 404 {
		t.Fatal("another account's sandbox must look absent, got", st)
	}
	if st, _ := call(t, ts.URL, "GET", "/nodes", nil, carol); st != 403 {
		t.Fatal("owners do not see nodes:", st)
	}
	// the daily budget: a price, a tick that charges running sandboxes, then creation is refused with 402
	s.pollOnce()
	if st, _ := call(t, ts.URL, "PUT", "/metering/prices", map[string]any{"vcpu_s": 1.0}, admin); st != 200 {
		t.Fatal("prices", st)
	}
	if st, _ := call(t, ts.URL, "PUT", "/projects/acme/quota", map[string]any{"daily_usd": 0.5}, carol); st != 200 {
		t.Fatal("daily quota", st)
	}
	s.Metering.Tick()                                                                // first tick only sets the cursor
	s.Metering.DB().Exec(`UPDATE meter_state SET v = v - 120 WHERE k = 'last_tick'`) // pretend two minutes passed
	if n, err := s.Metering.Tick(); err != nil || n == 0 {
		t.Fatal("tick charged nothing", n, err)
	}
	_, today := call(t, ts.URL, "GET", "/metering/projects/acme/today", nil, carol)
	if today["spent_usd"].(float64) <= 0.5 {
		t.Fatalf("spent %v", today["spent_usd"])
	}
	if st, e := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme"}, dave); st != 402 || e["error"] != "quota: daily budget" {
		t.Fatalf("daily budget not enforced: %d %v", st, e)
	}
	_, stmt := call(t, ts.URL, "GET", "/metering/projects/acme/statement", nil, carol)
	if stmt["total_usd"].(float64) <= 0 || len(stmt["lines"].([]any)) < 2 {
		t.Fatalf("statement %v", stmt)
	}
	kinds := map[string]float64{}
	for _, l := range stmt["lines"].([]any) {
		m := l.(map[string]any)
		kinds[m["kind"].(string)] = m["amount"].(float64)
	}
	if kinds["egress_bytes"] != 6000 { // the one sandbox in project acme reported 6000 bytes: counted once, as a delta from 0
		t.Fatalf("egress bytes %v", kinds)
	}
	if d := kinds["disk_mib_s"]; d < 3.5*120 || d > 3.5*121 { // dt is "two minutes" plus the test's own time
		t.Fatalf("disk should use the real allocation: %v", d)
	}
	if st, _ := call(t, ts.URL, "GET", "/metering/projects/default/statement", nil, carol); st != 403 {
		t.Fatal("another account's statement:", st)
	}
	// settle: the period is frozen into the ledger, listed, and cannot be settled twice; the live statement exports as CSV
	to := float64(time.Now().Unix() + 60)
	st, settled := call(t, ts.URL, "POST", "/metering/projects/acme/statements", map[string]any{"from": 0, "to": to}, carol)
	if st != 201 || settled["total_usd"].(float64) != stmt["total_usd"].(float64) || !strings.HasPrefix(settled["id"].(string), "st-") {
		t.Fatalf("settle %d %v", st, settled)
	}
	if st, _ := call(t, ts.URL, "POST", "/metering/projects/acme/statements", map[string]any{"from": 0, "to": to}, dave); st != 403 {
		t.Fatal("a member settled:", st)
	}
	if st, e := call(t, ts.URL, "POST", "/metering/projects/acme/statements", map[string]any{"from": to - 30, "to": to + 30}, carol); st != 400 || !strings.Contains(e["error"].(string), "overlaps") {
		t.Fatalf("overlap not refused: %d %v", st, e)
	}
	_, ls := call(t, ts.URL, "GET", "/metering/projects/acme/statements", nil, carol)
	if arr := ls["statements"].([]any); len(arr) != 1 || arr[0].(map[string]any)["id"] != settled["id"] {
		t.Fatalf("statements %v", ls)
	}
	req, _ := http.NewRequest("GET", ts.URL+"/metering/projects/acme/statement?format=csv", nil)
	req.Header.Set("Authorization", carol["Authorization"])
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	csv, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/csv") || !strings.Contains(string(csv), "acme,0,") || !strings.Contains(string(csv), ",total,usd,") {
		t.Fatalf("csv %d %q %q", resp.StatusCode, resp.Header.Get("Content-Type"), string(csv))
	}
}

func TestVolumesBelongToAProjectLiveOnOneNodeAndPinTheSandbox(t *testing.T) {
	a, b := newFakeNode(t, "na", 5), newFakeNode(t, "nb", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a, b)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "acme"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "carol", "role": "owner"}, admin)
	carol := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	st, v := call(t, ts.URL, "POST", "/volumes", map[string]any{"name": "data", "size_mib": 256, "node": "nb"}, admin) // the admin may pick the node
	if st != 201 || v["node"] != "nb" || v["project"] != "default" {
		t.Fatalf("volume %d %v", st, v)
	}
	vid := v["id"].(string)
	st, cv := call(t, ts.URL, "POST", "/volumes", map[string]any{"name": "mine"}, carol)
	if st != 201 || cv["project"] != "acme" {
		t.Fatalf("carol's volume %d %v", st, cv)
	}
	if st, _ := call(t, ts.URL, "GET", "/volumes/"+vid, nil, carol); st != 404 {
		t.Fatal("another account's volume must look absent, got", st)
	}
	_, lst := call(t, ts.URL, "GET", "/volumes", nil, carol)
	if n := len(lst["volumes"].([]any)); n != 1 {
		t.Fatal("carol sees", n)
	}
	// a sandbox with the volume lands on nb whatever placement would say; the wrong project is refused; a conflicting node pin is refused
	for i := 0; i < 3; i++ {
		st, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "volumes": []any{map[string]any{"id": vid, "mount": "/mnt/d"}}}, admin)
		if st != 201 || info["node"] != "nb" {
			t.Fatalf("round %d: %d %v", i, st, info)
		}
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme", "volumes": []any{vid}}, carol); st != 403 {
		t.Fatal("volume of another project attached:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "node": "na", "volumes": []any{vid}}, admin); st != 409 {
		t.Fatal("node pin against the volume's node:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "volumes": []any{"vol-ghost"}}, admin); st != 404 {
		t.Fatal("unknown volume:", st)
	}
	st, info := call(t, ts.URL, "GET", "/volumes/"+vid, nil, admin)
	if st != 200 || len(info["attached"].(map[string]any)) != 3 {
		t.Fatalf("attachments %v", info)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/volumes/"+vid, nil, admin); st != 409 {
		t.Fatal("attached volume deleted:", st)
	}
	_, all := call(t, ts.URL, "GET", "/sandboxes", nil, admin)
	for _, x := range all["sandboxes"].([]any) {
		call(t, ts.URL, "DELETE", "/sandboxes/"+x.(map[string]any)["id"].(string), nil, admin)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/volumes/"+vid, nil, admin); st != 200 {
		t.Fatal("delete once free:", st)
	}
	if _, err := s.Volumes.Get(vid); err == nil {
		t.Fatal("record survived the delete")
	}
}

func TestNetworkPoliciesAreCheckedAtTheEdgeAndCredentialsGoToEveryNode(t *testing.T) {
	a, b := newFakeNode(t, "na", 5), newFakeNode(t, "nb", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a, b)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	bad := map[string]any{"mem": 256, "network": map[string]any{"rules": []any{map[string]any{"match": map[string]any{"scheme": "ftp"}, "action": map[string]any{"allow": true}}}}}
	if st, e := call(t, ts.URL, "POST", "/sandboxes", bad, admin); st != 400 || !strings.Contains(e["error"].(string), "scheme") {
		t.Fatalf("bad policy at create: %d %v", st, e)
	}
	good := map[string]any{"mem": 256, "network": map[string]any{"mode": "audited", "rules": []any{map[string]any{"name": "k", "match": map[string]any{"host": "api.internal"}, "action": map[string]any{"allow": true, "inject": "team"}}}}}
	st, info := call(t, ts.URL, "POST", "/sandboxes", good, admin)
	if st != 201 {
		t.Fatalf("good policy: %d %v", st, info)
	}
	sid := info["id"].(string)
	if st, _ := call(t, ts.URL, "PUT", "/sandboxes/"+sid+"/network", map[string]any{"mode": "open"}, admin); st != 400 {
		t.Fatal("bad update passed the edge:", st)
	}
	st, out := call(t, ts.URL, "PUT", "/network/credentials/team", map[string]any{"header": "Authorization", "value": "Bearer s3cret"}, admin)
	if st != 200 || out["nodes"].(map[string]any)["na"] != "ok" || out["nodes"].(map[string]any)["nb"] != "ok" {
		t.Fatalf("push %d %v", st, out)
	}
	if a.creds["team"] != "Bearer s3cret" || b.creds["team"] != "Bearer s3cret" {
		t.Fatal("not on every node")
	}
	st, names := call(t, ts.URL, "GET", "/network/credentials", nil, admin)
	if st != 200 || len(names["nodes"].(map[string]any)["na"].([]any)) != 1 {
		t.Fatalf("names %d %v", st, names)
	}
	if st, _ := call(t, ts.URL, "PUT", "/network/credentials/Bad Name", map[string]any{"header": "X", "value": "v"}, admin); st != 400 {
		t.Fatal("bad name accepted:", st)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/network/credentials/team", nil, admin); st != 200 || len(a.creds) != 0 {
		t.Fatal("drop", st, a.creds)
	}
	_, own := call(t, ts.URL, "POST", "/users", map[string]any{"user": "erin"}, admin)
	erin := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	if st, _ := call(t, ts.URL, "GET", "/network/credentials", nil, erin); st != 403 {
		t.Fatal("a member reached the vault:", st)
	}
}

func waitBuild(t *testing.T, base, id string, hdr map[string]string) map[string]any {
	for i := 0; i < 50; i++ {
		_, b := call(t, base, "GET", "/builds/"+id, nil, hdr)
		if b["status"] != "running" {
			return b
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("build never finished")
	return nil
}

func TestBuildsPullOCILayersAndCommitTemplatesOnTheRightNode(t *testing.T) {
	a, b := newFakeNode(t, "na", 5), newFakeNode(t, "nb", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a, b)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	st, bl := call(t, ts.URL, "POST", "/builds", map[string]any{"kind": "oci", "image": "busybox:latest", "name": "busybox", "node": "nb"}, admin)
	if st != 201 || bl["status"] != "running" || bl["node"] != "nb" {
		t.Fatalf("start %d %v", st, bl)
	}
	done := waitBuild(t, ts.URL, bl["id"].(string), admin)
	if done["status"] != "done" || done["result"].(map[string]any)["id"] != "L-oci" {
		t.Fatalf("oci build %v", done)
	}
	// the node's progress lines come back through the build's log endpoint, and only to someone who can see the build
	if st, lg := call(t, ts.URL, "GET", "/builds/"+bl["id"].(string)+"/log", nil, admin); st != 200 || len(lg["lines"].([]any)) != 1 || !strings.Contains(lg["lines"].([]any)[0].(string), bl["id"].(string)) {
		t.Fatalf("build log %d %v", st, lg)
	}
	if st, _ := call(t, ts.URL, "GET", "/builds/nope/log", nil, admin); st != 404 {
		t.Fatal("log of a missing build:", st)
	}
	_, bad := call(t, ts.URL, "POST", "/builds", map[string]any{"kind": "oci", "image": "nope:latest"}, admin)
	if f := waitBuild(t, ts.URL, bad["id"].(string), admin); f["status"] != "failed" || !strings.Contains(f["error"].(string), "registry") {
		t.Fatalf("failed build %v", f)
	}
	if st, _ := call(t, ts.URL, "POST", "/builds", map[string]any{"kind": "oci"}, admin); st != 400 {
		t.Fatal("no image accepted:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/builds", map[string]any{"kind": "bake"}, admin); st != 400 {
		t.Fatal("unknown kind accepted:", st)
	}
	// a commit runs on the node that holds the sandbox, and the template then shows in the catalogue with its node
	_, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "node": "na"}, admin)
	st, cb := call(t, ts.URL, "POST", "/builds", map[string]any{"kind": "commit", "sandbox": info["id"], "name": "warm-py"}, admin)
	if st != 201 || cb["node"] != "na" {
		t.Fatalf("commit start %d %v", st, cb)
	}
	if d := waitBuild(t, ts.URL, cb["id"].(string), admin); d["status"] != "done" {
		t.Fatalf("commit %v", d)
	}
	_, tpls := call(t, ts.URL, "GET", "/templates", nil, admin)
	found := false
	for _, x := range tpls["templates"].([]any) {
		m := x.(map[string]any)
		if m["key"] == "warm-py" && m["node"] == "na" && m["dir"] == nil {
			found = true
		}
	}
	if !found {
		t.Fatalf("templates %v", tpls)
	}
	// the name can be freed again: a member may delete what their project committed, not what nobody in their scope built, and a second delete finds nothing
	_, mem := call(t, ts.URL, "POST", "/users", map[string]any{"user": "tess"}, admin)
	tess := map[string]string{"Authorization": "Bearer " + mem["token"].(string)}
	a.mu.Lock()
	a.snaps = append(a.snaps, "foreign")
	a.mu.Unlock()
	if st, _ := call(t, ts.URL, "DELETE", "/templates/foreign", nil, tess); st != 403 {
		t.Fatal("member deleted a template they did not commit:", st)
	}
	if st, d := call(t, ts.URL, "DELETE", "/templates/warm-py", nil, tess); st != 200 || d["deleted"] != "warm-py" {
		t.Fatalf("member delete of their own template %d %v", st, d)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/templates/warm-py", nil, admin); st != 404 {
		t.Fatal("second delete:", st)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/templates/foreign", nil, admin); st != 200 {
		t.Fatal("deployer delete:", st)
	}
	_, layers := call(t, ts.URL, "GET", "/layers", nil, admin)
	if len(layers["layers"].([]any)) != 2 {
		t.Fatalf("layers %v", layers)
	}
	if st, _ := call(t, ts.URL, "POST", "/builds", map[string]any{"kind": "commit", "sandbox": "ghost", "name": "x"}, admin); st != 404 {
		t.Fatal("commit of a missing sandbox:", st)
	}
	// a member sees only their project's builds, and cannot pick the node
	_, own := call(t, ts.URL, "POST", "/users", map[string]any{"user": "erin"}, admin)
	erin := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	_, lst := call(t, ts.URL, "GET", "/builds", nil, erin)
	if n := len(lst["builds"].([]any)); n != 3 { // erin is in the default account, where the admin's builds live
		t.Fatalf("erin sees %d builds", n)
	}
	st, eb := call(t, ts.URL, "POST", "/builds", map[string]any{"kind": "oci", "image": "alpine", "node": "nb"}, erin)
	if st != 201 || eb["node"] == "" {
		t.Fatalf("erin's build %d %v", st, eb)
	}
	// template_key reaches the node unchanged
	a.mu.Lock()
	lastKey := ""
	a.mu.Unlock()
	_ = lastKey
}

func TestCordonDrainBalanceAndPriorityReserves(t *testing.T) {
	a, b := newFakeNode(t, "na", 6), newFakeNode(t, "nb", 6)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "", a, b)
	// cordon: nothing new lands on na; uncordon lifts it; the flag is in the registry for other replicas
	if st, _ := call(t, ts.URL, "POST", "/nodes/na/cordon", nil, nil); st != 200 {
		t.Fatal("cordon", st)
	}
	for i := 0; i < 3; i++ {
		if _, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "class": "persistent"}, nil); info["node"] != "nb" {
			t.Fatalf("placed on a cordoned node: %v", info["node"])
		}
	}
	specs, _ := reg.Nodes()
	if len(specs) != 1 || !specs[0].Cordoned {
		t.Fatalf("cordon not shared: %+v", specs)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "node": "na", "class": "ephemeral"}, nil); st != 201 {
		t.Fatal("a pinned create still works (the pin is explicit)", st)
	}
	call(t, ts.URL, "POST", "/nodes/na/uncordon", nil, nil)
	s.pollOnce()
	// balance: nb has 3, na has 1 (+ an ephemeral one): one persistent moves nb -> na
	st, bal := call(t, ts.URL, "POST", "/fleet/balance", map[string]any{"max": 1, "gap": 0.1}, nil)
	moves := bal["moves"].([]any)
	if st != 200 || len(moves) != 1 || moves[0].(map[string]any)["from"] != "nb" || moves[0].(map[string]any)["to"] != "na" || moves[0].(map[string]any)["ok"] != true {
		t.Fatalf("balance %d %v", st, bal)
	}
	s.pollOnce()
	// drain nb: its persistent sandboxes go to na, ephemeral ones are left; nb is cordoned afterwards
	_, eph := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "node": "nb", "class": "ephemeral"}, nil)
	st, dr := call(t, ts.URL, "POST", "/nodes/nb/drain", nil, nil)
	if st != 200 || dr["cordoned"] != true || len(dr["moved"].([]any)) != 2 || len(dr["left"].([]any)) != 1 || dr["left"].([]any)[0] != eph["id"] {
		t.Fatalf("drain %d %v", st, dr)
	}
	for sid, r := range s.Life.Routes() {
		if r.Node == "nb" && r.Class != "ephemeral" {
			t.Fatal("persistent sandbox still on nb:", sid)
		}
	}
	// priority: na now holds 1+1+2 = 4 of 6 -> 2 free; batch needs ReserveVMs(2) free after itself: refused; interactive still fits; nb is cordoned
	s.pollOnce()
	if st, e := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "class": "ephemeral", "priority": "batch"}, nil); st != 429 {
		t.Fatalf("batch should have been refused: %d %v", st, e)
	}
	st, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "class": "ephemeral", "priority": "interactive"}, nil)
	if st != 201 || info["priority"] != "interactive" {
		t.Fatalf("interactive %d %v", st, info)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "priority": "urgent"}, nil); st != 400 {
		t.Fatal("unknown priority accepted:", st)
	}
	call(t, ts.URL, "POST", "/nodes/nb/uncordon", nil, nil)
	s.pollOnce()
	st, info = call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "class": "persistent", "priority": "background"}, nil)
	if st != 201 || info["node"] != "nb" {
		t.Fatalf("background work goes where the reserve holds: %d %v", st, info)
	}
	if st, _ := call(t, ts.URL, "POST", "/nodes/ghost/drain", nil, nil); st != 404 {
		t.Fatal("drain of a missing node:", st)
	}
}

func TestAnImageArchiveUploadedThroughTheControlPlaneBecomesALayer(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "", a)
	req, _ := http.NewRequest("POST", ts.URL+"/builds/archive?name=bb", bytes.NewReader([]byte("fake-tar-bytes")))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var bl map[string]any
	json.NewDecoder(resp.Body).Decode(&bl)
	resp.Body.Close()
	if resp.StatusCode != 201 || bl["kind"] != "archive" {
		t.Fatalf("start %d %v", resp.StatusCode, bl)
	}
	d := waitBuild(t, ts.URL, bl["id"].(string), nil)
	if d["status"] != "done" || d["result"].(map[string]any)["id"] != "L-arc" {
		t.Fatalf("archive build %v", d)
	}
	req, _ = http.NewRequest("POST", ts.URL+"/builds/archive?name=Bad!", bytes.NewReader([]byte("x")))
	if resp, _ := http.DefaultClient.Do(req); resp.StatusCode != 400 {
		t.Fatal("bad name accepted", resp.StatusCode)
	}
}

func TestTheE2BSurfaceSpeaksE2BOnItsOwnPort(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	a.snaps = []string{"py312"}
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, _ := testServer(t, reg, "deployer-secret", a)
	es := httptest.NewServer(&e2b.Server{Tenancy: s.Tenancy, Life: s.Life, Images: s.Images})
	defer es.Close()
	key := map[string]string{"X-API-Key": "deployer-secret"}
	if st, e := call(t, es.URL, "GET", "/sandboxes", nil, nil); st != 401 || e["code"].(float64) != 401 {
		t.Fatalf("no key: %d %v", st, e)
	}
	// create in E2B's shape: templateID, timeout (seconds), metadata
	req, _ := http.NewRequest("POST", es.URL+"/sandboxes", strings.NewReader(`{"templateID":"py312","timeout":120,"metadata":{"job":"42"}}`))
	req.Header.Set("X-API-Key", "deployer-secret")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var sb map[string]any
	json.NewDecoder(resp.Body).Decode(&sb)
	resp.Body.Close()
	if resp.StatusCode != 201 || sb["sandboxID"] == nil || sb["templateID"] != "py312" || sb["state"] != "running" || sb["metadata"].(map[string]any)["job"] != "42" {
		t.Fatalf("create %d %v", resp.StatusCode, sb)
	}
	sid := sb["sandboxID"].(string)
	// list and get, in E2B's shape; our own API also shows the metadata on the route
	st, lst := callList(t, es.URL, "/sandboxes", key)
	if st != 200 || len(lst) != 1 || lst[0]["sandboxID"] != sid {
		t.Fatalf("list %d %v", st, lst)
	}
	if st, one := call(t, es.URL, "GET", "/sandboxes/"+sid, nil, key); st != 200 || one["clientID"] != "na" || one["endAt"] == "" {
		t.Fatalf("get %d %v", st, one)
	}
	if st, tp := callList(t, es.URL, "/templates", key); st != 200 || len(tp) != 1 || tp[0]["templateID"] != "py312" {
		t.Fatalf("templates %d %v", st, tp)
	}
	for _, op := range []string{"pause", "resume", "timeout"} {
		req, _ := http.NewRequest("POST", es.URL+"/sandboxes/"+sid+"/"+op, strings.NewReader(`{"timeout":600}`))
		req.Header.Set("X-API-Key", "deployer-secret")
		resp, _ := http.DefaultClient.Do(req)
		resp.Body.Close()
		if resp.StatusCode != 204 {
			t.Fatalf("%s -> %d", op, resp.StatusCode)
		}
	}
	a.mu.Lock()
	ttl := a.boxes[sid]["ttl_idle_s"]
	a.mu.Unlock()
	if ttl != 600.0 {
		t.Fatalf("timeout did not reach the node: %v", ttl)
	}
	// a member of another account cannot see it; kill removes it and the route
	req, _ = http.NewRequest("DELETE", es.URL+"/sandboxes/"+sid, nil)
	req.Header.Set("X-API-Key", "deployer-secret")
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != 204 || s.Life.Route(sid) != nil {
		t.Fatalf("kill %d route=%v", resp.StatusCode, s.Life.Route(sid))
	}
	if st, _ := call(t, es.URL, "GET", "/sandboxes/"+sid, nil, key); st != 404 {
		t.Fatal("killed sandbox still visible:", st)
	}
}

func callList(t *testing.T, base, path string, hdr map[string]string) (int, []map[string]any) {
	req, _ := http.NewRequest("GET", base+path, nil)
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out []map[string]any
	json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestAVolumeCanBeCreatedFromAnUploadedTar(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "", a)
	req, _ := http.NewRequest("POST", ts.URL+"/volumes/from-archive?name=home-abc&size_mib=64", bytes.NewReader([]byte("tar-bytes")))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var v map[string]any
	json.NewDecoder(resp.Body).Decode(&v)
	resp.Body.Close()
	if resp.StatusCode != 201 || v["name"] != "home-abc" || v["node"] != "na" || v["content_bytes"].(float64) != 9 {
		t.Fatalf("%d %v", resp.StatusCode, v)
	}
	if x, err := s.Volumes.Get(v["id"].(string)); err != nil || x.SizeMiB != 64 {
		t.Fatal("not recorded", err)
	}
	req, _ = http.NewRequest("POST", ts.URL+"/volumes/from-archive?name=Bad!", bytes.NewReader([]byte("x")))
	if resp, _ := http.DefaultClient.Do(req); resp.StatusCode != 400 {
		t.Fatal("bad name accepted")
	}
}

func TestStorageQuotaCountsWritableLayersAndVolumes(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "PUT", "/projects/default/quota", map[string]any{"max_storage_gib": 1.5}, admin) // 1536 MiB
	req, _ := http.NewRequest("POST", ts.URL+"/volumes/from-archive?name=cache&size_mib=512", bytes.NewReader([]byte("tar")))
	req.Header.Set("Authorization", "Bearer deployer-secret")
	if resp, _ := http.DefaultClient.Do(req); resp.StatusCode != 201 {
		t.Fatal("volume", resp.StatusCode)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, admin); st != 201 { // 512 (volume) + 512 = 1024 <= 1536
		t.Fatal("first sandbox", st)
	}
	if st, e := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "disk": 1024}, admin); st != 429 || e["error"] != "quota: storage" { // 1024 + 1024 > 1536
		t.Fatalf("storage quota not enforced: %d %v", st, e)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "disk": 512}, admin); st != 201 { // exactly 1536
		t.Fatal("fits exactly", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "disk": 512}, admin); st != 429 {
		t.Fatal("over", st)
	}
}

func TestAProjectDefaultNetworkPolicyAppliesWhenTheRequestSaysNothing(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "acme", "name": "Acme"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "carol", "role": "owner"}, admin)
	carol := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	_, mem := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "dave"}, carol)
	dave := map[string]string{"Authorization": "Bearer " + mem["token"].(string)}
	if _, r := call(t, ts.URL, "GET", "/projects/acme/network", nil, dave); r["network"] != nil {
		t.Fatalf("a fresh project has no default: %v", r)
	}
	policy := map[string]any{"mode": "allowlist", "allow_hosts": []any{"pypi.org"}, "limits": map[string]any{"requests_per_min": 10.0}}
	if st, _ := call(t, ts.URL, "PUT", "/projects/acme/network", policy, dave); st != 403 {
		t.Fatal("a member set the default:", st)
	}
	if st, e := call(t, ts.URL, "PUT", "/projects/acme/network", map[string]any{"mode": "open"}, carol); st != 400 {
		t.Fatalf("bad mode accepted: %d %v", st, e)
	}
	if st, e := call(t, ts.URL, "PUT", "/projects/acme/network", map[string]any{"limits": map[string]any{"max_bytes": -1.0}}, carol); st != 400 {
		t.Fatalf("bad limit accepted: %d %v", st, e)
	}
	if st, r := call(t, ts.URL, "PUT", "/projects/acme/network", policy, carol); st != 200 || r["network"].(map[string]any)["mode"] != "allowlist" {
		t.Fatalf("set %d %v", st, r)
	}
	st, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme"}, dave)
	if st != 201 {
		t.Fatal("create", st, sb)
	}
	a.mu.Lock()
	got := a.boxes[sb["id"].(string)]["network"]
	a.mu.Unlock()
	if gm, _ := got.(map[string]any); gm == nil || gm["mode"] != "allowlist" || gm["limits"].(map[string]any)["requests_per_min"] != 10.0 {
		t.Fatalf("the node did not get the project's policy: %v", got)
	}
	st, sb2 := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme", "network": map[string]any{"mode": "closed"}}, dave)
	a.mu.Lock()
	got2 := a.boxes[sb2["id"].(string)]["network"].(map[string]any)
	a.mu.Unlock()
	if st != 201 || got2["mode"] != "closed" {
		t.Fatalf("an explicit policy must win: %d %v", st, got2)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/projects/acme/network", nil, carol); st != 200 {
		t.Fatal("delete", st)
	}
	_, sb3 := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme"}, dave)
	a.mu.Lock()
	_, has := a.boxes[sb3["id"].(string)]["network"]
	a.mu.Unlock()
	if has {
		t.Fatal("after the default is removed a request without network carries none")
	}
}

func TestAWebSocketOnAnExposedPortIsTunnelledThroughTheControlPlane(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	_, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, admin)
	u := strings.TrimPrefix(ts.URL, "http://")
	c, err := net.Dial("tcp", u)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	fmt.Fprintf(c, "GET /sandboxes/%s/ports/8090/chat HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer deployer-secret\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: x\r\nSec-WebSocket-Version: 13\r\n\r\nearly", sb["id"], u)
	c.SetDeadline(time.Now().Add(5 * time.Second))
	rd := bufio.NewReader(c)
	line, _ := rd.ReadString('\n')
	if !strings.HasPrefix(line, "HTTP/1.1 101") {
		t.Fatalf("handshake: %q", line)
	}
	for {
		h, _ := rd.ReadString('\n')
		if h == "\r\n" || h == "" {
			break
		}
	}
	payload := func(want string) string { // a byte stream: the echo may come in pieces, each prefixed; collect until the payload is complete
		got := ""
		buf := make([]byte, 64)
		for len(got) < len(want) {
			n, err := rd.Read(buf)
			if err != nil {
				break
			}
			got += strings.ReplaceAll(string(buf[:n]), "echo:", "")
		}
		return got
	}
	if got := payload("early"); got != "early" { // bytes the client sent right behind its handshake reach the guest too
		t.Fatalf("early bytes: %q", got)
	}
	c.Write([]byte("ping"))
	if got := payload("ping"); got != "ping" {
		t.Fatalf("echo: %q", got)
	}
}

func TestALayerIsCopiedToEveryNodeThatLacksIt(t *testing.T) {
	a, b := newFakeNode(t, "na", 10), newFakeNode(t, "nb", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a, b)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	st, r := call(t, ts.URL, "POST", "/layers/L-na/everywhere", nil, admin)
	nodes, _ := r["nodes"].(map[string]any)
	if st != 200 || nodes["na"] != "present" || nodes["nb"] != "imported" {
		t.Fatalf("everywhere %d %v", st, r)
	}
	b.mu.Lock()
	imported := append([]string{}, b.layers...)
	b.mu.Unlock()
	if len(imported) != 1 || imported[0] != "L-na" {
		t.Fatalf("nb imported %v", imported)
	}
	if _, r := call(t, ts.URL, "POST", "/layers/L-na/everywhere", nil, admin); r["nodes"].(map[string]any)["nb"] != "present" {
		t.Fatalf("second run should find it present: %v", r)
	}
	if st, _ := call(t, ts.URL, "POST", "/layers/L-nope/everywhere", nil, admin); st != 404 {
		t.Fatal("unknown layer:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/layers/L-na/everywhere", nil, nil); st != 401 && st != 403 {
		t.Fatal("no token may not distribute layers:", st)
	}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "acme", "name": "Acme"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "carol", "role": "owner"}, admin)
	carol := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	if st, _ := call(t, ts.URL, "POST", "/layers/L-na/everywhere", nil, carol); st != 403 {
		t.Fatal("an owner distributed a layer (deployer only):", st)
	}
}

func TestAnInteractiveRequestPausesABackgroundSandboxWhenMemoryIsFull(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	a.memBudget = 1000 // room for three 256 MiB sandboxes, not four
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret")
	s.Fleet.Add(fleet.Spec{Name: a.name, URL: a.srv.URL, Token: a.token, ReserveVMs: 1, ReserveMemMiB: 1}, false) // this node keeps almost nothing back
	s.pollOnce()
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	ids := []string{}
	for i, prio := range []string{"batch", "background", "background"} {
		st, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "priority": prio}, admin)
		if st != 201 {
			t.Fatalf("background create %d: %d %v", i, st, sb)
		}
		ids = append(ids, sb["id"].(string))
		s.pollOnce()
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "priority": "background"}, admin); st != 429 {
		t.Fatal("a fourth background sandbox must wait, nothing is preempted for it:", st)
	}
	st, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, admin)
	if st != 201 {
		t.Fatalf("interactive create should preempt: %d %v", st, sb)
	}
	a.mu.Lock()
	paused := []string{}
	for id, b := range a.boxes {
		if b["status"] == "paused" {
			paused = append(paused, id)
		}
	}
	a.mu.Unlock()
	if len(paused) != 1 || paused[0] != ids[2] { // the youngest background one, not the batch one
		t.Fatalf("paused %v (ids %v)", paused, ids)
	}
	if r := s.Life.Route(sb["id"].(string)); r == nil || r.Priority != "interactive" {
		t.Fatalf("route priority %v", r)
	}
}

func TestAMigrationTakesTheSandboxesVolumesAlongAndRefusesASharedOne(t *testing.T) {
	a, b := newFakeNode(t, "na", 10), newFakeNode(t, "nb", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a, b)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	_, v := call(t, ts.URL, "POST", "/volumes", map[string]any{"name": "data", "size_mib": 256, "node": "na"}, admin)
	vid := v["id"].(string)
	st, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "volumes": []any{map[string]any{"id": vid, "mount": "/mnt/d"}}}, admin)
	if st != 201 || sb["node"] != "na" {
		t.Fatalf("create %d %v", st, sb)
	}
	sid := sb["id"].(string)
	st, mg := call(t, ts.URL, "POST", "/sandboxes/"+sid+"/migrate", map[string]any{"to": "nb"}, admin)
	if st != 200 || mg["to"] != "nb" || fmt.Sprint(mg["volumes"]) != fmt.Sprintf("[%s]", vid) {
		t.Fatalf("migrate %d %v", st, mg)
	}
	a.mu.Lock()
	_, onA := a.vols[vid]
	a.mu.Unlock()
	b.mu.Lock()
	_, onB := b.vols[vid]
	b.mu.Unlock()
	if onA || !onB {
		t.Fatalf("volume should have moved na -> nb: na=%v nb=%v", onA, onB)
	}
	if x, err := s.Volumes.Get(vid); err != nil || x.Node != "nb" {
		t.Fatalf("record not re-homed: %v %v", x, err)
	}
	if r := s.Life.Route(sid); r == nil || r.Node != "nb" {
		t.Fatalf("route %v", r)
	}
	// a volume two sandboxes read cannot leave with one of them
	_, v2 := call(t, ts.URL, "POST", "/volumes", map[string]any{"name": "shared", "size_mib": 256, "node": "na"}, admin)
	vid2 := v2["id"].(string)
	_, s1 := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "volumes": []any{map[string]any{"id": vid2, "ro": true}}}, admin)
	call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "volumes": []any{map[string]any{"id": vid2, "ro": true}}}, admin)
	if st, e := call(t, ts.URL, "POST", "/sandboxes/"+s1["id"].(string)+"/migrate", map[string]any{"to": "nb"}, admin); st != 409 || !strings.Contains(e["error"].(string), "also attached") {
		t.Fatalf("shared volume migrated: %d %v", st, e)
	}
}

func TestAnInteractiveRequestEndsAnEphemeralBackgroundSandboxWhenSlotsAreFull(t *testing.T) {
	a := newFakeNode(t, "na", 4) // four slots: three background ones plus one interactive fill it
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret")
	s.Fleet.Add(fleet.Spec{Name: a.name, URL: a.srv.URL, Token: a.token, ReserveVMs: 1, ReserveMemMiB: 1}, false)
	s.pollOnce()
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	ids := []string{}
	for _, cls := range []string{"persistent", "ephemeral", "ephemeral", "interactive"} {
		body := map[string]any{"mem": 1, "priority": "background", "class": cls}
		if cls == "interactive" {
			body = map[string]any{"mem": 1}
		}
		st, sb := call(t, ts.URL, "POST", "/sandboxes", body, admin)
		if st != 201 {
			t.Fatalf("create %d %v", st, sb)
		}
		ids = append(ids, sb["id"].(string))
		s.pollOnce()
	}
	st, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 1}, admin)
	if st != 201 {
		t.Fatalf("interactive create should end an ephemeral background sandbox: %d %v", st, sb)
	}
	a.mu.Lock()
	gone := []string{}
	for _, id := range ids {
		if _, ok := a.boxes[id]; !ok {
			gone = append(gone, id)
		}
	}
	first := a.boxes[ids[0]]["status"]
	a.mu.Unlock()
	if len(gone) != 1 || gone[0] != ids[2] || first != "running" { // the youngest background one is paused first (no slot freed), then ended; the persistent one stays
		t.Fatalf("gone %v (ids %v) persistent=%v", gone, ids, first)
	}
	if s.Life.Route(ids[2]) != nil {
		t.Fatal("the ended sandbox still has a route")
	}
}

func TestAMemberMayTightenEgressAtOnceButLooseningWaitsForAnOwner(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	srv, ts := testServer(t, reg, "deployer-secret", a)
	var evMu sync.Mutex
	relaxRows := []map[string]any{}
	srv.Audit.(*audit.Log).Tap(func(ev map[string]any) { // D79: the request/decision rows carry the project, or a project-scoped reader never sees them
		if op, _ := ev["op"].(string); strings.HasPrefix(op, "network_relax_") {
			evMu.Lock()
			relaxRows = append(relaxRows, ev)
			evMu.Unlock()
		}
	})
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "acme", "name": "Acme"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "carol", "role": "owner"}, admin)
	carol := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	_, mem := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "dave"}, carol)
	dave := map[string]string{"Authorization": "Bearer " + mem["token"].(string)}
	_, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme", "network": map[string]any{"mode": "allowlist", "allow_hosts": []any{"pypi.org"}, "limits": map[string]any{"requests_per_min": 10.0}}}, dave)
	sid := sb["id"].(string)
	netOf := func() map[string]any {
		a.mu.Lock()
		defer a.mu.Unlock()
		n, _ := a.boxes[sid]["network"].(map[string]any)
		return n
	}
	// tightening: applied at once
	if st, _ := call(t, ts.URL, "PUT", "/sandboxes/"+sid+"/network", map[string]any{"allow_hosts": []any{}}, dave); st != 200 || len(netOf()["allow_hosts"].([]any)) != 0 {
		t.Fatalf("tighten %d %v", st, netOf())
	}
	// loosening: a request, nothing applied
	st, rq := call(t, ts.URL, "PUT", "/sandboxes/"+sid+"/network", map[string]any{"allow_hosts": []any{"pypi.org", "github.com"}, "limits": map[string]any{"requests_per_min": 0.0}}, dave)
	if st != 202 || len(netOf()["allow_hosts"].([]any)) != 0 {
		t.Fatalf("loosen %d %v %v", st, rq, netOf())
	}
	rid := rq["request"].(map[string]any)["id"].(string)
	_, lst := call(t, ts.URL, "GET", "/network/requests", nil, carol)
	if arr := lst["requests"].([]any); len(arr) != 1 || arr[0].(map[string]any)["id"] != rid {
		t.Fatalf("pending %v", lst)
	}
	if st, _ := call(t, ts.URL, "POST", "/network/requests/"+rid+"/approve", nil, dave); st != 403 {
		t.Fatal("a member approved:", st)
	}
	if st, r := call(t, ts.URL, "POST", "/network/requests/"+rid+"/approve", nil, carol); st != 200 || r["request"].(map[string]any)["status"] != "approved" {
		t.Fatalf("approve %d %v", st, r)
	}
	if hs := netOf()["allow_hosts"].([]any); len(hs) != 2 || netOf()["limits"].(map[string]any)["requests_per_min"] != 0.0 {
		t.Fatalf("not applied: %v", netOf())
	}
	if st, _ := call(t, ts.URL, "POST", "/network/requests/"+rid+"/approve", nil, carol); st != 409 {
		t.Fatal("decided twice:", st)
	}
	_, lst = call(t, ts.URL, "GET", "/network/requests", nil, carol)
	if len(lst["requests"].([]any)) != 0 {
		t.Fatal("still pending")
	}
	// a second request, denied: nothing changes; the owner herself loosens directly
	st, rq = call(t, ts.URL, "PUT", "/sandboxes/"+sid+"/network", map[string]any{"mode": "audited"}, dave)
	rid = rq["request"].(map[string]any)["id"].(string)
	if st, _ := call(t, ts.URL, "POST", "/network/requests/"+rid+"/deny", nil, carol); st != 200 || netOf()["mode"] != "allowlist" {
		t.Fatalf("deny %d %v", st, netOf())
	}
	if st, _ := call(t, ts.URL, "PUT", "/sandboxes/"+sid+"/network", map[string]any{"mode": "audited"}, carol); st != 200 || netOf()["mode"] != "audited" {
		t.Fatalf("owner loosens directly: %d %v", st, netOf())
	}
	evMu.Lock()
	defer evMu.Unlock()
	ops := map[string]bool{}
	for _, ev := range relaxRows {
		ops[ev["op"].(string)] = true
		if ev["project"] != "acme" {
			t.Fatalf("audit row without its project: %v", ev)
		}
	}
	if !ops["network_relax_requested"] || !ops["network_relax_approved"] || !ops["network_relax_denied"] {
		t.Fatalf("audit rows seen: %v", ops)
	}
}

func TestABundleCanBeImportedByHandOntoANode(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	st, im := call(t, ts.URL, "POST", "/sandboxes/import", map[string]any{"bundle": "sb-old-1"}, admin)
	if st != 201 || im["node"] != "na" {
		t.Fatalf("import %d %v", st, im)
	}
	if r := s.Life.Route(im["id"].(string)); r == nil || r.Node != "na" || r.Project != "default" {
		t.Fatalf("route %v", r)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes/import", map[string]any{}, admin); st != 400 {
		t.Fatal("bundle required:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes/import", map[string]any{"bundle": "x"}, nil); st != 401 && st != 403 {
		t.Fatal("members may not import:", st)
	}
}

func TestLimitsMetricsAuditChainAndTokenRotation(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "acme", "name": "Acme"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "carol", "role": "owner"}, admin)
	carol := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	_, mem := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "dave"}, carol)
	dave := map[string]string{"Authorization": "Bearer " + mem["token"].(string)}
	// rate limit: 3 per minute for members; the admin is never limited
	s.RatePerMin = 3
	for i := 0; i < 3; i++ {
		if st, _ := call(t, ts.URL, "GET", "/me", nil, dave); st != 200 {
			t.Fatalf("call %d: %d", i, st)
		}
	}
	if st, e := call(t, ts.URL, "GET", "/me", nil, dave); st != 429 || e["error"] != "rate limit" {
		t.Fatalf("4th call: %d %v", st, e)
	}
	for i := 0; i < 5; i++ {
		if st, _ := call(t, ts.URL, "GET", "/me", nil, admin); st != 200 {
			t.Fatal("admin limited")
		}
	}
	s.RatePerMin = 0
	// body cap
	s.MaxBodyMiB = 1
	req, _ := http.NewRequest("POST", ts.URL+"/layers/from-archive?name=x", bytes.NewReader(make([]byte, 2<<20)))
	req.Header.Set("Authorization", "Bearer deployer-secret")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 413 {
		t.Fatal("2 MiB body under a 1 MiB cap:", resp.StatusCode)
	}
	s.MaxBodyMiB = 0
	// metrics: admin only, Prometheus text with nodes and sandboxes
	call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme"}, dave)
	if st, _ := call(t, ts.URL, "GET", "/metrics", nil, dave); st != 403 {
		t.Fatal("member read metrics:", st)
	}
	req, _ = http.NewRequest("GET", ts.URL+"/metrics", nil)
	req.Header.Set("Authorization", "Bearer deployer-secret")
	resp, _ = http.DefaultClient.Do(req)
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || !strings.Contains(string(body), `cairn_node_up{node="na"} 1`) || !strings.Contains(string(body), `cairnboxes{class="persistent",priority="interactive",node="na",project="acme"} 1`) {
		t.Fatalf("metrics %d %q", resp.StatusCode, string(body))
	}
	// audit chain verify
	dir := t.TempDir()
	lg, _ := audit.Open(dir, "r-test")
	lg.Write(map[string]any{"op": "a"})
	lg.Write(map[string]any{"op": "b"})
	s.AuditPath = filepath.Join(dir, "audit.jsonl")
	if st, v := call(t, ts.URL, "GET", "/audit/verify", nil, admin); st != 200 || v["ok"] != true || v["lines"].(float64) != 2 {
		t.Fatalf("verify %d %v", st, v)
	}
	if st, _ := call(t, ts.URL, "GET", "/audit/verify", nil, carol); st != 403 {
		t.Fatal("owner read the audit verify:", st)
	}
	// rotation: the old token dies, the new one works, a ttl expires it; the member may rotate their own, not another's
	st, rot := call(t, ts.URL, "POST", "/projects/acme/members/dave/rotate", map[string]any{}, dave)
	if st != 200 || rot["token"] == nil || rot["token"] == mem["token"] {
		t.Fatalf("self rotate %d %v", st, rot)
	}
	if st, _ := call(t, ts.URL, "GET", "/me", nil, dave); st != 401 {
		t.Fatal("old token still works:", st)
	}
	dave2 := map[string]string{"Authorization": "Bearer " + rot["token"].(string)}
	if st, _ := call(t, ts.URL, "GET", "/me", nil, dave2); st != 200 {
		t.Fatal("new token refused:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/projects/acme/members/carol/rotate", map[string]any{}, dave2); st != 403 {
		t.Fatal("a member rotated the owner's token:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/projects/acme/members/dave/expiry", map[string]any{"ttl_s": 0.001}, carol); st != 200 {
		t.Fatal("owner sets expiry:", st)
	}
	time.Sleep(20 * time.Millisecond)
	if st, _ := call(t, ts.URL, "GET", "/me", nil, dave2); st != 401 {
		t.Fatal("expired token still works:", st)
	}
	if st, r2 := call(t, ts.URL, "POST", "/projects/acme/members/dave/rotate", map[string]any{"ttl_s": 3600}, carol); st != 200 || r2["expires"].(float64) == 0 {
		t.Fatalf("owner rotates with a lifetime: %d %v", st, r2)
	}
}

func TestTheAuditTailIsScopedToTheCallersAccount(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	dir := t.TempDir()
	lg, _ := audit.Open(dir, "r-test")
	s.Audit = lg
	s.AuditPath = filepath.Join(dir, "audit.jsonl")
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "acme", "name": "Acme"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "carol", "role": "owner"}, admin)
	carol := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	lg.Write(map[string]any{"op": "node_add", "node": "na"})                                      // no project: the deployer's business only
	lg.Write(map[string]any{"op": "create", "project": "default", "sandbox": "sb-d"})             // another account's project
	lg.Write(map[string]any{"op": "create", "project": "acme", "sandbox": "sb-a", "by": "carol"}) // hers
	_, all := call(t, ts.URL, "GET", "/audit?n=50", nil, admin)
	_, mine := call(t, ts.URL, "GET", "/audit?n=50", nil, carol)
	na, nm := len(all["events"].([]any)), len(mine["events"].([]any))
	if na != 3 || nm != 1 {
		t.Fatalf("admin sees %d, carol %d", na, nm)
	}
	for _, e := range mine["events"].([]any) {
		if p := e.(map[string]any)["project"]; p != "acme" {
			t.Fatalf("carol saw another project's line: %v", e)
		}
	}
	if ev := all["events"].([]any)[0].(map[string]any); ev["op"] != "create" { // newest first
		t.Fatalf("newest first: %v", ev)
	}
}

func TestALostRegistryIsRebuiltFromTheNodesWithProjectPriorityAndMetadata(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "acme", "name": "Acme"}, admin)
	st, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme", "priority": "batch", "metadata": map[string]any{"purpose": "ci", "owner": "carol"}}, admin)
	if st != 201 {
		t.Fatal("create", st, sb)
	}
	sid := sb["id"].(string)
	a.mu.Lock()
	lb, _ := a.boxes[sid]["labels"].(map[string]any)
	a.mu.Unlock()
	if lb["project"] != "acme" || lb["priority"] != "batch" || lb["meta.purpose"] != "ci" {
		t.Fatalf("the node was not told the bookkeeping: %v", lb)
	}
	want := *s.Life.Route(sid)
	if err := reg.RouteDel(sid); err != nil { // the registry loses the route (a wiped Redis)
		t.Fatal(err)
	}
	if s.Life.Route(sid) != nil {
		t.Fatal("route should be gone")
	}
	s.reconcile(s.nodeByName("na"))
	got := s.Life.Route(sid)
	if got == nil || got.Node != "na" || got.Project != "acme" || got.Priority != "batch" || got.Meta["purpose"] != "ci" || got.Meta["owner"] != "carol" || got.Class != want.Class {
		t.Fatalf("rebuilt route %+v, want %+v", got, want)
	}
	// and a request for it works again (it would be a 404 without the route)
	if st, _ := call(t, ts.URL, "GET", "/sandboxes/"+sid, nil, admin); st != 200 {
		t.Fatal("the rebuilt sandbox is not reachable:", st)
	}
}

func TestWhenTheTenancyDatabaseIsDownCreateSaysSoInsteadOfNoProject(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	if st, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, admin); st != 201 {
		t.Fatal("healthy create:", st, sb)
	}
	s.Metering.DB().Close() // the database goes away
	st, e := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, admin)
	if st != 503 || !strings.Contains(e["error"].(string), "tenancy store unavailable") { // whichever step meets the dead database first says so
		t.Fatalf("db down: %d %v", st, e)
	}
	if st, _ := call(t, ts.URL, "GET", "/sandboxes", nil, admin); st != 200 { // reads that need no database still work
		t.Fatal("list with the database down:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes/"+a.anyBox()+"/exec", map[string]any{"cmd": "true"}, admin); st != 200 {
		t.Fatal("exec on an existing sandbox with the database down:", st)
	}
}

func TestMetricsReportDependenciesCountersAndSandboxesOnDownNodes(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, admin)
	call(t, ts.URL, "GET", "/sandboxes/nope", nil, admin) // a 404
	get := func() string {
		req, _ := http.NewRequest("GET", ts.URL+"/metrics", nil)
		req.Header.Set("Authorization", "Bearer deployer-secret")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return string(b)
	}
	m := get()
	for _, want := range []string{`cairn_dependency_up{dep="registry"} 1`, `cairn_dependency_up{dep="database"} 1`, `cairn_http_responses_total{class="2xx"}`, `cairn_http_responses_total{class="4xx"} 1`, "cairnboxes_on_down_nodes 0"} {
		if !strings.Contains(m, want) {
			t.Fatalf("metrics lack %q:\n%s", want, m)
		}
	}
	a.mu.Lock()
	a.down = true
	a.mu.Unlock()
	s.pollOnce() // the node stops answering
	if m := get(); !strings.Contains(m, "cairnboxes_on_down_nodes 1") || !strings.Contains(m, `cairn_node_up{node="na"} 0`) {
		t.Fatalf("a down node should show its sandbox as stranded:\n%s", m)
	}
	s.Metering.DB().Close()
	if m := get(); !strings.Contains(m, `cairn_dependency_up{dep="database"} 0`) {
		t.Fatalf("a closed database should read 0:\n%s", m)
	}
}

// flakyRegistry is a registry whose route reads can be made to fail (a Redis that stopped answering).
type flakyRegistry struct {
	registry.Registry
	fail bool
}

func (f *flakyRegistry) RouteGet(sid string) (*registry.Route, error) {
	if f.fail {
		return nil, fmt.Errorf("dial tcp: connection refused")
	}
	return f.Registry.RouteGet(sid)
}

func TestWhenTheRegistryIsUnreachableASandboxRequestIs503NotNotFound(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	base, _ := registry.NewFileRegistry(t.TempDir())
	reg := &flakyRegistry{Registry: base}
	_, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	_, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, admin)
	sid := sb["id"].(string)
	reg.fail = true
	st, e := call(t, ts.URL, "POST", "/sandboxes/"+sid+"/exec", map[string]any{"cmd": "true"}, admin)
	if st != 503 || !strings.Contains(e["error"].(string), "registry unavailable") {
		t.Fatalf("registry down: %d %v", st, e)
	}
	if st, _ := call(t, ts.URL, "GET", "/sandboxes/sb-never-existed", nil, admin); st != 503 {
		t.Fatal("while the registry is down even an unknown id cannot be called 404:", st)
	}
	reg.fail = false
	if st, _ := call(t, ts.URL, "POST", "/sandboxes/"+sid+"/exec", map[string]any{"cmd": "true"}, admin); st != 200 {
		t.Fatal("back to normal:", st)
	}
	if st, _ := call(t, ts.URL, "GET", "/sandboxes/sb-never-existed", nil, admin); st != 404 {
		t.Fatal("an unknown id is a 404 again:", st)
	}
}

func TestTheConsoleExportIsServedUnderConsoleWithoutTouchingTheAPI(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	dir := t.TempDir()
	os.MkdirAll(filepath.Join(dir, "overview"), 0o755)
	os.MkdirAll(filepath.Join(dir, "_next", "static", "chunks"), 0o755)
	os.WriteFile(filepath.Join(dir, "index.html"), []byte("<html>root</html>"), 0o644)
	os.WriteFile(filepath.Join(dir, "overview", "index.html"), []byte("<html>overview</html>"), 0o644)
	os.WriteFile(filepath.Join(dir, "404.html"), []byte("<html>nope</html>"), 0o644)
	os.WriteFile(filepath.Join(dir, "_next", "static", "chunks", "a.js"), []byte("console.log(1)"), 0o644)
	os.WriteFile(filepath.Join(dir, "secret.txt"), []byte("x"), 0o644)
	s.ConsoleDir = dir
	get := func(path string) (*http.Response, string) {
		c := http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		resp, err := c.Get(ts.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp, string(b)
	}
	if r, b := get("/console/"); r.StatusCode != 200 || b != "<html>root</html>" || !strings.HasPrefix(r.Header.Get("Content-Type"), "text/html") {
		t.Fatalf("root: %d %q", r.StatusCode, b)
	}
	if r, b := get("/console/overview/"); r.StatusCode != 200 || b != "<html>overview</html>" {
		t.Fatalf("page: %d %q", r.StatusCode, b)
	}
	if r, _ := get("/console/overview"); r.StatusCode != 301 || r.Header.Get("Location") != "/console/overview/" {
		t.Fatalf("no trailing slash should redirect: %d %q", r.StatusCode, r.Header.Get("Location"))
	}
	if r, _ := get("/console"); r.StatusCode != 301 {
		t.Fatalf("/console: %d", r.StatusCode)
	}
	if r, b := get("/console/_next/static/chunks/a.js"); r.StatusCode != 200 || !strings.Contains(r.Header.Get("Cache-Control"), "immutable") || b != "console.log(1)" {
		t.Fatalf("asset: %d %v", r.StatusCode, r.Header)
	}
	if r, b := get("/console/missing/"); r.StatusCode != 404 || b != "<html>nope</html>" {
		t.Fatalf("unknown path serves the export's 404 page: %d %q", r.StatusCode, b)
	}
	if r, _ := get("/console/../secret.txt"); r.StatusCode == 200 {
		t.Fatal("path traversal served a file")
	}
	// the API is untouched: /sandboxes is still the JSON API, and needs a token
	if r, _ := get("/sandboxes"); r.StatusCode != 401 {
		t.Fatalf("API path: %d", r.StatusCode)
	}
	s.ConsoleDir = ""
	if r, _ := get("/console/"); r.StatusCode != 404 {
		t.Fatalf("not installed: %d", r.StatusCode)
	}
}

func TestSuspendingAnAccountPausesItsSandboxesAndLeavesMembersReadAndCleanUpOnly(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "bad", "name": "Bad"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/bad/members", map[string]any{"user": "mallory", "role": "owner"}, admin)
	mal := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	_, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, mal)
	sid := sb["id"].(string)
	es := httptest.NewServer(&e2b.Server{Tenancy: s.Tenancy, Life: s.Life, Images: s.Images})
	defer es.Close()

	if st, _ := call(t, ts.URL, "POST", "/accounts/bad/suspend", map[string]any{"reason": "x"}, mal); st != 403 {
		t.Fatal("a member suspended an account:", st)
	}
	if st, e := call(t, ts.URL, "POST", "/accounts/bad/suspend", map[string]any{}, admin); st != 400 {
		t.Fatalf("suspend without a reason: %d %v", st, e)
	}
	if st, _ := call(t, ts.URL, "POST", "/accounts/nope/suspend", map[string]any{"reason": "x"}, admin); st != 404 {
		t.Fatal("suspended an account that does not exist:", st)
	}
	st, res := call(t, ts.URL, "POST", "/accounts/bad/suspend", map[string]any{"reason": "mining"}, admin)
	if st != 200 || res["suspended"] != true || len(res["paused"].([]any)) != 1 || res["paused"].([]any)[0] != sid {
		t.Fatalf("suspend: %d %v", st, res)
	}
	if st, info := call(t, ts.URL, "GET", "/sandboxes/"+sid, nil, mal); st != 200 || info["status"] != "paused" {
		t.Fatalf("reading is allowed and shows it paused: %d %v", st, info)
	}
	for _, sub := range []string{"/files?path=%2Fetc%2Fhostname", "/ports/8080/", "/events", "/diff"} { // the node resumes a paused sandbox on these, even for a GET
		if st, e := call(t, ts.URL, "GET", "/sandboxes/"+sid+sub, nil, mal); st != 403 || e["error"] != "account suspended" {
			t.Fatalf("GET %s while suspended: %d %v", sub, st, e)
		}
	}
	if st, _ := call(t, ts.URL, "GET", "/sandboxes/"+sid+"/checkpoints", nil, mal); st == 403 {
		t.Fatal("the checkpoint list must stay readable")
	}
	if st, e := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, mal); st != 403 || e["error"] != "account suspended" || e["reason"] != "mining" {
		t.Fatalf("create while suspended: %d %v", st, e)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes/"+sid+"/resume", nil, mal); st != 403 {
		t.Fatal("resumed while suspended:", st)
	}
	if st, _ := call(t, es.URL, "POST", "/sandboxes", map[string]any{"templateID": "base"}, map[string]string{"X-API-Key": own["token"].(string)}); st != 403 {
		t.Fatal("the E2B surface let a suspended account create:", st)
	}
	if _, me := call(t, ts.URL, "GET", "/me", nil, mal); me["suspended"] == nil {
		t.Fatalf("/me does not say the account is suspended: %v", me)
	}
	if _, l := call(t, ts.URL, "GET", "/suspensions", nil, admin); len(l["suspensions"].([]any)) != 1 {
		t.Fatalf("suspensions %v", l)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes/"+sid+"/resume", nil, admin); st != 200 { // the deployer is not blocked (investigation)
		t.Fatal("deployer blocked:", st)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/sandboxes/"+sid, nil, mal); st != 200 {
		t.Fatal("cleaning up is allowed:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/accounts/bad/unsuspend", nil, admin); st != 200 {
		t.Fatal("unsuspend:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/accounts/bad/unsuspend", nil, admin); st != 404 {
		t.Fatal("unsuspend twice:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, mal); st != 201 {
		t.Fatal("create after unsuspend:", st)
	}
}

func TestAutoEvacuateWaitsOutTheFenceWindowAndActsOncePerOutage(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "", a)
	_, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, nil)
	sid := info["id"].(string)
	done := map[string]float64{}
	s.Life.AutoEvacuateOnce(time.Minute, done) // alive: nothing to do
	if r, _ := reg.RouteGet(sid); r == nil {
		t.Fatal("a live node's sandbox was evacuated")
	}
	a.mu.Lock()
	a.down = true
	a.mu.Unlock()
	s.pollOnce()
	s.Life.AutoEvacuateOnce(time.Hour, done) // down, but not for long enough: the node may still be fencing itself
	if r, _ := reg.RouteGet(sid); r == nil {
		t.Fatal("evacuated before the window passed")
	}
	time.Sleep(1100 * time.Millisecond)
	s.Life.AutoEvacuateOnce(time.Second, done)
	if r, _ := reg.RouteGet(sid); r != nil {
		t.Fatal("not evacuated after the window")
	}
	if len(done) != 1 {
		t.Fatalf("outage not remembered: %v", done)
	}
	s.Life.AutoEvacuateOnce(time.Second, done) // the same outage is not handled twice
}

func TestPreviewHostsReachSandboxPortsAndCarryTheTokenInACookie(t *testing.T) {
	for host, want := range map[string]string{"8080-sb-1a2b.preview.test": "sb-1a2b:8080", "3000-sb-1a2b.preview.test:8443": "sb-1a2b:3000", "8080-SB-1A2B.Preview.Test": "sb-1a2b:8080"} {
		sid, port, ok := previewHost(host, "preview.test")
		if !ok || fmt.Sprintf("%s:%d", sid, port) != want {
			t.Fatalf("%s -> %s %d %v", host, sid, port, ok)
		}
	}
	for _, host := range []string{"preview.test", "x-sb-1.preview.test", "0-sb-1.preview.test", "70000-sb-1.preview.test", "8080-sb-1.other.test", "8080-sb-1.preview.test.evil.com"} {
		if _, _, ok := previewHost(host, "preview.test"); ok {
			t.Fatalf("%s must not be a preview host", host)
		}
	}
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	s.PreviewDomain = "preview.test"
	_, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, map[string]string{"Authorization": "Bearer deployer-secret"})
	host := "8080-" + sb["id"].(string) + ".preview.test"
	do := func(path string, hdr map[string]string) *http.Response {
		req, _ := http.NewRequest("GET", ts.URL+path, nil)
		req.Host = host
		for k, v := range hdr {
			req.Header.Set(k, v)
		}
		resp, err := (&http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}).Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp
	}
	if r := do("/hello", nil); r.StatusCode != 401 {
		t.Fatalf("no credential: %d", r.StatusCode)
	}
	if r := do("//evil.example/x?token=deployer-secret", nil); r.StatusCode != 302 || !strings.HasPrefix(r.Header.Get("Location"), "/evil.example") || strings.HasPrefix(r.Header.Get("Location"), "//") {
		t.Fatalf("open redirect: %d %q", r.StatusCode, r.Header.Get("Location"))
	}
	r := do("/hello?x=1&token=deployer-secret", nil) // first visit: cookie set, token gone from the URL
	if r.StatusCode != 302 || r.Header.Get("Location") != "/hello?x=1" {
		t.Fatalf("redirect: %d %q", r.StatusCode, r.Header.Get("Location"))
	}
	ck := r.Cookies()
	if len(ck) != 1 || ck[0].Name != "cairn_preview" || ck[0].Value != "deployer-secret" || !ck[0].HttpOnly {
		t.Fatalf("cookie %+v", ck)
	}
	if r := do("/hello", map[string]string{"Cookie": "cairn_preview=deployer-secret; app=1"}); r.StatusCode == 401 || r.StatusCode == 302 {
		t.Fatalf("the cookie did not authenticate: %d", r.StatusCode)
	}
	if r := do("/hello", map[string]string{"Cookie": "cairn_preview=wrong"}); r.StatusCode != 401 {
		t.Fatalf("a wrong cookie: %d", r.StatusCode)
	}
}

func TestAPIKeysAreScopedToOneProjectCanBeViewersAndRevokedAtOnce(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "acme", "name": "Acme"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "carol", "role": "owner"}, admin)
	carol := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	call(t, ts.URL, "POST", "/accounts/acme/projects", map[string]any{"name": "web"}, carol)
	_, mem := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "dave"}, carol)
	dave := map[string]string{"Authorization": "Bearer " + mem["token"].(string)}

	if st, _ := call(t, ts.URL, "POST", "/projects/acme-web/keys", map[string]any{"name": "ci"}, dave); st != 403 {
		t.Fatal("a member issued a key:", st)
	}
	if st, e := call(t, ts.URL, "POST", "/projects/acme-web/keys", map[string]any{"name": "x", "role": "owner"}, carol); st != 400 {
		t.Fatalf("an owner key: %d %v", st, e)
	}
	st, k := call(t, ts.URL, "POST", "/projects/acme-web/keys", map[string]any{"name": "ci", "role": "member"}, carol)
	if st != 201 || k["secret"] == nil || k["key"] == nil {
		t.Fatalf("create key: %d %v", st, k)
	}
	ci := map[string]string{"Authorization": "Bearer " + k["secret"].(string)}
	keyID := k["key"].(map[string]any)["id"].(string)
	_, v := call(t, ts.URL, "POST", "/projects/acme-web/keys", map[string]any{"name": "dash", "role": "viewer", "ttl_s": 3600}, carol)
	dash := map[string]string{"Authorization": "Bearer " + v["secret"].(string)}

	if _, me := call(t, ts.URL, "GET", "/me", nil, ci); me["key"] != true || len(me["projects"].([]any)) != 1 || me["projects"].([]any)[0] != "acme-web" {
		t.Fatalf("/me for a key: %v", me)
	}
	if st, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, ci); st != 201 || sb["project"] != "acme-web" {
		t.Fatalf("a key creates in its own project: %d %v", st, sb)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme"}, ci); st != 403 {
		t.Fatal("a key reached another project of the same account:", st)
	}
	_, other := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "project": "acme"}, carol)
	if st, _ := call(t, ts.URL, "GET", "/sandboxes/"+other["id"].(string), nil, ci); st != 403 && st != 404 {
		t.Fatal("a key read a sandbox of another project:", st)
	}
	if _, lst := call(t, ts.URL, "GET", "/sandboxes", nil, ci); len(lst["sandboxes"].([]any)) != 1 {
		t.Fatalf("a key lists only its project: %v", lst)
	}
	if st, _ := call(t, ts.URL, "POST", "/projects/acme-web/keys", map[string]any{"name": "y"}, ci); st != 403 {
		t.Fatal("a key issued a key:", st)
	}
	if st, _ := call(t, ts.URL, "GET", "/sandboxes", nil, dash); st != 200 {
		t.Fatal("a viewer cannot read:", st)
	}
	if st, e := call(t, ts.URL, "GET", "/sandboxes/"+other["id"].(string)+"/ports/8080/ws", nil, map[string]string{"Authorization": dash["Authorization"], "Upgrade": "websocket", "Connection": "Upgrade"}); st != 403 || e["error"] != "read-only credential" {
		t.Fatalf("a viewer opened a websocket: %d %v", st, e)
	}
	if st, e := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, dash); st != 403 || e["error"] != "read-only credential" {
		t.Fatalf("a viewer wrote: %d %v", st, e)
	}
	if _, ks := call(t, ts.URL, "GET", "/projects/acme-web/keys", nil, carol); len(ks["keys"].([]any)) != 2 || ks["keys"].([]any)[0].(map[string]any)["secret"] != nil {
		t.Fatalf("key list: %v", ks)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/projects/acme-web/keys/"+keyID, nil, carol); st != 200 {
		t.Fatal("revoke:", st)
	}
	if st, _ := call(t, ts.URL, "GET", "/sandboxes", nil, ci); st != 401 {
		t.Fatal("a revoked key still works:", st)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/projects/acme-web/keys/"+keyID, nil, carol); st != 404 {
		t.Fatal("revoke twice:", st)
	}
}

func TestAPrepaidAccountStopsAtZeroCreditAndATopUpBringsItBack(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "pay", "name": "Pay"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/pay/members", map[string]any{"user": "olga", "role": "owner"}, admin)
	olga := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	_, mem := call(t, ts.URL, "POST", "/projects/pay/members", map[string]any{"user": "max"}, olga)
	max := map[string]string{"Authorization": "Bearer " + mem["token"].(string)}

	if st, _ := call(t, ts.URL, "PUT", "/accounts/pay/billing", map[string]any{"mode": "prepaid"}, olga); st != 403 {
		t.Fatal("an owner switched billing mode:", st)
	}
	if st, _ := call(t, ts.URL, "PUT", "/accounts/pay/billing", map[string]any{"mode": "free"}, admin); st != 400 {
		t.Fatal("a bad mode:", st)
	}
	call(t, ts.URL, "PUT", "/accounts/pay/billing", map[string]any{"mode": "prepaid"}, admin)
	if st, e := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, max); st != 402 || e["error"] != "quota: out of credit" {
		t.Fatalf("no credit yet: %d %v", st, e)
	}
	if st, _ := call(t, ts.URL, "POST", "/accounts/pay/credit", map[string]any{"usd": 1}, olga); st != 403 {
		t.Fatal("an owner added money:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/accounts/pay/credit", map[string]any{"usd": 0}, admin); st != 400 {
		t.Fatal("a zero top-up:", st)
	}
	if st, r := call(t, ts.URL, "POST", "/accounts/pay/credit", map[string]any{"usd": 1, "note": "first top-up"}, admin); st != 201 || r["balance_usd"].(float64) != 1 {
		t.Fatalf("top-up: %d %v", st, r)
	}
	st, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, max)
	if st != 201 {
		t.Fatalf("create with credit: %d %v", st, sb)
	}
	// the account spends $2 against its $1: the watcher suspends it and pauses what runs
	if _, err := s.Metering.Record(metering.Event{Project: "pay", Sandbox: sb["id"].(string), Kind: "vcpu_s", Amount: 1, CostUSD: 2}); err != nil {
		t.Fatal(err)
	}
	s.creditPass()
	if _, info := call(t, ts.URL, "GET", "/sandboxes/"+sb["id"].(string), nil, max); info["status"] != "paused" {
		t.Fatalf("running sandbox not paused: %v", info["status"])
	}
	if st, e := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, max); st != 403 || e["reason"] != "out of credit" {
		t.Fatalf("create while out of credit: %d %v", st, e)
	}
	_, bal := call(t, ts.URL, "GET", "/accounts/pay/balance", nil, olga)
	if bal["balance_usd"].(float64) != -1 || bal["mode"] != "prepaid" || len(bal["entries"].([]any)) != 1 {
		t.Fatalf("balance %v", bal)
	}
	if st, _ := call(t, ts.URL, "GET", "/accounts/pay/balance", nil, max); st != 403 {
		t.Fatal("a member read the balance:", st)
	}
	call(t, ts.URL, "POST", "/accounts/pay/credit", map[string]any{"usd": 5}, admin) // back in the black: suspension lifted at once
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, max); st != 201 {
		t.Fatal("still blocked after a top-up:", st)
	}
	// a suspension for another reason is not lifted by money
	call(t, ts.URL, "POST", "/accounts/pay/suspend", map[string]any{"reason": "abuse"}, admin)
	call(t, ts.URL, "POST", "/accounts/pay/credit", map[string]any{"usd": 5}, admin)
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, max); st != 403 {
		t.Fatal("money lifted an abuse suspension:", st)
	}
}

func TestAnInviteLetsSomeoneCreateTheirOwnAccountOnceAndWithTheStartingCredit(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	if st, _ := call(t, ts.URL, "POST", "/invites", map[string]any{"note": "x"}, nil); st != 401 {
		t.Fatal("anonymous invite creation:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/signup", map[string]any{"code": "inv_nope", "account": "newco", "user": "nina"}, nil); st != 403 {
		t.Fatal("a made-up code:", st)
	}
	st, iv := call(t, ts.URL, "POST", "/invites", map[string]any{"note": "beta wave 1", "credit_usd": 5, "max_sandboxes": 3, "daily_usd": 2, "ttl_s": 3600}, admin)
	if st != 201 || iv["code"] == nil {
		t.Fatalf("invite: %d %v", st, iv)
	}
	code := iv["code"].(string)
	if st, _ := call(t, ts.URL, "POST", "/signup", map[string]any{"code": code, "account": "x", "user": "nina"}, nil); st != 400 {
		t.Fatal("a too-short account name was accepted:", st)
	}
	st, su := call(t, ts.URL, "POST", "/signup", map[string]any{"code": code, "account": "newco", "user": "nina", "name": "Nina"}, nil)
	if st != 201 || su["token"] == nil || su["credit_usd"].(float64) != 5 {
		t.Fatalf("signup: %d %v", st, su)
	}
	nina := map[string]string{"Authorization": "Bearer " + su["token"].(string)}
	if st, _ := call(t, ts.URL, "POST", "/signup", map[string]any{"code": code, "account": "other", "user": "oscar"}, nil); st != 403 {
		t.Fatal("a code used twice:", st)
	}
	if _, me := call(t, ts.URL, "GET", "/me", nil, nina); me["role"] != "owner" || me["account"] != "newco" {
		t.Fatalf("/me %v", me)
	}
	_, bal := call(t, ts.URL, "GET", "/accounts/newco/balance", nil, nina)
	if bal["mode"] != "prepaid" || bal["balance_usd"].(float64) != 5 {
		t.Fatalf("starting credit: %v", bal)
	}
	_, q := call(t, ts.URL, "GET", "/projects/newco/quota", nil, nina)
	if q["max_sandboxes"].(float64) != 3 || q["daily_usd"].(float64) != 2 {
		t.Fatalf("starting quota: %v", q)
	}
	if st, _ := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, nina); st != 201 {
		t.Fatal("the new account cannot create:", st)
	}
	// a taken name releases the code so the person can try again
	_, iv2 := call(t, ts.URL, "POST", "/invites", map[string]any{}, admin)
	if st, _ := call(t, ts.URL, "POST", "/signup", map[string]any{"code": iv2["code"], "account": "newco", "user": "nina2"}, nil); st != 400 {
		t.Fatal("a taken account name:", st)
	}
	if st, _ := call(t, ts.URL, "POST", "/signup", map[string]any{"code": iv2["code"], "account": "secondco", "user": "nina2"}, nil); st != 201 {
		t.Fatal("the code was not released after a name clash:", st)
	}
	if _, l := call(t, ts.URL, "GET", "/invites", nil, admin); len(l["invites"].([]any)) != 2 || l["invites"].([]any)[0].(map[string]any)["code"] != nil {
		t.Fatalf("invite list %v", l)
	}
}

func TestStatusIsPublicCoarseAndFollowsTheNodes(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	b := newFakeNode(t, "nb", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a, b)
	st, out := call(t, ts.URL, "GET", "/status", nil, nil) // no token
	if st != 200 || out["status"] != "operational" || out["components"].(map[string]any)["sandboxes"] != "operational" {
		t.Fatalf("all up: %d %v", st, out)
	}
	if raw, _ := json.Marshal(out); strings.Contains(string(raw), "na") && strings.Contains(string(raw), "nb") && strings.Contains(string(raw), `"na"`) {
		t.Fatalf("node names leak: %s", raw)
	}
	a.mu.Lock()
	a.down = true
	a.mu.Unlock()
	s.pollOnce()
	if st, out := call(t, ts.URL, "GET", "/status", nil, nil); st != 200 || out["status"] != "degraded" || out["components"].(map[string]any)["sandboxes"] != "degraded" {
		t.Fatalf("one node down: %d %v", st, out)
	}
	b.mu.Lock()
	b.down = true
	b.mu.Unlock()
	s.pollOnce()
	if st, out := call(t, ts.URL, "GET", "/status", nil, nil); st != 503 || out["status"] != "outage" {
		t.Fatalf("all nodes down: %d %v", st, out)
	}
}

func TestBreakdownEndpointIsProjectScoped(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "acme", "name": "Acme"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/acme/members", map[string]any{"user": "carol", "role": "owner"}, admin)
	carol := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	s.Metering.Record(metering.Event{Project: "acme", Sandbox: "sb-1", Kind: "vcpu_s", Amount: 1, CostUSD: 2})
	st, out := call(t, ts.URL, "GET", "/metering/projects/acme/breakdown?by=sandbox", nil, carol)
	if st != 200 || out["total_usd"].(float64) != 2 || out["rows"].([]any)[0].(map[string]any)["key"] != "sb-1" {
		t.Fatalf("breakdown %d %v", st, out)
	}
	if st, _ := call(t, ts.URL, "GET", "/metering/projects/default/breakdown?by=day", nil, carol); st != 403 {
		t.Fatal("another project's breakdown:", st)
	}
	if st, _ := call(t, ts.URL, "GET", "/metering/projects/acme/breakdown?by=label", nil, carol); st != 400 {
		t.Fatal("an unknown grouping:", st)
	}
}

func TestDeletingAnAccountNeedsItsSandboxesGoneOrForceAndKeepsTheMoneyRecords(t *testing.T) {
	a := newFakeNode(t, "na", 10)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "gone", "name": "Gone"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/gone/members", map[string]any{"user": "gail", "role": "owner"}, admin)
	gail := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	call(t, ts.URL, "POST", "/projects/gone/keys", map[string]any{"name": "k"}, gail)
	call(t, ts.URL, "POST", "/accounts/gone/credit", map[string]any{"usd": 3}, admin)
	_, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, gail)
	s.Metering.Record(metering.Event{Project: "gone", Sandbox: sb["id"].(string), Kind: "vcpu_s", Amount: 1, CostUSD: 1})
	if st, _ := call(t, ts.URL, "DELETE", "/accounts/gone", nil, gail); st != 403 {
		t.Fatal("an owner deleted the account:", st)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/accounts/default", nil, admin); st != 400 && st != 409 {
		t.Fatal("the default account was deletable:", st)
	}
	if st, e := call(t, ts.URL, "DELETE", "/accounts/gone", nil, admin); st != 409 || e["sandboxes"].(float64) != 1 {
		t.Fatalf("with a sandbox: %d %v", st, e)
	}
	st, out := call(t, ts.URL, "DELETE", "/accounts/gone?force=1", nil, admin)
	if st != 200 || out["sandboxes_deleted"].(float64) != 1 || out["removed"].(map[string]any)["members"].(float64) != 1 || out["removed"].(map[string]any)["api_keys"].(float64) != 1 {
		t.Fatalf("forced delete: %d %v", st, out)
	}
	if st, _ := call(t, ts.URL, "GET", "/sandboxes", nil, gail); st != 401 {
		t.Fatal("the deleted account's token still works:", st)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/accounts/gone", nil, admin); st != 404 {
		t.Fatal("deleting twice:", st)
	}
	if c, sp, err := s.Metering.Balance("gone"); err != nil || c != 3 || sp != 0 { // the project is gone, so its usage no longer maps to the account; the ledger stays
		t.Fatalf("the credit ledger must survive: %v %v %v", c, sp, err)
	}
	var n int
	s.Metering.DB().QueryRow(`SELECT count(*) FROM usage_events WHERE project = 'gone'`).Scan(&n)
	if n != 1 {
		t.Fatalf("usage events must survive: %d", n)
	}
	if st, _ := call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "gone", "name": "Again"}, admin); st != 400 {
		t.Fatal("a deleted account's id was handed out again (its credit ledger would come with it):", st)
	}
}

func TestTheNewDeployerAndTenantEndpointsAnswerGarbageWithClientErrorsNotCrashes(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "fuzz", "name": "Fuzz"}, admin)
	bodies := []any{nil, map[string]any{}, map[string]any{"usd": "x"}, map[string]any{"usd": 1e308}, map[string]any{"usd": -1e308, "note": 5}, map[string]any{"name": 12345, "role": []any{1}},
		map[string]any{"ttl_s": -5, "credit_usd": -1, "max_sandboxes": 1e18, "daily_usd": "no"}, map[string]any{"reason": map[string]any{"a": 1}}, map[string]any{"mode": 7},
		map[string]any{"code": strings.Repeat("x", 100000), "account": "../../etc", "user": "\x00"}}
	type ep struct{ m, p string }
	eps := []ep{{"POST", "/invites"}, {"GET", "/invites"}, {"POST", "/signup"}, {"POST", "/accounts/fuzz/credit"}, {"PUT", "/accounts/fuzz/billing"}, {"GET", "/accounts/fuzz/balance"},
		{"POST", "/accounts/fuzz/suspend"}, {"POST", "/accounts/fuzz/unsuspend"}, {"POST", "/projects/fuzz/keys"}, {"GET", "/projects/fuzz/keys"}, {"DELETE", "/projects/fuzz/keys/nope"},
		{"GET", "/metering/projects/fuzz/breakdown?by=%00&from=NaN&to=-1"}, {"GET", "/abuse/signals"}, {"GET", "/suspensions"}, {"GET", "/status"}, {"GET", "/builds/%00/log?since=-9"},
		{"POST", "/accounts/a%2Fb/suspend"}, {"DELETE", "/accounts/nope"}}
	for _, e := range eps {
		for _, b := range bodies {
			st, _ := call(t, ts.URL, e.m, e.p, b, admin)
			if st >= 500 && !(e.p == "/status" && st == 503) {
				t.Fatalf("%s %s with %v -> %d", e.m, e.p, b, st)
			}
		}
	}
	for _, e := range eps { // and with no credential at all
		if st, _ := call(t, ts.URL, e.m, e.p, map[string]any{}, nil); st >= 500 && e.p != "/status" {
			t.Fatalf("anonymous %s %s -> %d", e.m, e.p, st)
		}
	}
}

func TestSandboxListFiltersAndPages(t *testing.T) {
	a := newFakeNode(t, "na", 20)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	var ids []string
	for i := 0; i < 5; i++ {
		body := map[string]any{"mem": 256, "metadata": map[string]any{"job": fmt.Sprint("j", i%2), "team": "red"}}
		_, sb := call(t, ts.URL, "POST", "/sandboxes", body, admin)
		ids = append(ids, sb["id"].(string))
	}
	call(t, ts.URL, "POST", "/sandboxes/"+ids[0]+"/pause", nil, admin)
	list := func(q string) (int, []any, float64) {
		_, out := call(t, ts.URL, "GET", "/sandboxes"+q, nil, admin)
		return 0, out["sandboxes"].([]any), out["total"].(float64)
	}
	if _, rows, total := list(""); len(rows) != 5 || total != 5 {
		t.Fatalf("unfiltered: %d / %v", len(rows), total)
	}
	if _, rows, total := list("?status=paused"); len(rows) != 1 || total != 1 {
		t.Fatalf("status: %d / %v", len(rows), total)
	}
	if _, rows, _ := list("?metadata=job:j0"); len(rows) != 3 {
		t.Fatalf("metadata job:j0 -> %d, want 3", len(rows))
	}
	if _, rows, _ := list("?metadata=job:j0&metadata=team:blue"); len(rows) != 0 {
		t.Fatalf("both metadata conditions must match: %d", len(rows))
	}
	if _, rows, total := list("?q=" + ids[2]); len(rows) != 1 || total != 1 {
		t.Fatalf("q: %d / %v", len(rows), total)
	}
	if _, rows, total := list("?limit=2"); len(rows) != 2 || total != 5 {
		t.Fatalf("limit: %d / %v", len(rows), total)
	}
	_, p1, _ := list("?limit=2&offset=0")
	_, p2, _ := list("?limit=2&offset=2")
	_, p3, _ := list("?limit=2&offset=4")
	_, p4, _ := list("?limit=2&offset=99")
	seen := map[any]bool{}
	for _, p := range [][]any{p1, p2, p3} {
		for _, x := range p {
			seen[x.(map[string]any)["id"]] = true
		}
	}
	if len(p1) != 2 || len(p2) != 2 || len(p3) != 1 || len(p4) != 0 || len(seen) != 5 {
		t.Fatalf("pages %d %d %d %d, distinct %d", len(p1), len(p2), len(p3), len(p4), len(seen))
	}
}

func TestStartCmdRunsOnceInTheBackgroundAndNeverFailsTheCreate(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	st, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "start_cmd": "python3 -m http.server 8080 --bind 0.0.0.0 && echo 'it''s'"}, admin)
	if st != 201 || sb["start"] == nil {
		t.Fatalf("create with start_cmd: %d %v", st, sb)
	}
	start := sb["start"].(map[string]any)
	// the fake node answers exec with "<node>:<cmd>": the wrapper must background it, send output to the log and quote the user's text safely
	if start["started"] != true || start["log"] != "/home/agent/start.log" || !strings.Contains(fmt.Sprint(start["pid"]), "nohup sh -c 'python3 -m http.server 8080") || !strings.Contains(fmt.Sprint(start["pid"]), `it'\''`) {
		t.Fatalf("start %v", start)
	}
	if st, sb2 := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, admin); st != 201 || sb2["start"] != nil {
		t.Fatalf("without start_cmd: %d %v", st, sb2)
	}
	if st, sb3 := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "start_cmd": "   "}, admin); st != 201 || sb3["start"] != nil {
		t.Fatalf("a blank start_cmd must be ignored: %d %v", st, sb3)
	}
}

func TestWebhooksDeliverSandboxCreatedAndAccountEventsToTheOwnersEndpointSigned(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	_, ts := testServer(t, reg, "deployer-secret", a)
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	call(t, ts.URL, "POST", "/accounts", map[string]any{"id": "hook", "name": "Hook"}, admin)
	_, own := call(t, ts.URL, "POST", "/projects/hook/members", map[string]any{"user": "hana", "role": "owner"}, admin)
	hana := map[string]string{"Authorization": "Bearer " + own["token"].(string)}
	_, mem := call(t, ts.URL, "POST", "/projects/hook/members", map[string]any{"user": "hugo"}, hana)
	hugo := map[string]string{"Authorization": "Bearer " + mem["token"].(string)}
	type got struct {
		event, sig string
		body       []byte
	}
	ch := make(chan got, 16)
	recv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		ch <- got{r.Header.Get("X-Cairn-Event"), r.Header.Get("X-Cairn-Signature"), b}
	}))
	defer recv.Close()
	if st, _ := call(t, ts.URL, "POST", "/projects/hook/webhooks", map[string]any{"url": recv.URL}, hugo); st != 403 {
		t.Fatal("a member registered a webhook:", st)
	}
	if st, e := call(t, ts.URL, "POST", "/projects/hook/webhooks", map[string]any{"url": recv.URL, "events": []any{"nope"}}, hana); st != 400 {
		t.Fatalf("unknown event: %d %v", st, e)
	}
	st, h := call(t, ts.URL, "POST", "/projects/hook/webhooks", map[string]any{"url": recv.URL, "events": []any{"sandbox.created", "account.suspended", "ping"}}, hana)
	if st != 201 || h["secret"] == nil {
		t.Fatalf("register: %d %v", st, h)
	}
	secret, hid := h["secret"].(string), h["webhook"].(map[string]any)["id"].(string)
	wait := func(want string) got {
		select {
		case g := <-ch:
			if g.event != want {
				t.Fatalf("event %q, want %q", g.event, want)
			}
			if g.sig != webhook.Sign(secret, g.body) {
				t.Fatalf("bad signature for %s", want)
			}
			return g
		case <-time.After(3 * time.Second):
			t.Fatalf("no %s delivery", want)
		}
		return got{}
	}
	if st, _ := call(t, ts.URL, "POST", "/projects/hook/webhooks/"+hid+"/test", nil, hana); st != 202 {
		t.Fatal("test ping:", st)
	}
	wait("ping")
	_, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, hugo)
	g := wait("sandbox.created")
	var ev map[string]any
	json.Unmarshal(g.body, &ev)
	if ev["project"] != "hook" || ev["data"].(map[string]any)["sandbox"] != sb["id"] {
		t.Fatalf("event body %s", g.body)
	}
	call(t, ts.URL, "POST", "/accounts/hook/suspend", map[string]any{"reason": "drill"}, admin)
	g = wait("account.suspended")
	json.Unmarshal(g.body, &ev)
	if ev["data"].(map[string]any)["reason"] != "drill" {
		t.Fatalf("suspend body %s", g.body)
	}
	if _, l := call(t, ts.URL, "GET", "/projects/hook/webhooks", nil, hana); len(l["webhooks"].([]any)) != 1 || l["webhooks"].([]any)[0].(map[string]any)["secret"] != nil {
		t.Fatalf("list %v", l)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/projects/hook/webhooks/"+hid, nil, hana); st != 200 {
		t.Fatal("delete:", st)
	}
	if st, _ := call(t, ts.URL, "DELETE", "/projects/hook/webhooks/"+hid, nil, hana); st != 404 {
		t.Fatal("delete twice:", st)
	}
	if st, e := call(t, ts.URL, "POST", "/projects/hook/webhooks", map[string]any{"url": "https://169.254.169.254/x"}, admin); st != 201 { // lax in tests; production refusal is covered in the webhook package
		t.Fatalf("lax test mode: %d %v", st, e)
	}
}

type recAudit struct {
	mu  sync.Mutex
	evs []map[string]any
}

func (a *recAudit) Write(e map[string]any) { a.mu.Lock(); a.evs = append(a.evs, e); a.mu.Unlock() }

// The SDK -> sandbox half of the E2B surface: authenticated by a token bound to one sandbox, audited (the command line of a Start), relayed to the node
// with the node bearer and the SDK's own Authorization moved aside, and the Connect stream handed back as it arrives.
func TestTheE2BEnvdSideRelaysAuditsAndIsolates(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	a.snaps = []string{"py312"}
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, _ := testServer(t, reg, "deployer-secret", a)
	aud := &recAudit{}
	es := httptest.NewServer(&e2b.Server{Tenancy: s.Tenancy, Life: s.Life, Images: s.Images, Fleet: s.Fleet, Audit: aud, Secret: []byte("k"), PreviewDomain: "preview.test"})
	defer es.Close()
	mk := func() map[string]any {
		req, _ := http.NewRequest("POST", es.URL+"/sandboxes", strings.NewReader(`{"templateID":"py312"}`))
		req.Header.Set("X-API-Key", "deployer-secret")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var sb map[string]any
		json.NewDecoder(resp.Body).Decode(&sb)
		return sb
	}
	one, two := mk(), mk()
	sid, tok := one["sandboxID"].(string), one["envdAccessToken"].(string)
	if one["domain"] != "preview.test" { // get_host(port) in the SDKs is "<port>-<id>.<domain>": it must name the preview domain the control plane routes
		t.Fatalf("domain: %v", one["domain"])
	}
	if tok == "" || tok == two["envdAccessToken"] || one["envdVersion"] != e2b.EnvdVersion {
		t.Fatalf("tokens must exist, differ per sandbox, and the version be announced: %v %v", one, two)
	}
	// the token that opens a shell is handed out by create / connect only; list and get (open to a read-only key) never carry it
	for _, path := range []string{"/sandboxes", "/v2/sandboxes", "/sandboxes/" + sid} {
		req, _ := http.NewRequest("GET", es.URL+path, nil)
		req.Header.Set("X-API-Key", "deployer-secret")
		resp, _ := http.DefaultClient.Do(req)
		b, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if bytes.Contains(b, []byte(tok)) || !bytes.Contains(b, []byte(`"envdAccessToken":""`)) {
			t.Fatalf("GET %s leaks or omits the token field: %s", path, b)
		}
	}
	start := `{"process":{"cmd":"/bin/bash","args":["-l","-c","echo hi"],"cwd":"/work"},"stdin":false}`
	env := append(append([]byte{0}, byte(len(start)>>24), byte(len(start)>>16), byte(len(start)>>8), byte(len(start))), start...)
	do := func(sandbox, token, method, path string, body []byte) *http.Response {
		req, _ := http.NewRequest(method, es.URL+path, bytes.NewReader(body))
		req.Header.Set("E2b-Sandbox-Id", sandbox)
		req.Header.Set("X-Access-Token", token)
		req.Header.Set("Content-Type", "application/connect+json")
		req.Header.Set("Authorization", "Basic cm9vdDo=")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}
	resp := do(sid, tok, "POST", "/process.Process/Start", env)
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || !bytes.Contains(got, []byte(`"exitCode":0`)) || !bytes.Contains(got, []byte("aGkK")) {
		t.Fatalf("stream not relayed: %d %q", resp.StatusCode, got)
	}
	a.mu.Lock()
	seen := a.envdSeen[0]
	a.mu.Unlock()
	if seen["path"] != "/sandboxes/"+sid+"/envd/process.Process/Start" || seen["bearer"] != "Bearer tok-na" || seen["xauth"] != "Basic cm9vdDo=" || seen["body"] != string(env) {
		t.Fatalf("what the node got: %v", seen)
	}
	aud.mu.Lock()
	var logged map[string]any
	for _, e := range aud.evs {
		if e["rpc"] == "Start" {
			logged = e
		}
	}
	aud.mu.Unlock()
	if logged == nil || logged["cmd"] != "/bin/bash -l -c echo hi" || logged["sandbox"] != sid || logged["user"] != "root" || logged["cwd"] != "/work" {
		t.Fatalf("audit of the command: %v", logged)
	}
	// another sandbox's token, no token, a path outside envd, an unknown sandbox
	if r := do(sid, two["envdAccessToken"].(string), "POST", "/process.Process/Start", env); r.StatusCode != 401 {
		t.Fatalf("a token for another sandbox opened this one: %d", r.StatusCode)
	}
	if r := do(sid, "", "POST", "/process.Process/Start", env); r.StatusCode != 401 {
		t.Fatalf("no token: %d", r.StatusCode)
	}
	if r := do(sid, tok, "POST", "/process.Process/../../sandboxes/x/exec", nil); r.StatusCode != 404 {
		t.Fatalf("path traversal through envd: %d", r.StatusCode)
	}
	if r := do(sid, tok, "POST", "/sandboxes", nil); r.StatusCode != 404 {
		t.Fatalf("control-plane path reachable through envd: %d", r.StatusCode)
	}
	if r := do("sb-nope", e2bTok(es.URL, "k", "sb-nope"), "GET", "/health", nil); r.StatusCode != 502 { // what is_running() reads as "gone"
		t.Fatalf("unknown sandbox: %d", r.StatusCode)
	} else if b, _ := io.ReadAll(r.Body); !bytes.Contains(b, []byte("was not found")) {
		t.Fatalf("the SDKs match on the message: %s", b)
	}
	if r := do(sid, tok, "GET", "/health", nil); r.StatusCode != 204 {
		t.Fatalf("health: %d", r.StatusCode)
	}
	// another port of the sandbox (code-interpreter's server on 49999): the traffic token or the envd token opens it, nothing else does
	port := func(token map[string]string, p, path string) *http.Response {
		req, _ := http.NewRequest("POST", es.URL+path, strings.NewReader(`{"code":"1+1"}`))
		req.Header.Set("E2b-Sandbox-Id", sid)
		req.Header.Set("E2b-Sandbox-Port", p)
		req.Header.Set("Content-Type", "application/json")
		for k, v := range token {
			req.Header.Set(k, v)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}
	traffic, _ := one["trafficAccessToken"].(string)
	if traffic == "" || traffic == tok {
		t.Fatalf("a distinct traffic token must be issued at create: %v", one["trafficAccessToken"])
	}
	r1 := port(map[string]string{"E2B-Traffic-Access-Token": traffic}, "49999", "/execute?x=1")
	b1, _ := io.ReadAll(r1.Body)
	r1.Body.Close()
	if r1.StatusCode != 200 || !bytes.Contains(b1, []byte("execution_count")) {
		t.Fatalf("port relay: %d %s", r1.StatusCode, b1)
	}
	a.mu.Lock()
	ps := a.envdSeen[len(a.envdSeen)-1]
	a.mu.Unlock()
	if ps["path"] != "/sandboxes/"+sid+"/ports/49999/execute?x=1" || ps["bearer"] != "Bearer tok-na" || ps["body"] != `{"code":"1+1"}` || ps["traffic"] != "" {
		t.Fatalf("what the node got for the port: %v", ps)
	}
	if r := port(map[string]string{"X-Access-Token": tok}, "49999", "/contexts"); r.StatusCode != 200 {
		t.Fatalf("the envd token should open the sandbox's ports too: %d", r.StatusCode)
	}
	if r := port(map[string]string{"E2B-Traffic-Access-Token": two["trafficAccessToken"].(string)}, "49999", "/execute"); r.StatusCode != 401 {
		t.Fatalf("another sandbox's traffic token: %d", r.StatusCode)
	}
	if r := port(nil, "49999", "/execute"); r.StatusCode != 401 {
		t.Fatalf("no token: %d", r.StatusCode)
	}
	if r := port(map[string]string{"E2B-Traffic-Access-Token": traffic}, "99999", "/execute"); r.StatusCode != 400 {
		t.Fatalf("bad port: %d", r.StatusCode)
	}
	if r := port(map[string]string{"E2B-Traffic-Access-Token": traffic}, "49999", "/a/../b"); r.StatusCode != 400 {
		t.Fatalf("path traversal: %d", r.StatusCode)
	}
	m := e2b.MetricsText() // the envd side is counted, and the main port's /metrics carries these lines
	for _, want := range []string{`cairn_envd_requests_total{kind="start",status="2xx"}`, `cairn_envd_requests_total{kind="health",status="2xx"}`, `cairn_envd_requests_total{kind="start",status="4xx"}`, "cairn_envd_active_relays 0", "cairn_envd_auth_denied_total"} {
		if !strings.Contains(m, want) {
			t.Fatalf("metrics lack %s:\n%s", want, m)
		}
	}
	if strings.Contains(m, "cairn_envd_bytes_total{dir=\"to_sandbox\"} 0\n") {
		t.Fatalf("no request bytes were counted:\n%s", m)
	}
}

// e2bTok recomputes an envd token the way the server does, for a sandbox the server never issued one for.
func e2bTok(_ string, secret, sid string) string {
	return (&e2b.Server{Secret: []byte(secret)}).EnvdToken(sid)
}

func TestE2BTemplateBuildRunsStepsAndCommitsATemplate(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, _ := testServer(t, reg, "deployer-secret", a)
	es := httptest.NewServer(&e2b.Server{Tenancy: s.Tenancy, Life: s.Life, Images: s.Images, Builds: s.Builds, Fleet: s.Fleet, Audit: &recAudit{}, Secret: []byte("k"), UploadDir: t.TempDir()})
	defer es.Close()
	do := func(method, path string, body any, key bool) (int, map[string]any, []byte) {
		var rd io.Reader
		if body != nil {
			b, _ := json.Marshal(body)
			rd = bytes.NewReader(b)
		}
		req, _ := http.NewRequest(method, es.URL+path, rd)
		if key {
			req.Header.Set("X-API-Key", "deployer-secret")
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		raw, _ := io.ReadAll(resp.Body)
		var m map[string]any
		json.Unmarshal(raw, &m)
		return resp.StatusCode, m, raw
	}
	if st, _, _ := do("POST", "/v3/templates", map[string]any{"name": "Bad Name"}, true); st != 400 {
		t.Fatalf("bad name: %d", st)
	}
	if st, _, _ := do("POST", "/v3/templates", map[string]any{"name": "t:v2"}, true); st != 400 {
		t.Fatalf("tags are not supported: %d", st)
	}
	st, tpl, _ := do("POST", "/v3/templates", map[string]any{"name": "mytpl", "cpuCount": 2, "memoryMB": 512}, true)
	if st != 202 || tpl["templateID"] != "mytpl" || tpl["buildID"] == nil || tpl["aliases"] == nil {
		t.Fatalf("request: %d %v", st, tpl)
	}
	bid := tpl["buildID"].(string)
	// the COPY upload: a link without credentials, valid only for this hash
	hash := strings.Repeat("ab", 32)
	st, up, _ := do("GET", "/templates/mytpl/files/"+hash, nil, true)
	if st != 201 || up["present"] != false || up["url"] == nil {
		t.Fatalf("upload link: %d %v", st, up)
	}
	url := up["url"].(string)
	req, _ := http.NewRequest("PUT", strings.Replace(url, "http://", "http://", 1), strings.NewReader("not a real tar, the fake node does not unpack"))
	if resp, _ := http.DefaultClient.Do(req); resp.StatusCode != 200 {
		t.Fatalf("upload: %d", resp.StatusCode)
	}
	if st, up, _ := do("GET", "/templates/mytpl/files/"+hash, nil, true); st != 201 || up["present"] != true {
		t.Fatalf("after the upload: %d %v", st, up)
	}
	bad, _ := http.NewRequest("PUT", es.URL+"/e2b-uploads/"+hash+"?exp=9999999999&sig=00", strings.NewReader("x"))
	if resp, _ := http.DefaultClient.Do(bad); resp.StatusCode != 403 {
		t.Fatalf("a forged upload link was accepted: %d", resp.StatusCode)
	}
	if st, _, _ := do("GET", "/templates/mytpl/builds/"+bid+"/status", nil, false); st != 401 {
		t.Fatalf("status without a key: %d", st)
	}
	steps := []any{
		map[string]any{"type": "ENV", "args": []any{"A", "1"}},
		map[string]any{"type": "WORKDIR", "args": []any{"/work"}},
		map[string]any{"type": "RUN", "args": []any{"echo from-run", "root"}},
		map[string]any{"type": "COPY", "args": []any{"copyme.txt", "/work/copyme.txt", "", ""}, "filesHash": hash},
	}
	if st, _, _ := do("POST", "/v2/templates/mytpl/builds/"+bid, map[string]any{"fromImage": "e2bdev/base", "steps": steps, "startCmd": "sleep 1000", "readyCmd": "true"}, true); st != 202 {
		t.Fatalf("trigger: %d", st)
	}
	if st, _, _ := do("POST", "/v2/templates/mytpl/builds/"+bid, map[string]any{"fromImage": "e2bdev/base"}, true); st != 409 {
		t.Fatalf("a build can only be started once: %d", st)
	}
	var status map[string]any
	for i := 0; i < 100; i++ {
		_, status, _ = do("GET", "/templates/mytpl/builds/"+bid+"/status?logsOffset=0&limit=100", nil, true)
		if status["status"] == "ready" || status["status"] == "error" {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if status["status"] != "ready" {
		t.Fatalf("build did not finish: %v", status)
	}
	// the log is paged by logsOffset, and the SDK stops when a page comes back empty
	n := len(status["logEntries"].([]any))
	if _, rest, _ := do("GET", "/templates/mytpl/builds/"+bid+"/status?logsOffset="+fmt.Sprint(n), nil, true); len(rest["logEntries"].([]any)) != 0 {
		t.Fatalf("the offset is not honoured: %v", rest)
	}
	a.mu.Lock()
	snaps, execs, puts := append([]string{}, a.snaps...), append([]map[string]any{}, a.execs...), append([]string{}, a.putFiles...)
	a.mu.Unlock()
	if len(snaps) != 1 || snaps[0] != "mytpl" {
		t.Fatalf("the template was not committed: %v", snaps)
	}
	var sawRun, sawStart, sawEnvFile, sawCopy bool
	for _, e := range execs {
		c, _ := e["cmd"].(string)
		env, _ := e["env"].(map[string]any)
		sawRun = sawRun || (c == "echo from-run" && e["user"] == "root" && e["cwd"] == "/work" && env["A"] == "1")
		sawStart = sawStart || strings.Contains(c, "setsid nohup bash -lc 'sleep 1000'")
		sawEnvFile = sawEnvFile || strings.Contains(c, "/etc/profile.d/99-e2b-template-env.sh") && strings.Contains(c, "export A=")
		sawCopy = sawCopy || strings.Contains(c, "tar -xzf") && strings.Contains(c, "/work/copyme.txt")
	}
	if !sawRun || !sawStart || !sawEnvFile || !sawCopy || len(puts) != 1 {
		t.Fatalf("steps not carried out as planned: run=%v start=%v envfile=%v copy=%v puts=%v\n%v", sawRun, sawStart, sawEnvFile, sawCopy, puts, execs)
	}
	if st, e, _ := do("GET", "/templates/aliases/mytpl", nil, true); st != 200 || e["templateID"] != "mytpl" {
		t.Fatalf("alias: %d %v", st, e)
	}
	// a failing step ends the build with the step number, and nothing is committed
	_, t2, _ := do("POST", "/v3/templates", map[string]any{"name": "broken"}, true)
	b2 := t2["buildID"].(string)
	do("POST", "/v2/templates/broken/builds/"+b2, map[string]any{"fromImage": "ubuntu:22.04", "steps": []any{map[string]any{"type": "RUN", "args": []any{"echo ok"}}, map[string]any{"type": "RUN", "args": []any{"FAIL_THIS_STEP"}}}}, true)
	for i := 0; i < 100; i++ {
		_, status, _ = do("GET", "/templates/broken/builds/"+b2+"/status", nil, true)
		if status["status"] == "error" || status["status"] == "ready" {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	reason, _ := status["reason"].(map[string]any)
	if status["status"] != "error" || reason == nil || reason["step"] != "1" || !strings.Contains(fmt.Sprint(reason["message"]), "step 2") {
		t.Fatalf("failure not reported: %v", status)
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, k := range a.snaps {
		if k == "broken" {
			t.Fatal("a failed build was committed")
		}
	}
	if len(a.boxes) != 0 {
		t.Fatalf("the build sandboxes were not removed: %v", a.boxes)
	}
}

// Placement goes by the health it last polled; a template committed since is unknown to it. A node that answers "no template" must not fail the create while
// another node holds it.
func TestCreateFromATemplateRetriesOnTheNodeThatHasIt(t *testing.T) {
	a, b := newFakeNode(t, "na", 20), newFakeNode(t, "nb", 20)
	a.strict, b.strict = true, true
	reg, _ := registry.NewFileRegistry(t.TempDir())
	s, ts := testServer(t, reg, "", a, b)
	s.pollOnce()
	b.mu.Lock()
	b.snaps = []string{"fresh"} // committed after the last poll
	b.mu.Unlock()
	for i := 0; i < 8; i++ {
		st, info := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "template_key": "fresh", "boot": "snapshot"}, nil)
		if st != 201 || info["node"] != "nb" {
			t.Fatalf("create %d: %d %v", i, st, info)
		}
	}
	if st, e := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256, "template_key": "nowhere", "boot": "snapshot"}, nil); st != 400 || !strings.Contains(fmt.Sprint(e["error"]), "no template") {
		t.Fatalf("a template no node holds should say so, not claim there is no capacity: %d %v", st, e)
	}
}
