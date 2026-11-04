package registry

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
)

func newFakeRedis(t *testing.T) *miniredis.Miniredis { return miniredis.RunT(t) }

func registries(t *testing.T) map[string]Registry {
	fr, err := NewFileRegistry(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	fake := newFakeRedis(t)
	rr := NewRedisRegistry(fake.Addr(), 0, "t:")
	fakes[rr] = fake
	return map[string]Registry{"file": fr, "redis": rr}
}

func TestRoutesUsersNodesRoundTripOnBothBackends(t *testing.T) {
	for kind, reg := range registries(t) {
		t.Run(kind, func(t *testing.T) {
			if err := reg.Ping(); err != nil {
				t.Fatal(err)
			}
			if r, _ := reg.RouteGet("x"); r != nil {
				t.Fatal("route from nothing")
			}
			reg.RouteSet("sb1", Route{Node: "n1", Class: "persistent", Durable: true, Created: 1.5})
			reg.RouteSet("sb2", Route{Node: "n2"})
			r, _ := reg.RouteGet("sb1")
			if r == nil || r.Node != "n1" || !r.Durable || r.Created != 1.5 {
				t.Fatalf("got %+v", r)
			}
			all, _ := reg.Routes()
			if len(all) != 2 || all["sb2"].Node != "n2" {
				t.Fatalf("routes %+v", all)
			}
			reg.RouteDel("sb1")
			reg.RouteDel("never")
			if all, _ = reg.Routes(); len(all) != 1 {
				t.Fatalf("after del %+v", all)
			}
			if us, _ := reg.Users(); len(us) != 0 {
				t.Fatal("users from nothing")
			}
			reg.UsersSave([]User{{User: "alice", Name: "A", TokenSHA256: "h", Created: 1}})
			us, _ := reg.Users()
			if len(us) != 1 || us[0].User != "alice" {
				t.Fatalf("users %+v", us)
			}
			reg.NodePut(NodeSpec{Name: "n9", URL: "http://n9:8800", TokenFile: "/x"})
			reg.NodePut(NodeSpec{Name: "n9", URL: "http://n9:8801"})
			ns, _ := reg.Nodes()
			if len(ns) != 1 || ns[0].URL != "http://n9:8801" {
				t.Fatalf("nodes %+v", ns)
			}
		})
	}
}

func TestLeasesAreExclusiveRenewableAndExpire(t *testing.T) {
	for kind, reg := range registries(t) {
		t.Run(kind, func(t *testing.T) {
			tok, err := reg.Acquire("job", 200*time.Millisecond)
			if err != nil || tok == "" {
				t.Fatal("first acquire failed", err)
			}
			if tok2, _ := reg.Acquire("job", 200*time.Millisecond); tok2 != "" {
				t.Fatal("second holder")
			}
			if ok, _ := reg.Renew("job", "wrong", time.Second); ok {
				t.Fatal("renewed with a wrong token")
			}
			if ok, _ := reg.Renew("job", tok, 100*time.Millisecond); !ok {
				t.Fatal("renew failed")
			}
			reg.Release("job", "wrong")
			if tok2, _ := reg.Acquire("job", time.Second); tok2 != "" {
				t.Fatal("release with a wrong token freed it")
			}
			reg.Release("job", tok)
			tok3, _ := reg.Acquire("job", 50*time.Millisecond)
			if tok3 == "" {
				t.Fatal("not free after release")
			}
			if fake, ok := fakes[reg]; ok {
				fake.FastForward(80 * time.Millisecond) // miniredis does not tick time by itself
			} else {
				time.Sleep(80 * time.Millisecond)
			}
			if tok4, _ := reg.Acquire("job", time.Second); tok4 == "" {
				t.Fatal("expired lease still held")
			}
		})
	}
}

func TestFileRegistryPersistsRoutesAcrossRestarts(t *testing.T) {
	dir := t.TempDir()
	a, _ := NewFileRegistry(dir)
	a.RouteSet("sb", Route{Node: "n1"})
	b, _ := NewFileRegistry(dir)
	if r, _ := b.RouteGet("sb"); r == nil || r.Node != "n1" {
		t.Fatal("routes.json not read back")
	}
	os.WriteFile(filepath.Join(dir, "routes.json"), []byte("{broken"), 0o644)
	c, _ := NewFileRegistry(dir)
	if all, _ := c.Routes(); len(all) != 0 {
		t.Fatal("a corrupt file must read as empty, not fail")
	}
}

func TestRedisRegistrySurvivesAServerRestartAndReportsNoServer(t *testing.T) {
	fake := newFakeRedis(t)
	reg := NewRedisRegistry(fake.Addr(), 0, "t:")
	reg.RouteSet("a", Route{Node: "n"})
	fake.Close()
	if _, err := reg.RouteGet("a"); err == nil {
		t.Fatal("a dead registry must surface as an error, not as an empty route")
	}
	if err := fake.Restart(); err != nil {
		t.Fatal(err)
	}
	if r, err := reg.RouteGet("a"); err != nil || r == nil || r.Node != "n" {
		t.Fatalf("after restart: %v %v", r, err)
	}
	if err := NewRedisRegistry("127.0.0.1:1", 0, "t:").Ping(); err == nil {
		t.Fatal("ping to nothing succeeded")
	}
}

func TestOpenRegistryParsesTheSpec(t *testing.T) {
	if r, err := Open("", t.TempDir()); err != nil || r.Kind() != "file" {
		t.Fatal(r, err)
	}
	r, err := Open("redis://h:1234/2", t.TempDir())
	if err != nil || r.Kind() != "redis" || r.Where() != "redis://h:1234/2" {
		t.Fatal(r, err)
	}
	if _, err := Open("etcd://x", t.TempDir()); err == nil {
		t.Fatal("unsupported scheme accepted")
	}
}

var fakes = map[Registry]*miniredis.Miniredis{} // registry -> the miniredis behind it (for clock control)
