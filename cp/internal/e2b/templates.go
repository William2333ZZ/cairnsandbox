package e2b

// E2B's template builder (Template().from_...().run_cmd(...) and Template.build()): the SDK asks for a template, uploads the files its COPY steps need, triggers
// the build with the step list, and polls the status. Here the build is run by the control plane itself in a throw-away build sandbox: boot a base, run the
// steps through the node's exec and files endpoints, start the start command and wait for the ready command, then commit the sandbox as a template (memory and
// disk, D37), so a sandbox created from it starts with whatever the steps left running. What is and is not supported is in docs/guide/api/e2b.md.

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"fcnode/cp/internal/images"
	"fcnode/cp/internal/tenancy"
)

var reHash = regexp.MustCompile(`^[0-9a-f]{64}$`)
var tplSlots = make(chan struct{}, 4) // at most this many template builds run at once per replica

const maxUpload = 512 << 20

func (s *Server) uploadDir() string {
	d := s.UploadDir
	if d == "" {
		d = filepath.Join(os.TempDir(), "cairn-e2b-uploads")
	}
	os.MkdirAll(d, 0o700)
	return d
}

func (s *Server) uploadSig(hash string, exp int64) string {
	m := hmac.New(sha256.New, s.Secret)
	m.Write([]byte("upload|" + hash + "|" + strconv.FormatInt(exp, 10)))
	return hex.EncodeToString(m.Sum(nil))
}

// publicBase is how the SDK can reach this port (the upload URL we hand out is absolute).
func (s *Server) publicBase(r *http.Request) string {
	if s.PublicURL != "" {
		return strings.TrimRight(s.PublicURL, "/")
	}
	scheme := "http"
	if r.TLS != nil || r.Header.Get("X-Forwarded-Proto") == "https" {
		scheme = "https"
	}
	return scheme + "://" + r.Host
}

// serveUpload: PUT /e2b-uploads/<sha256>?exp=&sig= -- the SDK uploads the tar of a COPY step here, without credentials (like a presigned URL), so the link carries
// an HMAC of the hash and an expiry.
func (s *Server) serveUpload(w http.ResponseWriter, r *http.Request) {
	hash := strings.TrimPrefix(r.URL.Path, "/e2b-uploads/")
	exp, _ := strconv.ParseInt(r.URL.Query().Get("exp"), 10, 64)
	if r.Method != "PUT" || !reHash.MatchString(hash) || len(s.Secret) == 0 || time.Now().Unix() > exp ||
		!hmac.Equal([]byte(r.URL.Query().Get("sig")), []byte(s.uploadSig(hash, exp))) {
		e2bErr(w, 403, "invalid or expired upload link")
		return
	}
	tmp, err := os.CreateTemp(s.uploadDir(), "up-*")
	if err != nil {
		e2bErr(w, 500, "cannot store the upload")
		return
	}
	n, err := io.Copy(tmp, io.LimitReader(r.Body, maxUpload+1))
	tmp.Close()
	if err != nil || n > maxUpload {
		os.Remove(tmp.Name())
		e2bErr(w, 413, "upload too large or interrupted")
		return
	}
	if err := os.Rename(tmp.Name(), filepath.Join(s.uploadDir(), hash)); err != nil {
		e2bErr(w, 500, "cannot store the upload")
		return
	}
	w.WriteHeader(200)
}

// templateRoutes serves the builder's endpoints; false = not one of them.
func (s *Server) templateRoutes(w http.ResponseWriter, r *http.Request, path string, id tenancy.Identity, project string, scope map[string]bool, body map[string]any) bool {
	switch {
	case path == "/v3/templates" && r.Method == "POST":
		s.tplRequest(w, id, project, scope, body)
	case strings.HasPrefix(path, "/templates/aliases/") && r.Method == "GET":
		name := strings.TrimPrefix(path, "/templates/aliases/")
		if images.NameOK(name) && s.Images.TemplateExists(name) {
			send(w, 200, map[string]any{"templateID": name, "public": false})
		} else {
			e2bErr(w, 404, "template not found")
		}
	case regexp.MustCompile(`^/templates/[^/]+/files/[0-9a-f]{64}$`).MatchString(path) && r.Method == "GET":
		hash := path[strings.LastIndex(path, "/")+1:]
		if _, err := os.Stat(filepath.Join(s.uploadDir(), hash)); err == nil {
			send(w, 201, map[string]any{"present": true})
			return true
		}
		exp := time.Now().Add(time.Hour).Unix()
		send(w, 201, map[string]any{"present": false, "url": fmt.Sprintf("%s/e2b-uploads/%s?exp=%d&sig=%s", s.publicBase(r), hash, exp, s.uploadSig(hash, exp))})
	case regexp.MustCompile(`^/v2/templates/[^/]+/builds/[^/]+$`).MatchString(path) && r.Method == "POST":
		parts := strings.Split(path, "/")
		s.tplStart(w, parts[3], parts[5], project, scope, body)
	case regexp.MustCompile(`^/templates/[^/]+/builds/[^/]+/status$`).MatchString(path) && r.Method == "GET":
		parts := strings.Split(path, "/")
		s.tplStatus(w, r, parts[2], parts[4], scope)
	default:
		return false
	}
	return true
}

