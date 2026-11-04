package images

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"time"

	"fcnode/cp/internal/db"
	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/lifecycle"
	"fcnode/cp/internal/registry"
)

// Build (docs/002 沙箱侧 4 Build; D37): one asynchronous job that produces a Layer or a Template on a node — kind "oci" pulls an image into a layer,
// kind "commit" turns a running sandbox into a named template. The record is in SQL; the artefact lives on the node (and in the store once exported).
type Build struct {
	ID       string         `json:"id"`
	Project  string         `json:"project"`
	Kind     string         `json:"kind"`
	Input    map[string]any `json:"input"`
	Node     string         `json:"node"`
	Status   string         `json:"status"` // running | done | failed
	Result   map[string]any `json:"result,omitempty"`
	Error    string         `json:"error,omitempty"`
	Created  float64        `json:"created"`
	Finished float64        `json:"finished,omitempty"`
}

var ErrNotFound = errors.New("not found")
var nameRe = regexp.MustCompile(`^[a-z][a-z0-9_.-]{0,39}$`)

// Builds runs builds and keeps their records.
type Builds struct {
	db     *db.DB
	images *Images
	nodeOf func(sandbox string) string // lifecycle.Route lookup, for commits
}

func NewBuilds(d *db.DB, im *Images, nodeOf func(string) string) *Builds {
	return &Builds{db: d, images: im, nodeOf: nodeOf}
}

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// StartArchive: an image archive (docker save / OCI layout) uploaded as bytes becomes a layer on a node; the archive is forwarded, not stored.
func (b *Builds) StartArchive(project, name, nodeName string, data []byte) (Build, int, error) {
	if name == "" {
		name = "archive"
	}
	if !nameRe.MatchString(name) {
		return Build{}, 400, errors.New("layer name: a letter, then letters, digits, _ . -, up to 40 chars")
	}
	if len(data) == 0 {
		return Build{}, 400, errors.New("upload the archive as the body")
	}
	var n *fleet.Node
	if nodeName != "" {
		n = b.images.fleet.Get(nodeName)
	} else {
		n = b.images.fleet.LeastLoaded(map[string]any{"mem": 0.0}, nil)
	}
	if n == nil || !n.Alive() {
		return Build{}, 429, errors.New("no node for the build")
	}
	bl := Build{ID: "b-" + registry.NewToken()[:10], Project: project, Kind: "archive", Input: map[string]any{"name": name, "archive_bytes": len(data)}, Node: n.Name, Status: "running", Created: now()}
	in, _ := json.Marshal(bl.Input)
	if _, err := b.db.Exec(`INSERT INTO builds(id, project, kind, input, node, status, created) VALUES ($1, $2, $3, $4, $5, $6, $7)`, bl.ID, bl.Project, bl.Kind, string(in), bl.Node, bl.Status, bl.Created); err != nil {
		return Build{}, 500, err
	}
	b.images.audit.Write(map[string]any{"op": "build_start", "build": bl.ID, "kind": "archive", "node": n.Name, "project": project, "bytes": len(data)})
	go func() {
		hdr := http.Header{"Content-Type": {"application/x-tar"}}
		resp, err := b.images.nc.Do(n, "POST", "/layers/from-archive?name="+url.QueryEscape(name), data, 30*time.Minute, hdr)
		status, msg := "done", ""
		out := map[string]any{}
		if err != nil {
			status, msg = "failed", "node unreachable: "+err.Error()
		} else {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			_ = json.Unmarshal(body, &out)
			if resp.StatusCode != 201 {
				status, msg = "failed", fmt.Sprintf("node %d: %v", resp.StatusCode, out["error"])
			}
		}
		res, _ := json.Marshal(out)
		b.db.Exec(`UPDATE builds SET status = $1, result = $2, error = $3, finished = $4 WHERE id = $5`, status, string(res), msg, now(), bl.ID)
		b.images.audit.Write(map[string]any{"op": "build_" + status, "build": bl.ID, "kind": "archive", "node": n.Name, "error": msg})
	}()
	return bl, 201, nil
}

