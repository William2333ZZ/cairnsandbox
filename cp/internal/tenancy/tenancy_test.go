package tenancy

import (
	"testing"
	"time"

	"fcnode/cp/internal/db"
	"fcnode/cp/internal/registry"
)

type legacy []registry.User

func (l legacy) Users() ([]registry.User, error) { return l, nil }

func open(t *testing.T, token string) *Tenancy {
	return New(db.OpenForTest(t), token)
}

func TestBootstrapImportsLegacyUsersOnceAndTheDeployerIsAdmin(t *testing.T) {
	tn := open(t, "deployer-secret")
	if err := tn.Bootstrap(legacy{{User: "alice", Name: "A", TokenSHA256: sha("alice-token"), Created: 1}}); err != nil {
		t.Fatal(err)
	}
	if err := tn.Bootstrap(legacy{{User: "bob", TokenSHA256: sha("x")}}); err != nil { // second bootstrap: members exist, nothing imported
		t.Fatal(err)
	}
	ms, _ := tn.Members(DefaultAccount)
	if len(ms) != 1 || ms[0].User != "alice" || ms[0].Role != RoleMember {
		t.Fatalf("members %+v", ms)
	}
	id, ok := tn.Resolve("alice-token")
	if !ok || id.User != "alice" || id.Account != DefaultAccount || id.Admin() {
		t.Fatalf("alice %+v %v", id, ok)
	}
	if id, ok := tn.Resolve("deployer-secret"); !ok || !id.Admin() || id.User != Admin {
		t.Fatalf("deployer %+v", id)
	}
	if _, ok := tn.Resolve("nope"); ok {
		t.Fatal("stranger resolved")
	}
	if _, ok := tn.Resolve(""); ok {
		t.Fatal("empty bearer resolved")
	}
	if p, _ := tn.DefaultProjectOf(DefaultAccount); p != DefaultProject {
		t.Fatal("default project", p)
	}
	if id, ok := open(t, "").Resolve(""); !ok || !id.Admin() {
		t.Fatal("with no CP_TOKEN everyone is the admin")
	}
}

func TestAccountsProjectsMembersAndQuotas(t *testing.T) {
	tn := open(t, "s")
	tn.Bootstrap(nil)
	a, err := tn.AccountAdd("acme", "Acme Inc")
	if err != nil || a.ID != "acme" {
		t.Fatal(a, err)
	}
	if _, err := tn.AccountAdd("acme", ""); err == nil {
		t.Fatal("duplicate account")
	}
	if _, err := tn.AccountAdd("Bad Name", ""); err == nil {
		t.Fatal("bad id accepted")
	}
	ps, _ := tn.Projects("acme")
	if len(ps) != 1 || ps[0].ID != "acme" || ps[0].Name != "default" {
		t.Fatalf("a new account gets a default project: %+v", ps)
	}
	p2, err := tn.ProjectAdd("acme", "", "web")
	if err != nil || p2.ID != "acme-web" {
		t.Fatal(p2, err)
	}
	if _, err := tn.ProjectAdd("nope", "", "x"); err != ErrNotFound {
		t.Fatal("project on a missing account")
	}
	owner, err := tn.MemberAdd("acme", "carol", "Carol", RoleOwner)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tn.MemberAdd("acme", "carol", "", ""); err == nil {
		t.Fatal("duplicate user")
	}
	if _, err := tn.MemberAdd("acme", "dave", "", "king"); err == nil {
		t.Fatal("bad role")
	}
	cid, ok := tn.Resolve(owner["token"].(string))
	if !ok || cid.Account != "acme" || cid.Role != RoleOwner {
		t.Fatalf("carol %+v", cid)
	}
	// project resolution: own account's projects yes, another account's no, the admin anything
	if p, err := tn.ResolveProject(cid, ""); err != nil || p != "acme" {
		t.Fatal(p, err)
	}
	if p, err := tn.ResolveProject(cid, "acme-web"); err != nil || p != "acme-web" {
		t.Fatal(p, err)
	}
	if _, err := tn.ResolveProject(cid, DefaultProject); err != ErrForbidden {
		t.Fatal("crossed accounts:", err)
	}
	if _, err := tn.ResolveProject(cid, "ghost"); err != ErrNotFound {
		t.Fatal(err)
	}
	admin, _ := tn.Resolve("s")
	if p, err := tn.ResolveProject(admin, "acme-web"); err != nil || p != "acme-web" {
		t.Fatal(p, err)
	}
	// quotas: none = unlimited; set; refuse negatives and missing projects
	if q, _ := tn.Quota("acme-web"); q.MaxSandboxes != 0 || q.DailyUSD != 0 {
		t.Fatal("no row must read as unlimited")
	}
	q, err := tn.QuotaSet("acme-web", Quota{MaxSandboxes: 3, DailyUSD: 2.5})
	if err != nil || q.Project != "acme-web" {
		t.Fatal(q, err)
	}
	q, _ = tn.QuotaSet("acme-web", Quota{MaxSandboxes: 4, DailyUSD: 2.5})
	if got, _ := tn.Quota("acme-web"); got.MaxSandboxes != 4 || got.DailyUSD != 2.5 {
		t.Fatalf("quota %+v", got)
	}
	if _, err := tn.QuotaSet("acme-web", Quota{MaxSandboxes: -1}); err == nil {
		t.Fatal("negative quota")
	}
	if _, err := tn.QuotaSet("ghost", Quota{}); err != ErrNotFound {
		t.Fatal(err)
	}
	if err := tn.MemberDelete("acme", "carol"); err != nil {
		t.Fatal(err)
	}
	if err := tn.MemberDelete("acme", "carol"); err != ErrNotFound {
		t.Fatal("deleted twice")
	}
	if _, ok := tn.Resolve(owner["token"].(string)); ok {
		t.Fatal("deleted member still resolves")
	}
	me := tn.Describe(admin)
	if me["admin"] != true || len(me["projects"].([]string)) != 1 {
		t.Fatalf("me %v", me)
	}
}

