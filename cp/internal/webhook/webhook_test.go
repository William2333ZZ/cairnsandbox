package webhook

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"sync"
	"testing"
	"time"

	"fcnode/cp/internal/db"
)

type res struct{}

func (res) ProjectOfSandbox(id string) string   { return map[string]string{"sb-1": "p1"}[id] }
func (res) ProjectOfBuild(id string) string     { return map[string]string{"b-1": "p1"}[id] }
func (res) ProjectsOfAccount(a string) []string { return map[string][]string{"acme": {"p1", "p2"}}[a] }

func newD(t *testing.T) *Dispatcher {
	d := New(db.OpenForTest(t), res{})
	d.AllowPrivateForTests()
	d.backoff = []time.Duration{0, 10 * time.Millisecond, 10 * time.Millisecond}
	return d
}

func TestForbiddenAddressesAndURLs(t *testing.T) {
	for _, ip := range []string{"127.0.0.1", "10.1.2.3", "192.168.0.9", "172.16.5.5", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1", "224.0.0.1", "::ffff:10.0.0.1", "255.255.255.255"} {
		if !forbidden(netip.MustParseAddr(ip)) {
			t.Errorf("%s must be refused", ip)
		}
	}
	for _, ip := range []string{"8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"} {
		if forbidden(netip.MustParseAddr(ip)) {
			t.Errorf("%s is public", ip)
		}
	}
	for _, u := range []string{"http://example.com/x", "https://", "ftp://example.com", "https://user:pw@example.com/", "https://127.0.0.1/x", "https://localhost/x", "https://[::1]/", "https://169.254.169.254/latest/meta-data", "not a url"} {
		if ValidURL(u, false) == nil {
			t.Errorf("%q must be refused", u)
		}
	}
	if ValidURL("https://hooks.example.com/aios?x=1", false) != nil {
		t.Error("a normal https URL was refused")
	}
}

func TestTheDialerRefusesPrivateTargetsEvenWhenTheNameLooksFine(t *testing.T) {
	d := New(db.OpenForTest(t), res{}) // production mode: allowPrivate false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Error("a request reached a loopback server") }))
	defer srv.Close()
	req, _ := http.NewRequest("POST", srv.URL, nil) // http://127.0.0.1:port: the guard sees the resolved address at connect time
	if resp, err := d.client.Do(req); err == nil {
		resp.Body.Close()
		t.Fatal("the guarded client connected to loopback")
	}
}

func TestDeliveriesAreSignedRetriedAndTheHookSwitchesOffAfterARunOfFailures(t *testing.T) {
	d := newD(t)
	var mu sync.Mutex
	var got []http.Header
	var bodies [][]byte
	failFor := 2
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		got, bodies = append(got, r.Header.Clone()), append(bodies, b)
		fail := failFor > 0
		if fail {
			failFor--
		}
		mu.Unlock()
		if fail {
			http.Error(w, "later", 500)
			return
		}
		w.WriteHeader(204)
	}))
	defer srv.Close()
	h, secret, err := d.Add("p1", srv.URL+"/hook", []string{"sandbox.created"}, "me")
	if err != nil || secret == "" {
		t.Fatal(err)
	}
	d.Run(1)
	d.Observe(map[string]any{"op": "create", "status": 201, "sandbox": "sb-1", "node": "n", "project": "p1"})
	d.Observe(map[string]any{"op": "create", "status": 429, "project": "p1"})                       // a refused create is not an event
	d.Observe(map[string]any{"op": "volume_create", "project": "p1"})                               // not a public event
	d.Observe(map[string]any{"op": "create", "status": 201, "sandbox": "sb-9", "project": "other"}) // someone else's project
	time.Sleep(300 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 3 { // two failures, then the retry that works
		t.Fatalf("attempts: %d", len(got))
	}
	last := got[2]
	if last.Get("X-Cairn-Event") != "sandbox.created" || last.Get("X-Cairn-Signature") != Sign(secret, bodies[2]) || last.Get("X-Cairn-Delivery") == "" {
		t.Fatalf("headers %v", last)
	}
	var ev map[string]any
	json.Unmarshal(bodies[2], &ev)
	if ev["type"] != "sandbox.created" || ev["project"] != "p1" || ev["data"].(map[string]any)["sandbox"] != "sb-1" {
		t.Fatalf("body %s", bodies[2])
	}
	hs, _ := d.List("p1")
	if len(hs) != 1 || hs[0].LastCode != 204 || hs[0].Failures != 0 || hs[0].Disabled {
		t.Fatalf("after success: %+v", hs)
	}
	for i := 0; i < maxFailures; i++ { // a run of failed deliveries switches it off
		d.record(h.ID, 500, false)
	}
	hs, _ = d.List("p1")
	if !hs[0].Disabled || hs[0].Failures != maxFailures {
		t.Fatalf("not disabled: %+v", hs[0])
	}
	if !d.Enable("p1", h.ID) {
		t.Fatal("enable")
	}
	if hs, _ = d.List("p1"); hs[0].Disabled || hs[0].Failures != 0 {
		t.Fatalf("not re-enabled: %+v", hs[0])
	}
}

func TestAccountEventsGoToEveryProjectOfTheAccountAndLimitsAndNamesAreChecked(t *testing.T) {
	d := newD(t)
	var mu sync.Mutex
	hits := map[string]int{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		hits[r.URL.Path+" "+r.Header.Get("X-Cairn-Event")]++
		mu.Unlock()
	}))
	defer srv.Close()
	d.Add("p1", srv.URL+"/p1", nil, "me") // empty = every event
	d.Add("p2", srv.URL+"/p2", []string{"account.suspended"}, "me")
	d.Add("p2", srv.URL+"/p2only-credit", []string{"account.credit"}, "me")
	d.Run(2)
	d.Observe(map[string]any{"op": "account_suspend", "account": "acme", "reason": "mining"})
	d.Observe(map[string]any{"op": "network_relax_requested", "sandbox": "sb-1", "request": "nr-1"})
	d.Observe(map[string]any{"op": "build_done", "build": "b-1", "kind": "oci"})
	time.Sleep(200 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if hits["/p1 account.suspended"] != 1 || hits["/p2 account.suspended"] != 1 || hits["/p2only-credit account.suspended"] != 0 || hits["/p1 network.approval_requested"] != 1 || hits["/p1 build.finished"] != 1 {
		t.Fatalf("hits %v", hits)
	}
	if _, _, err := d.Add("p1", srv.URL, []string{"nope.event"}, "me"); err == nil {
		t.Fatal("an unknown event was accepted")
	}
	d.MaxPerProject = 1
	if _, _, err := d.Add("p1", srv.URL+"/again", nil, "me"); err == nil {
		t.Fatal("the per-project limit was not enforced")
	}
}