// Start validates, records and launches the build; returns the record in status running.
func (b *Builds) Start(project, kind string, input map[string]any) (Build, int, error) {
	var n *fleet.Node
	switch kind {
	case "oci":
		img, _ := input["image"].(string)
		if img == "" {
			return Build{}, 400, errors.New("image is required (e.g. python:3.12-slim)")
		}
		if name, _ := input["name"].(string); name != "" && !nameRe.MatchString(name) {
			return Build{}, 400, errors.New("layer name: a letter, then letters, digits, _ . -, up to 40 chars")
		}
		if want, _ := input["node"].(string); want != "" {
			n = b.images.fleet.Get(want)
		} else {
			n = b.images.fleet.LeastLoaded(map[string]any{"mem": 0.0}, nil)
		}
	case "commit":
		sid, _ := input["sandbox"].(string)
		name, _ := input["name"].(string)
		if sid == "" || !nameRe.MatchString(name) {
			return Build{}, 400, errors.New("commit needs sandbox and a template name (a letter, then letters, digits, _ . -)")
		}
		if nn := b.nodeOf(sid); nn != "" {
			n = b.images.fleet.Get(nn)
		}
		if n == nil {
			return Build{}, 404, errors.New("no sandbox " + sid)
		}
	default:
		return Build{}, 400, errors.New("kind must be oci or commit")
	}
	if n == nil || !n.Alive() {
		return Build{}, 429, errors.New("no node for the build")
	}
	bl := Build{ID: "b-" + registry.NewToken()[:10], Project: project, Kind: kind, Input: input, Node: n.Name, Status: "running", Created: now()}
	in, _ := json.Marshal(input)
	if _, err := b.db.Exec(`INSERT INTO builds(id, project, kind, input, node, status, created) VALUES ($1, $2, $3, $4, $5, $6, $7)`, bl.ID, bl.Project, bl.Kind, string(in), bl.Node, bl.Status, bl.Created); err != nil {
		return Build{}, 500, err
	}
	b.images.audit.Write(map[string]any{"op": "build_start", "build": bl.ID, "kind": kind, "node": n.Name, "project": project})
	go b.run(bl, n)
	return bl, 201, nil
}

func (b *Builds) run(bl Build, n *fleet.Node) {
	var st int
	var out map[string]any
	var err error
	switch bl.Kind {
	case "oci":
		st, out, err = b.images.nc.JSON(n, "POST", "/layers/from-oci", map[string]any{"image": bl.Input["image"], "name": bl.Input["name"], "build": bl.ID}, 30*time.Minute)
	case "commit":
		sid, _ := bl.Input["sandbox"].(string)
		st, out, err = b.images.nc.JSON(n, "POST", "/sandboxes/"+sid+"/commit", map[string]any{"name": bl.Input["name"], "note": bl.Input["note"]}, 10*time.Minute)
	}
	status, msg := "done", ""
	if err != nil {
		status, msg = "failed", "node unreachable: "+err.Error()
	} else if st != 201 {
		status, msg = "failed", fmt.Sprintf("node %d: %v", st, out["error"])
	}
	res, _ := json.Marshal(out)
	b.db.Exec(`UPDATE builds SET status = $1, result = $2, error = $3, finished = $4 WHERE id = $5`, status, string(res), msg, now(), bl.ID)
	b.images.audit.Write(map[string]any{"op": "build_" + status, "build": bl.ID, "kind": bl.Kind, "node": n.Name, "error": msg})
}

func (b *Builds) scan(rows *sql.Rows) (Build, error) {
	var x Build
	var in, res string
	if err := rows.Scan(&x.ID, &x.Project, &x.Kind, &in, &x.Node, &x.Status, &res, &x.Error, &x.Created, &x.Finished); err != nil {
		return x, err
	}
	_ = json.Unmarshal([]byte(in), &x.Input)
	if res != "" {
		_ = json.Unmarshal([]byte(res), &x.Result)
	}
	return x, nil
}

const cols = `id, project, kind, input, node, status, result, error, created, finished`