func TestTheLegacyUsersSurfaceIsTheDefaultAccount(t *testing.T) {
	tn := open(t, "s")
	tn.Bootstrap(nil)
	r, err := tn.UserAdd("erin", "Erin")
	if err != nil || len(r["token"].(string)) != 48 {
		t.Fatal(r, err)
	}
	us, _ := tn.UsersList()
	if len(us) != 1 || us[0]["user"] != "erin" || us[0]["role"] != RoleMember {
		t.Fatalf("users %v", us)
	}
	for _, bad := range []string{"Erin", "e", "default", "erin"} {
		if _, err := tn.UserAdd(bad, ""); err == nil {
			t.Fatal("accepted", bad)
		}
	}
	if err := tn.UserDelete("erin"); err != nil {
		t.Fatal(err)
	}
	if err := tn.UserDelete("erin"); err != ErrNotFound {
		t.Fatal(err)
	}
}

func TestAnExpiredKeyIsNoKeyAndAKeyReachesOnlyItsProject(t *testing.T) {
	tn := open(t, "deployer-secret")
	if err := tn.Bootstrap(legacy{}); err != nil {
		t.Fatal(err)
	}
	res, err := tn.KeyAdd(DefaultProject, "short", "viewer", 0.05, "test")
	if err != nil {
		t.Fatal(err)
	}
	secret := res["secret"].(string)
	id, ok := tn.Resolve(secret)
	if !ok || id.Role != RoleViewer || id.Project != DefaultProject || id.Account != DefaultAccount {
		t.Fatalf("key identity %+v %v", id, ok)
	}
	if p, err := tn.ResolveProject(id, ""); err != nil || p != DefaultProject {
		t.Fatalf("default project of a key: %q %v", p, err)
	}
	if _, err := tn.ResolveProject(id, "other"); err == nil {
		t.Fatal("a key named another project")
	}
	time.Sleep(120 * time.Millisecond)
	if _, ok := tn.Resolve(secret); ok {
		t.Fatal("an expired key still resolves")
	}
	if _, err := tn.KeyAdd(DefaultProject, "bad", "owner", 0, "test"); err == nil {
		t.Fatal("an owner key")
	}
}