// tplRequest: POST /v3/templates {name, tags?, cpuCount?, memoryMB?} -> the template and the build that will make it.
func (s *Server) tplRequest(w http.ResponseWriter, id tenancy.Identity, project string, scope map[string]bool, body map[string]any) {
	name, _ := body["name"].(string)
	if i := strings.Index(name, ":"); i >= 0 {
		if tag := name[i+1:]; tag != "" && tag != "latest" {
			e2bErr(w, 400, "template tags are not supported: use a plain name")
			return
		}
		name = name[:i]
	}
	if tags, _ := body["tags"].([]any); len(tags) > 0 {
		e2bErr(w, 400, "template tags are not supported")
		return
	}
	if !images.NameOK(name) {
		e2bErr(w, 400, "template name: a lowercase letter, then lowercase letters, digits, _ . - (at most 40 characters)")
		return
	}
	if strings.HasPrefix(name, "snap-") {
		e2bErr(w, 400, "the snap- prefix is reserved for snapshots")
		return
	}
	if s.Images.TemplateExists(name) && !id.Admin() && !s.Builds.CommittedBy(scope, name) {
		e2bErr(w, 409, "a template with this name already exists and is not yours")
		return
	}
	in := map[string]any{"name": name}
	for _, k := range []string{"cpuCount", "memoryMB"} {
		if v, ok := body[k].(float64); ok && v > 0 {
			in[k] = v
		}
	}
	bl, err := s.Builds.NewE2B(project, in)
	if err != nil {
		e2bErr(w, 500, "cannot record the build")
		return
	}
	send(w, 202, map[string]any{"templateID": name, "buildID": bl.ID, "public": false, "aliases": []string{name}, "names": []string{name}, "tags": []string{}})
}

func (s *Server) ownBuild(buildID string, scope map[string]bool, templateID string) (images.Build, bool) {
	bl, err := s.Builds.Get(buildID)
	if err != nil || bl.Kind != "e2b" || bl.Input["name"] != templateID || (scope != nil && !scope[bl.Project]) {
		return images.Build{}, false
	}
	return bl, true
}

// tplStart: POST /v2/templates/<id>/builds/<buildID> {fromImage|fromTemplate, steps, startCmd, readyCmd, force}: store the plan and run it in the background.
func (s *Server) tplStart(w http.ResponseWriter, templateID, buildID, project string, scope map[string]bool, body map[string]any) {
	bl, ok := s.ownBuild(buildID, scope, templateID)
	if !ok {
		e2bErr(w, 404, "build not found")
		return
	}
	if bl.Status != "waiting" {
		e2bErr(w, 409, "this build was already started")
		return
	}
	in := bl.Input
	for _, k := range []string{"fromImage", "fromTemplate", "steps", "startCmd", "readyCmd"} {
		if v, ok := body[k]; ok {
			in[k] = v
		}
	}
	if in["fromImage"] == nil && in["fromTemplate"] == nil {
		e2bErr(w, 400, "fromImage or fromTemplate is required")
		return
	}
	res := map[string]any{"logs": []any{}}
	if err := s.Builds.SetE2B(buildID, in, "", "running", res, ""); err != nil {
		e2bErr(w, 500, "cannot record the build")
		return
	}
	go s.runTemplateBuild(buildID, bl.Project, in)
	w.WriteHeader(202)
}

