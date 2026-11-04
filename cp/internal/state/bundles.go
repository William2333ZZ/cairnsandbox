// Package state is context 5 (docs/002 沙箱侧的领域 状态:快照与卷) as the control plane sees it today: the published bundles in the chunk store
// (node/imgstore.py's HTTP surface). Checkpoints, rollback and volumes are not here yet.
package state

import (
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
)

type Store struct{ URL string } // empty = no chunk store

func (s Store) Enabled() bool { return s.URL != "" }

func (s Store) Bundles() []string {
	if s.URL == "" {
		return []string{}
	}
	resp, err := http.Get(s.URL + "/bundles.json")
	if err != nil {
		return []string{}
	}
	defer resp.Body.Close()
	var doc struct {
		Bundles []string `json:"bundles"`
	}
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 || json.Unmarshal(data, &doc) != nil || doc.Bundles == nil {
		return []string{}
	}
	return doc.Bundles
}

// LatestBundle: the newest sb-<id>-<ts> bundle of a sandbox, or "".
func (s Store) LatestBundle(sid string) string {
	best, bestTS := "", int64(-1)
	for _, b := range s.Bundles() {
		if !strings.HasPrefix(b, "sb-"+sid+"-") {
			continue
		}
		i := strings.LastIndex(b, "-")
		ts, err := strconv.ParseInt(b[i+1:], 10, 64)
		if err == nil && ts > bestTS {
			best, bestTS = b, ts
		}
	}
	return best
}

func (s Store) GC() any {
	if s.URL == "" {
		return map[string]any{"error": "no store"}
	}
	resp, err := http.Post(s.URL+"/gc", "application/json", strings.NewReader(""))
	if err != nil {
		return map[string]any{"error": err.Error()}
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var out any
	if resp.StatusCode != 200 || json.Unmarshal(data, &out) != nil {
		return map[string]any{"error": resp.StatusCode}
	}
	return out
}
