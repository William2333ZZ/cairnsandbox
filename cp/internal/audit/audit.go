// Package audit is the per-replica append-only log of what the control plane did (cp-state/audit.jsonl). Every replica writes its own file
// with its name on every line; it is evidence, not shared state.
package audit

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type Log struct {
	taps    []func(map[string]any) // D72: observers of every line written (webhooks); they must not block
	replica string
	mu      sync.Mutex
	f       *os.File
	prev    string // hash of the last line written: every line carries prev and its own h, so a removed or edited line breaks the chain (013 威胁模型)
}

func Open(stateDir, replica string) (*Log, error) {
	p := filepath.Join(stateDir, "audit.jsonl")
	prev := lastHash(p)
	f, err := os.OpenFile(p, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	return &Log{replica: replica, f: f, prev: prev}, nil
}

// Discard is a log that keeps nothing (tests).
func Discard(replica string) *Log { return &Log{replica: replica} }

func (l *Log) Replica() string { return l.replica }

func (l *Log) Write(ev map[string]any) {
	if l == nil {
		return
	}
	ev["ts"] = float64(int64(float64(time.Now().UnixNano())/1e6+0.5)) / 1000
	ev["replica"] = l.replica
	l.mu.Lock()
	defer l.mu.Unlock()
	ev["prev"] = l.prev
	delete(ev, "h")
	body, _ := json.Marshal(ev)
	h := hashOf(l.prev, body)
	ev["h"] = h
	b, _ := json.Marshal(ev)
	l.prev = h
	if l.f != nil {
		l.f.Write(append(b, '\n'))
	}
	if len(l.taps) > 0 {
		cp := make(map[string]any, len(ev))
		for k, v := range ev {
			cp[k] = v
		}
		for _, t := range l.taps {
			t(cp)
		}
	}
}

// Tap registers an observer that sees a copy of every line after it is written. It runs under the log's lock: hand the event to a queue and return.
func (l *Log) Tap(fn func(map[string]any)) {
	if l == nil {
		return
	}
	l.mu.Lock()
	l.taps = append(l.taps, fn)
	l.mu.Unlock()
}

// hashOf chains a line to the one before it: sha256(prev || line-without-h), hex, 16 bytes.
func hashOf(prev string, lineWithoutH []byte) string {
	sum := sha256.Sum256(append([]byte(prev), lineWithoutH...))
	return hex.EncodeToString(sum[:])[:32]
}

func lastHash(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	last := ""
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		if t := strings.TrimSpace(sc.Text()); t != "" {
			last = t
		}
	}
	var ev map[string]any
	if json.Unmarshal([]byte(last), &ev) == nil {
		if h, _ := ev["h"].(string); h != "" {
			return h
		}
	}
	return ""
}

// Verify walks a log and recomputes the chain. Lines from before the chain existed (no h) are skipped as "legacy"; the first break is reported.
func Verify(path string) (lines, legacy int, firstBad int, err error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, 0, 0, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	prev := ""
	for sc.Scan() {
		t := strings.TrimSpace(sc.Text())
		if t == "" {
			continue
		}
		lines++
		var ev map[string]any
		if json.Unmarshal([]byte(t), &ev) != nil {
			if firstBad == 0 {
				firstBad = lines
			}
			continue
		}
		h, _ := ev["h"].(string)
		if h == "" {
			legacy++
			continue
		}
		p, _ := ev["prev"].(string)
		delete(ev, "h")
		body, _ := json.Marshal(ev)
		if p != prev || hashOf(p, body) != h {
			if firstBad == 0 {
				firstBad = lines
			}
		}
		prev = h
	}
	return lines, legacy, firstBad, sc.Err()
}

// Tail returns the last n parsed lines of a log (oldest first).
func Tail(path string, n int) ([]map[string]any, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return []map[string]any{}, nil
		}
		return nil, err
	}
	defer f.Close()
	ring := make([]string, 0, n)
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		t := strings.TrimSpace(sc.Text())
		if t == "" {
			continue
		}
		if len(ring) == n {
			ring = ring[1:]
		}
		ring = append(ring, t)
	}
	out := make([]map[string]any, 0, len(ring))
	for _, t := range ring {
		var ev map[string]any
		if json.Unmarshal([]byte(t), &ev) == nil {
			out = append(out, ev)
		}
	}
	return out, sc.Err()
}
