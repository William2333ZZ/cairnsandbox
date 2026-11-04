package httpapi

import (
	"strings"
	"sync"
	"testing"

	"fcnode/cp/internal/audit"
	"fcnode/cp/internal/registry"
	"fcnode/cp/internal/tenancy"
)

func TestRedactCmdBlanksTheUsualSecretsAndCutsLongLines(t *testing.T) {
	cases := map[string]string{
		`curl -H "Authorization: Bearer abcdef1234567890" https://x`:  "Bearer ***",
		`export API_KEY=hunter2 && run`:                               "API_KEY=***",
		`psql postgres://u:p@h/db --password=s3cret`:                  "password=***",
		`echo sk-ABCDEFGHIJKLMNOPQRSTUV and ghp_abcdefghijklmnopqrst`: "***",
		`ls -la /tmp`: "ls -la /tmp",
	}
	for in, want := range cases {
		got := redactCmd(in, 200)
		if !strings.Contains(got, want) || strings.Contains(got, "hunter2") || strings.Contains(got, "s3cret") || strings.Contains(got, "abcdef1234567890") || strings.Contains(got, "ABCDEFGHIJKLMNOPQRSTUV") || strings.Contains(got, "ghp_abcdefghijklmnopqrst") {
			t.Fatalf("redact %q -> %q (want to contain %q and no secret)", in, got, want)
		}
	}
	if got := redactCmd(strings.Repeat("a", 500), 200); len([]rune(got)) != 201 {
		t.Fatalf("not cut: %d", len([]rune(got)))
	}
	if actor(tenancy.Identity{User: tenancy.Admin, Role: "admin"}) != "deployer" || actor(tenancy.Identity{User: "bob", Role: "member"}) != "bob" {
		t.Fatal("actor names")
	}
}

func TestExecAndFilesLeaveRowsInTheChainWithProjectAndRedactedCommand(t *testing.T) {
	a := newFakeNode(t, "na", 5)
	reg, _ := registry.NewFileRegistry(t.TempDir())
	srv, ts := testServer(t, reg, "deployer-secret", a)
	var mu sync.Mutex
	rows := []map[string]any{}
	srv.Audit.(*audit.Log).Tap(func(ev map[string]any) {
		if op, _ := ev["op"].(string); op == "exec" || strings.HasPrefix(op, "files_") {
			mu.Lock()
			rows = append(rows, ev)
			mu.Unlock()
		}
	})
	admin := map[string]string{"Authorization": "Bearer deployer-secret"}
	_, sb := call(t, ts.URL, "POST", "/sandboxes", map[string]any{"mem": 256}, admin)
	sid := sb["id"].(string)
	srv.AuditExec = false
	call(t, ts.URL, "POST", "/sandboxes/"+sid+"/exec", map[string]any{"cmd": "off"}, admin)
	mu.Lock()
	if len(rows) != 0 {
		t.Fatalf("rows with CP_AUDIT_EXEC=0: %v", rows)
	}
	mu.Unlock()
	srv.AuditExec = true
	if st, _ := call(t, ts.URL, "POST", "/sandboxes/"+sid+"/exec", map[string]any{"cmd": "curl -H 'Authorization: Bearer abcdef1234567890' x", "user": "root"}, admin); st != 200 {
		t.Fatal("exec", st)
	}
	call(t, ts.URL, "PUT", "/sandboxes/"+sid+"/files?path=/tmp/a.txt", nil, admin)
	mu.Lock()
	defer mu.Unlock()
	var ex map[string]any
	for _, r := range rows {
		if r["op"] == "exec" {
			ex = r
		}
	}
	if ex == nil {
		t.Fatalf("no exec row: %v", rows)
	}
	if ex["by"] != "deployer" || ex["project"] != "default" || ex["sandbox"] != sid || ex["user"] != "root" || ex["exit"] != float64(0) && ex["exit"] != 0 {
		t.Fatalf("exec row %v", ex)
	}
	if c, _ := ex["cmd"].(string); !strings.Contains(c, "Bearer ***") || strings.Contains(c, "abcdef1234567890") {
		t.Fatalf("command not redacted: %q", c)
	}
}
