package state

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"time"

	"fcnode/cp/internal/db"
	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/nodeclient"
	"fcnode/cp/internal/registry"
)

// Volume (docs/002 沙箱侧 5, D35): an ext4 image with a life of its own, owned by a project, living on one node; a sandbox that attaches it is placed there.
type Volume struct {
	ID      string  `json:"id"`
	Project string  `json:"project"`
	Node    string  `json:"node"`
	Name    string  `json:"name"`
	SizeMiB int64   `json:"size_mib"`
	Created float64 `json:"created"`
}

var ErrNotFound = errors.New("not found")
var nameRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]{0,39}$`)

type Auditor interface{ Write(map[string]any) }

type Volumes struct {
	db    *db.DB
	fleet *fleet.Fleet
	nc    nodeclient.Client
	audit Auditor
}

func NewVolumes(d *db.DB, f *fleet.Fleet, audit Auditor) *Volumes {
	return &Volumes{db: d, fleet: f, audit: audit}
}

func (v *Volumes) List(projects map[string]bool) ([]Volume, error) {
	rows, err := v.db.Query(`SELECT id, project, node, name, size_mib, created FROM volumes ORDER BY created`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Volume{}
	for rows.Next() {
		var x Volume
		if err := rows.Scan(&x.ID, &x.Project, &x.Node, &x.Name, &x.SizeMiB, &x.Created); err != nil {
			return nil, err
		}
		if projects == nil || projects[x.Project] {
			out = append(out, x)
		}
	}
	return out, nil
}

func (v *Volumes) Get(id string) (Volume, error) {
	var x Volume
	err := v.db.QueryRow(`SELECT id, project, node, name, size_mib, created FROM volumes WHERE id = $1`, id).Scan(&x.ID, &x.Project, &x.Node, &x.Name, &x.SizeMiB, &x.Created)
	if err == sql.ErrNoRows {
		return x, ErrNotFound
	}
	return x, err
}

// Create makes the image on a node (the one named, else the least loaded alive one) and records it.
func (v *Volumes) Create(project, name string, sizeMiB int64, nodeName string) (Volume, int, error) {
	if !nameRe.MatchString(name) {
		return Volume{}, 400, errors.New("volume name: lowercase letters, digits, _ . -, up to 40 chars")
	}
	if sizeMiB == 0 {
		sizeMiB = 1024
	}
	if sizeMiB < 64 || sizeMiB > 65536 {
		return Volume{}, 400, errors.New("size_mib must be 64..65536")
	}
	var n *fleet.Node
	if nodeName != "" {
		n = v.fleet.Get(nodeName)
	} else {
		n = v.fleet.LeastLoaded(map[string]any{"mem": 0.0}, nil)
	}
	if n == nil || !n.Alive() {
		return Volume{}, 429, errors.New("no node for the volume")
	}
	id := "vol-" + registry.NewToken()[:8]
	st, info, err := v.nc.JSON(n, "POST", "/volumes", map[string]any{"id": id, "name": name, "size_mib": sizeMiB, "project": project}, 120*time.Second)
	if err != nil {
		return Volume{}, 502, fmt.Errorf("node unreachable: %w", err)
	}
	if st != 201 {
		return Volume{}, st, fmt.Errorf("node: %v", info["error"])
	}
	x := Volume{ID: id, Project: project, Node: n.Name, Name: name, SizeMiB: sizeMiB, Created: float64(time.Now().UnixNano()) / 1e9}
	if _, err := v.db.Exec(`INSERT INTO volumes(id, project, node, name, size_mib, created) VALUES ($1, $2, $3, $4, $5, $6)`, x.ID, x.Project, x.Node, x.Name, x.SizeMiB, x.Created); err != nil {
		return Volume{}, 500, err
	}
	v.audit.Write(map[string]any{"op": "volume_create", "volume": id, "project": project, "node": n.Name, "size_mib": sizeMiB})
	return x, 201, nil
}

// CreateFrom makes a volume whose filesystem holds the uploaded tar's tree (a harness directory, a cache): the bytes go straight to the node.
func (v *Volumes) CreateFrom(project, name string, sizeMiB int64, nodeName string, data []byte) (Volume, int, error) {
	if !nameRe.MatchString(name) {
		return Volume{}, 400, errors.New("volume name: lowercase letters, digits, _ . -, up to 40 chars")
	}
	if len(data) == 0 {
		return Volume{}, 400, errors.New("upload the tar as the body")
	}
	if sizeMiB == 0 {
		sizeMiB = 64
	}
	var n *fleet.Node
	if nodeName != "" {
		n = v.fleet.Get(nodeName)
	} else {
		n = v.fleet.LeastLoaded(map[string]any{"mem": 0.0}, nil)
	}
	if n == nil || !n.Alive() {
		return Volume{}, 429, errors.New("no node for the volume")
	}
	id := "vol-" + registry.NewToken()[:8]
	q := url.Values{"id": {id}, "name": {name}, "project": {project}, "size_mib": {fmt.Sprint(sizeMiB)}}
	resp, err := v.nc.Do(n, "POST", "/volumes/from-archive?"+q.Encode(), data, 10*time.Minute, http.Header{"Content-Type": {"application/x-tar"}})
	if err != nil {
		return Volume{}, 502, fmt.Errorf("node unreachable: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 201 {
		var e map[string]any
		_ = json.Unmarshal(body, &e)
		return Volume{}, resp.StatusCode, fmt.Errorf("node: %v", e["error"])
	}
	x := Volume{ID: id, Project: project, Node: n.Name, Name: name, SizeMiB: sizeMiB, Created: float64(time.Now().UnixNano()) / 1e9}
	if _, err := v.db.Exec(`INSERT INTO volumes(id, project, node, name, size_mib, created) VALUES ($1, $2, $3, $4, $5, $6)`, x.ID, x.Project, x.Node, x.Name, x.SizeMiB, x.Created); err != nil {
		return Volume{}, 500, err
	}
	v.audit.Write(map[string]any{"op": "volume_create", "volume": id, "project": project, "node": n.Name, "size_mib": sizeMiB, "content_bytes": len(data)})
	return x, 201, nil
}

// Delete removes the image on its node (a node refuses while it is attached: 409 passes through) and the record.
func (v *Volumes) Delete(id string) (int, map[string]any) {
	x, err := v.Get(id)
	if err != nil {
		return 404, map[string]any{"error": "no volume " + id}
	}
	n := v.fleet.Get(x.Node)
	if n != nil && n.Alive() {
		st, info, err := v.nc.JSON(n, "DELETE", "/volumes/"+id, nil, 60*time.Second)
		if err != nil {
			return 502, map[string]any{"error": "node unreachable: " + err.Error()}
		}
		if st != 200 && st != 404 {
			return st, info
		}
	} else {
		return 503, map[string]any{"error": "node " + x.Node + " is down; the image cannot be removed now"}
	}
	v.db.Exec(`DELETE FROM volumes WHERE id = $1`, id)
	v.audit.Write(map[string]any{"op": "volume_delete", "volume": id, "node": x.Node})
	return 200, map[string]any{"deleted": id}
}

// Info adds what the node knows (attachments, allocation) to the record.
func (v *Volumes) Info(x Volume) map[string]any {
	out := map[string]any{"id": x.ID, "project": x.Project, "node": x.Node, "name": x.Name, "size_mib": x.SizeMiB, "created": x.Created}
	if n := v.fleet.Get(x.Node); n != nil && n.Alive() {
		if st, info, err := v.nc.JSON(n, "GET", "/volumes/"+x.ID, nil, 10*time.Second); err == nil && st == 200 {
			out["attached"], out["allocated_mib"], out["content_bytes"] = info["attached"], info["allocated_mib"], info["content_bytes"]
		}
	}
	return out
}

// VolumeMiB: the size of a project's volumes, for the storage quota (satisfies lifecycle.VolumeStorage).
func (v *Volumes) VolumeMiB(project string) float64 {
	var s sql.NullFloat64
	v.db.QueryRow(`SELECT sum(size_mib) FROM volumes WHERE project = $1`, project).Scan(&s)
	return s.Float64
}

// NodeFor: the one node every listed volume lives on (a sandbox attaching them must be placed there), and their projects.
func (v *Volumes) NodeFor(ids []string) (node string, projects map[string]bool, err error) {
	projects = map[string]bool{}
	for _, id := range ids {
		x, e := v.Get(id)
		if e != nil {
			return "", nil, fmt.Errorf("no volume %s", id)
		}
		if node != "" && node != x.Node {
			return "", nil, fmt.Errorf("volumes %s are on different nodes", ids)
		}
		node = x.Node
		projects[x.Project] = true
	}
	return node, projects, nil
}

// Move carries a volume from one node to another (export as a sparse tar, import under the same id, delete at the source) and re-homes its record.
// Used by a migration whose sandbox attaches it; the volume must be attached to nothing but that sandbox (or nothing), else 409.
func (v *Volumes) Move(id string, from, to *fleet.Node, onlyFor string) (int, map[string]any) {
	x, err := v.Get(id)
	if err != nil {
		return 404, map[string]any{"error": "no volume " + id}
	}
	if x.Node != from.Name {
		return 409, map[string]any{"error": fmt.Sprintf("volume %s lives on %s, not %s", id, x.Node, from.Name)}
	}
	st, info, err := v.nc.JSON(from, "GET", "/volumes/"+id, nil, 10*time.Second)
	if err != nil || st != 200 {
		return 502, map[string]any{"error": "volume lookup failed", "detail": info}
	}
	if att, _ := info["attached"].(map[string]any); len(att) > 0 {
		for sid := range att {
			if sid != onlyFor {
				return 409, map[string]any{"error": fmt.Sprintf("volume %s is also attached to %s; it cannot move with %s", id, sid, onlyFor)}
			}
		}
	}
	resp, err := v.nc.Do(from, "GET", "/volumes/"+id+"/export", nil, 30*time.Minute, nil)
	if err != nil {
		return 502, map[string]any{"error": "export failed: " + err.Error()}
	}
	data, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		return resp.StatusCode, map[string]any{"error": "export failed", "detail": string(data[:min(200, len(data))])}
	}
	hdr := http.Header{}
	hdr.Set("Content-Type", "application/x-tar")
	resp, err = v.nc.Do(to, "POST", "/volumes/import", data, 30*time.Minute, hdr)
	if err != nil {
		return 502, map[string]any{"error": "import failed: " + err.Error()}
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 201 {
		return resp.StatusCode, map[string]any{"error": "import failed", "detail": string(body[:min(200, len(body))])}
	}
	v.db.Exec(`UPDATE volumes SET node = $1 WHERE id = $2`, to.Name, id)
	v.audit.Write(map[string]any{"op": "volume_move", "volume": id, "from": from.Name, "to": to.Name, "bytes": len(data), "with": onlyFor})
	return 200, map[string]any{"volume": id, "from": from.Name, "to": to.Name, "bytes": len(data)}
}

// DropAt removes a volume's image from one node only (after a move, once the sandbox that held it is gone there); the record is untouched.
func (v *Volumes) DropAt(id string, n *fleet.Node) {
	v.nc.JSON(n, "DELETE", "/volumes/"+id, nil, 60*time.Second)
}

// SetNode re-homes a volume's record (the undo of a half-done move).
func (v *Volumes) SetNode(id, node string) {
	v.db.Exec(`UPDATE volumes SET node = $1 WHERE id = $2`, node, id)
}