// tplStatus: GET /templates/<id>/builds/<buildID>/status?logsOffset=&limit= -> {status: waiting|building|ready|error, logs, logEntries, reason?}
func (s *Server) tplStatus(w http.ResponseWriter, r *http.Request, templateID, buildID string, scope map[string]bool) {
	bl, ok := s.ownBuild(buildID, scope, templateID)
	if !ok {
		e2bErr(w, 404, "build not found")
		return
	}
	status := map[string]string{"waiting": "waiting", "running": "building", "done": "ready", "failed": "error"}[bl.Status]
	if bl.Status == "running" && time.Now().Unix()-int64(bl.Created) > 4*3600 { // the replica that ran it is gone
		status = "error"
	}
	entries, _ := bl.Result["logs"].([]any)
	off, _ := strconv.Atoi(r.URL.Query().Get("logsOffset"))
	lim, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	if lim <= 0 || lim > 100 {
		lim = 100
	}
	if off < 0 || off > len(entries) {
		off = len(entries)
	}
	end := off + lim
	if end > len(entries) {
		end = len(entries)
	}
	msgs := []string{}
	for _, e := range entries[off:end] {
		m, _ := e.(map[string]any)
		msgs = append(msgs, fmt.Sprint(m["message"]))
	}
	out := map[string]any{"buildID": buildID, "templateID": templateID, "status": status, "logs": msgs, "logEntries": entries[off:end]}
	if status == "error" {
		step, _ := bl.Result["step"].(float64)
		reason := map[string]any{"message": firstNonEmpty(bl.Error, "build failed")}
		if step > 0 {
			reason["step"] = fmt.Sprint(int(step) - 1) // the SDK counts steps from 0
		}
		out["reason"] = reason
	}
	send(w, 200, out)
}

func firstNonEmpty(a ...string) string {
	for _, x := range a {
		if x != "" {
			return x
		}
	}
	return ""
}

// ---------------------------------------------------------------- the runner

type tplRun struct {
	s       *Server
	id      string
	project string
	in      map[string]any
	mu      sync.Mutex
	logs    []any
	step    int
}

func (t *tplRun) log(level, msg string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.logs = append(t.logs, map[string]any{"timestamp": time.Now().UTC().Format(time.RFC3339), "level": level, "message": msg})
	t.save("running", "")
}

func (t *tplRun) save(status, errMsg string) {
	res := map[string]any{"logs": t.logs, "step": t.step}
	if status == "done" {
		res["template"] = t.in["name"]
	}
	t.s.Builds.SetE2B(t.id, t.in, "", status, res, errMsg)
}

func shq(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }

