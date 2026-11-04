package audit

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTheChainLinksLinesAndVerifyFindsATamperedOne(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(dir, "r1")
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		l.Write(map[string]any{"op": "x", "i": i})
	}
	l.f.Close()
	p := filepath.Join(dir, "audit.jsonl")
	if n, legacy, bad, err := Verify(p); err != nil || n != 5 || legacy != 0 || bad != 0 {
		t.Fatalf("clean log: n=%d legacy=%d bad=%d err=%v", n, legacy, bad, err)
	}
	// a replica restart continues the chain from the last line
	l2, _ := Open(dir, "r1")
	l2.Write(map[string]any{"op": "y"})
	l2.f.Close()
	if n, _, bad, _ := Verify(p); n != 6 || bad != 0 {
		t.Fatalf("after reopen: n=%d bad=%d", n, bad)
	}
	// edit line 3: the chain breaks there
	b, _ := os.ReadFile(p)
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	lines[2] = strings.Replace(lines[2], `"i":2`, `"i":7`, 1)
	os.WriteFile(p, []byte(strings.Join(lines, "\n")+"\n"), 0o644)
	if _, _, bad, _ := Verify(p); bad != 3 {
		t.Fatalf("tampered line not found: first bad %d", bad)
	}
	// remove line 2: the chain breaks at the line that followed it
	lines = append(lines[:1], lines[2:]...)
	os.WriteFile(p, []byte(strings.Join(lines, "\n")+"\n"), 0o644)
	if _, _, bad, _ := Verify(p); bad != 2 {
		t.Fatalf("removed line not found: first bad %d", bad)
	}
}