func (b *Builds) List(projects map[string]bool) ([]Build, error) {
	rows, err := b.db.Query(`SELECT ` + cols + ` FROM builds ORDER BY created DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Build{}
	for rows.Next() {
		x, err := b.scan(rows)
		if err != nil {
			return nil, err
		}
		if projects == nil || projects[x.Project] {
			out = append(out, x)
		}
	}
	return out, nil
}

func (b *Builds) Get(id string) (Build, error) {
	rows, err := b.db.Query(`SELECT `+cols+` FROM builds WHERE id = $1`, id)
	if err != nil {
		return Build{}, err
	}
	defer rows.Close()
	if !rows.Next() {
		return Build{}, ErrNotFound
	}
	return b.scan(rows)
}

// Log: the build's progress lines as its node recorded them (D64); since = how many lines the caller already has.
func (b *Builds) Log(bl Build, since int) (int, map[string]any) {
	n := b.images.fleet.Get(bl.Node)
	if n == nil || !n.Alive() {
		return 503, map[string]any{"error": "the build's node is not reachable"}
	}
	st, out, err := b.images.nc.JSON(n, "GET", fmt.Sprintf("/builds/log/%s?since=%d", bl.ID, since), nil, 10*time.Second)
	if err != nil {
		return 502, map[string]any{"error": err.Error()}
	}
	return st, out
}

// Templates and Layers: what every alive node holds, with the node named (artefacts are node-local until exported to the store).
func (im *Images) Templates() []map[string]any {
	out := []map[string]any{}
	for _, n := range im.fleet.Alive() {
		st, info, err := im.nc.JSON(n, "GET", "/templates", nil, 10*time.Second)
		if err != nil || st != 200 {
			continue
		}
		if arr, ok := info["snapshots"].([]any); ok {
			for _, x := range arr {
				if m, ok := x.(map[string]any); ok {
					delete(m, "dir")
					m["node"] = n.Name
					out = append(out, m)
				}
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return fmt.Sprint(out[i]["key"]) < fmt.Sprint(out[j]["key"]) })
	return out
}

func (im *Images) Layers() []map[string]any {
	out := []map[string]any{}
	for _, n := range im.fleet.Alive() {
		st, info, err := im.nc.JSON(n, "GET", "/layers", nil, 10*time.Second)
		if err != nil || st != 200 {
			continue
		}
		if arr, ok := info["layers"].([]any); ok {
			for _, x := range arr {
				if m, ok := x.(map[string]any); ok {
					delete(m, "path")
					m["node"] = n.Name
					out = append(out, m)
				}
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return fmt.Sprint(out[i]["id"]) < fmt.Sprint(out[j]["id"]) })
	return out
}

// LayerEverywhere copies one layer to every alive node that lacks it (D37 补): exported as a tar (ext4 image + its description) from a node that has it,
// imported on the others under the same id, so a template or a Build's layers can be placed anywhere.
func (im *Images) LayerEverywhere(lid string) lifecycle.Result {
	alive := im.fleet.Alive()
	if len(alive) == 0 {
		return lifecycle.Result{Code: 429, Body: map[string]any{"error": "no node alive"}}
	}
	has := map[string]bool{}
	for _, n := range alive {
		st, info, err := im.nc.JSON(n, "GET", "/layers", nil, 10*time.Second)
		if err != nil || st != 200 {
			continue
		}
		if arr, ok := info["layers"].([]any); ok {
			for _, x := range arr {
				if m, ok := x.(map[string]any); ok && m["id"] == lid {
					has[n.Name] = true
				}
			}
		}
	}
	var src *fleet.Node
	for _, n := range alive {
		if has[n.Name] {
			src = n
			break
		}
	}
	if src == nil {
		return lifecycle.Result{Code: 404, Body: map[string]any{"error": "no alive node has layer " + lid}}
	}
	t0 := time.Now()
	results := map[string]any{src.Name: "present"}
	var tarBytes []byte
	for _, n := range alive {
		if has[n.Name] {
			if n != src {
				results[n.Name] = "present"
			}
			continue
		}
		if tarBytes == nil {
			resp, err := im.nc.Do(src, "GET", "/layers/"+lid+"/export", nil, 30*time.Minute, nil)
			if err != nil {
				return lifecycle.Result{Code: 502, Body: map[string]any{"error": "export failed: " + err.Error()}}
			}
			tarBytes, _ = io.ReadAll(resp.Body)
			resp.Body.Close()
			if resp.StatusCode != 200 {
				return lifecycle.Result{Code: resp.StatusCode, Body: map[string]any{"error": "export failed", "detail": string(tarBytes[:min(200, len(tarBytes))])}}
			}
		}
		hdr := http.Header{}
		hdr.Set("Content-Type", "application/x-tar")
		resp, err := im.nc.Do(n, "POST", "/layers/import", tarBytes, 30*time.Minute, hdr)
		if err != nil {
			results[n.Name] = "error: " + err.Error()
			continue
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode == 201 {
			results[n.Name] = "imported"
		} else {
			results[n.Name] = fmt.Sprintf("error %d: %s", resp.StatusCode, string(body[:min(200, len(body))]))
		}
	}
	out := map[string]any{"layer": lid, "nodes": results, "bytes": len(tarBytes), "s": lifecycle.Round3(time.Since(t0).Seconds())}
	ev := map[string]any{"op": "layer_everywhere"}
	for k, v := range out {
		ev[k] = v
	}
	im.audit.Write(ev)
	return lifecycle.Result{Code: 200, Body: out}
}

// DeleteTemplate removes a named template snapshot (and its bundle in the store) from every alive node that holds it, so the name is free again.
// Sandboxes already started from it keep running. 404 when no node has it.
func (im *Images) DeleteTemplate(name string) lifecycle.Result {
	removed := []string{}
	for _, n := range im.fleet.Alive() {
		st, _, err := im.nc.JSON(n, "DELETE", "/templates/"+name, map[string]any{"drop_bundle": true}, 60*time.Second)
		if err == nil && st == 200 {
			removed = append(removed, n.Name)
		}
	}
	if len(removed) == 0 {
		return lifecycle.Result{Code: 404, Body: map[string]any{"error": "no template " + name}}
	}
	im.audit.Write(map[string]any{"op": "template_delete", "name": name, "nodes": removed})
	return lifecycle.Result{Code: 200, Body: map[string]any{"deleted": name, "nodes": removed}}
}

// CommittedBy reports whether a finished commit build in one of the projects (nil = any) made the template, i.e. whether the caller may delete it.
func (b *Builds) CommittedBy(projects map[string]bool, name string) bool {
	bs, err := b.List(projects)
	if err != nil {
		return false
	}
	for _, x := range bs {
		if x.Kind == "commit" && x.Status == "done" && x.Input["name"] == name {
			return true
		}
	}
	return false
}