func (s *Server) runTemplateBuild(buildID, project string, in map[string]any) {
	tplSlots <- struct{}{}
	defer func() { <-tplSlots }()
	t := &tplRun{s: s, id: buildID, project: project, in: in}
	name, _ := in["name"].(string)
	var sid string
	fail := func(msg string) {
		t.mu.Lock()
		t.logs = append(t.logs, map[string]any{"timestamp": time.Now().UTC().Format(time.RFC3339), "level": "error", "message": msg})
		t.save("failed", msg)
		t.mu.Unlock()
	}
	defer func() {
		if sid != "" {
			s.Life.NodeCall(sid, "DELETE", "", nil)
		}
	}()
	defer func() {
		if r := recover(); r != nil {
			fail(fmt.Sprint("internal error in the build: ", r))
		}
	}()

	// the starting point
	spec := map[string]any{"class": "persistent", "vcpu": 2, "mem": 1024, "disk": 2048, "ttl_idle_s": 3600}
	if v, ok := in["cpuCount"].(float64); ok {
		spec["vcpu"] = int(v)
	}
	if v, ok := in["memoryMB"].(float64); ok {
		spec["mem"] = int(v)
	}
	if ft, _ := in["fromTemplate"].(string); ft != "" {
		if !s.Images.TemplateExists(ft) {
			fail("template " + ft + " does not exist")
			return
		}
		spec["template_key"], spec["boot"] = ft, "snapshot"
		t.log("info", "starting from template "+ft)
	} else {
		img, _ := in["fromImage"].(string)
		base := baseFor(img)
		if base == "" {
			fail("image " + img + " is not available: CairnSandbox builds from its own bases (e2bdev/base, ubuntu:*, debian:*) or from a template (from_template); pulling other images needs a layer built with POST /builds {kind:\"oci\"}")
			return
		}
		if base != "default" {
			spec["template"] = base
		}
		t.log("info", fmt.Sprintf("image %s -> CairnSandbox base %s (the image is not pulled)", img, base))
	}
	res := s.Life.Create(spec, project)
	if res.Code != 201 {
		fail(fmt.Sprintf("cannot start the build sandbox (%d): %v", res.Code, errText(res.Body)))
		return
	}
	info, _ := res.Body.(map[string]any)
	sid, _ = info["id"].(string)
	t.log("info", "build sandbox "+sid)

	env := map[string]string{}
	user, cwd := "root", ""
	exec := func(cmd, u string, timeout int) (int, string, string, error) {
		body := map[string]any{"cmd": cmd, "user": u, "timeout": timeout}
		if cwd != "" {
			body["cwd"] = cwd
		}
		if len(env) > 0 {
			body["env"] = env
		}
		st, out := s.Life.NodeCall(sid, "POST", "/exec", body)
		if st != 200 {
			return -1, "", "", fmt.Errorf("exec: %d %v", st, errText(out))
		}
		code := -1
		if f, ok := out["exit"].(float64); ok {
			code = int(f)
		}
		so, _ := out["stdout"].(string)
		se, _ := out["stderr"].(string)
		return code, so, se, nil
	}
	dump := func(so, se string) {
		for _, blk := range []struct{ lvl, text string }{{"info", so}, {"warn", se}} {
			lines := strings.Split(strings.TrimRight(blk.text, "\n"), "\n")
			if len(lines) > 200 {
				lines = append(lines[:100], append([]string{fmt.Sprintf("... %d lines omitted ...", len(lines)-200)}, lines[len(lines)-100:]...)...)
			}
			for _, l := range lines {
				if l != "" {
					t.log(blk.lvl, l)
				}
			}
		}
	}

	steps, _ := in["steps"].([]any)
	for i, x := range steps {
		st, _ := x.(map[string]any)
		typ, _ := st["type"].(string)
		var args []string
		if a, ok := st["args"].([]any); ok {
			for _, v := range a {
				args = append(args, fmt.Sprint(v))
			}
		}
		t.mu.Lock()
		t.step = i + 1
		t.mu.Unlock()
		t.log("info", fmt.Sprintf("step %d/%d: %s %s", i+1, len(steps), typ, clip(strings.Join(args, " "), 200)))
		switch typ {
		case "ENV":
			for j := 0; j+1 < len(args); j += 2 {
				env[args[j]] = args[j+1]
			}
		case "WORKDIR":
			if len(args) < 1 {
				fail("WORKDIR needs a path")
				return
			}
			cwd = args[0]
			if code, so, se, err := exec("mkdir -p "+shq(cwd), "root", 60); err != nil || code != 0 {
				dump(so, se)
				fail(fmt.Sprintf("step %d: cannot create %s", i+1, cwd))
				return
			}
		case "USER":
			if len(args) < 1 {
				fail("USER needs a name")
				return
			}
			user = args[0]
		case "RUN":
			if len(args) < 1 {
				fail("RUN needs a command")
				return
			}
			u := user
			if len(args) > 1 && args[1] != "" {
				u = args[1]
			}
			code, so, se, err := exec(args[0], u, 280)
			dump(so, se)
			if err != nil {
				fail(fmt.Sprintf("step %d: %v", i+1, err))
				return
			}
			if code != 0 {
				fail(fmt.Sprintf("step %d: command exited with status %d", i+1, code))
				return
			}
		case "COPY":
			hash, _ := st["filesHash"].(string)
			if len(args) < 2 || !reHash.MatchString(hash) {
				fail(fmt.Sprintf("step %d: COPY needs a source, a destination and uploaded files", i+1))
				return
			}
			if msg := s.copyStep(sid, hash, args, user, cwd); msg != "" {
				fail(fmt.Sprintf("step %d: %s", i+1, msg))
				return
			}
		default:
			fail(fmt.Sprintf("step %d: the step type %q is not supported", i+1, typ))
			return
		}
	}

	// the environment the steps set stays for later logins (envd runs `bash -l`), written as exports
	if len(env) > 0 {
		keys := make([]string, 0, len(env))
		for k := range env {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var sb strings.Builder
		for _, k := range keys {
			if regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`).MatchString(k) {
				fmt.Fprintf(&sb, "export %s=%s\n", k, shq(env[k]))
			}
		}
		exec("printf %s "+shq(sb.String())+" > /etc/profile.d/99-e2b-template-env.sh", "root", 60)
	}

	// start command and ready command
	if sc, _ := in["startCmd"].(string); sc != "" {
		t.log("info", "start command: "+clip(sc, 200))
		cmd := fmt.Sprintf("setsid nohup bash -lc %s > /tmp/e2b-start.log 2>&1 < /dev/null &", shq(sc))
		if code, so, se, err := exec(cmd, user, 60); err != nil || code != 0 {
			dump(so, se)
			fail("could not start the start command")
			return
		}
		ready, _ := in["readyCmd"].(string)
		if ready == "" {
			ready = "sleep 2"
		}
		deadline := time.Now().Add(5 * time.Minute)
		for {
			code, _, _, err := exec(ready, user, 60)
			if err == nil && code == 0 {
				t.log("info", "ready command succeeded")
				break
			}
			if time.Now().After(deadline) {
				_, so, _, _ := exec("tail -n 40 /tmp/e2b-start.log", "root", 30)
				dump(so, "")
				fail("the ready command did not succeed within 5 minutes")
				return
			}
			time.Sleep(time.Second)
		}
	}

	// commit as the template, replacing an earlier build of the same name
	if s.Images.TemplateExists(name) {
		if r := s.Images.DeleteTemplate(name); r.Code != 200 {
			fail(fmt.Sprintf("cannot replace the existing template %s: %v", name, errText(r.Body)))
			return
		}
		t.log("info", "replaced the earlier template "+name)
	}
	t.log("info", "committing the sandbox as template "+name)
	if st, out := s.Life.NodeCall(sid, "POST", "/commit", map[string]any{"name": name, "note": "E2B template build " + t.id}); st != 201 {
		fail(fmt.Sprintf("commit failed (%d): %v", st, errText(out)))
		return
	}
	s.Fleet.PollOnce(s.NC) // placement learns which node holds the template from the node's health: refresh it now, not at the next poll, so a create right after the build finds it
	t.log("info", "template "+name+" is ready")
	t.mu.Lock()
	t.save("done", "")
	t.mu.Unlock()
}

// copyStep: a COPY step: the SDK uploaded a gzip tar of the build context; unpack it in the sandbox and copy the source to the destination like Docker's COPY.
func (s *Server) copyStep(sid, hash string, args []string, user, cwd string) string {
	data, err := os.ReadFile(filepath.Join(s.uploadDir(), hash))
	if err != nil {
		return "the uploaded files are gone; run the build again"
	}
	node := s.Fleet.Get(s.Life.NodeOf(sid))
	if node == nil {
		return "node unknown"
	}
	arch := "/tmp/e2b-ctx-" + hash + ".tgz"
	resp, err := s.NC.Do(node, "PUT", "/sandboxes/"+sid+"/files?path="+arch, data, 10*time.Minute, nil)
	if err != nil {
		return "uploading the files into the sandbox failed: " + err.Error()
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Sprintf("uploading the files into the sandbox failed (%d)", resp.StatusCode)
	}
	src, dst := args[0], args[1]
	owner, mode := "", ""
	if len(args) > 2 {
		owner = args[2]
	}
	if len(args) > 3 {
		mode = args[3]
	}
	if cwd != "" && !strings.HasPrefix(dst, "/") {
		dst = strings.TrimRight(cwd, "/") + "/" + dst
	}
	ctx := "/tmp/e2b-ctx-" + hash
	script := fmt.Sprintf("set -e; rm -rf %[1]s; mkdir -p %[1]s; tar -xzf %[2]s -C %[1]s; rm -f %[2]s; S=%[1]s/%[3]s; ", ctx, arch, strings.TrimLeft(src, "./"))
	script += fmt.Sprintf("if [ -d \"$S\" ]; then mkdir -p %[1]s; cp -a \"$S\"/. %[1]s/; else mkdir -p \"$(dirname %[1]s)\"; cp -a \"$S\" %[1]s; fi; rm -rf %[2]s", shq(dst), ctx)
	if owner != "" {
		script += "; chown -R " + shq(owner) + " " + shq(dst)
	}
	if mode != "" {
		script += "; chmod -R " + shq(mode) + " " + shq(dst)
	}
	st, out := s.Life.NodeCall(sid, "POST", "/exec", map[string]any{"cmd": script, "user": "root", "timeout": 280})
	if f, _ := out["exit"].(float64); st != 200 || f != 0 {
		return "copying failed: " + clip(fmt.Sprint(out["stderr"], out["error"]), 300)
	}
	return ""
}

// baseFor maps an image name to one of our bases: "default" is the node's default template, "" = not one of ours.
func baseFor(img string) string {
	img = strings.ToLower(strings.TrimSpace(img))
	switch {
	case img == "" || img == "base" || strings.HasPrefix(img, "e2bdev/base"):
		return "default"
	case img == "ubuntu" || strings.HasPrefix(img, "ubuntu:"):
		return "ubuntu-24.04"
	case img == "debian" || strings.HasPrefix(img, "debian:"):
		return "debian-13"
	}
	return ""
}

func errText(v any) string {
	if m, ok := v.(map[string]any); ok {
		if e, ok := m["error"].(string); ok {
			return e
		}
	}
	b, _ := json.Marshal(v)
	return clip(string(b), 300)
}
