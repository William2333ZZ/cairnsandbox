// fc-cp: the control plane over the node agents (docs/003 控制面), wired from the contexts in internal/ (docs/002 沙箱侧的领域).
// Env: CP_PORT (8700), CP_STATE (/var/lib/fc-cp), NODES (JSON list of {"name","url","token"|"token_file"}), STORE, EVAL_URL, INTAKE_URL,
// WEB_DIR (/web), CP_TOKEN, CP_REGISTRY (empty = files in CP_STATE, redis://host:port[/db] = shared by every replica), CP_REPLICA,
// CP_DB (empty = SQLite in CP_STATE, postgres://... = shared by every replica), MODEL_PROXY_URL + PROXY_ADMIN_TOKEN (model usage for metering).
package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"fcnode/cp/internal/abuse"
	"fcnode/cp/internal/audit"
	"fcnode/cp/internal/db"
	"fcnode/cp/internal/e2b"
	"fcnode/cp/internal/exec"
	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/httpapi"
	"fcnode/cp/internal/images"
	"fcnode/cp/internal/lifecycle"
	"fcnode/cp/internal/metering"
	"fcnode/cp/internal/network"
	"fcnode/cp/internal/nodeclient"
	"fcnode/cp/internal/registry"
	"fcnode/cp/internal/state"
	"fcnode/cp/internal/tenancy"
	"fcnode/cp/internal/webhook"
)

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func envFloat(k string, d float64) float64 {
	if v := os.Getenv(k); v != "" {
		var n float64
		if _, err := fmt.Sscan(v, &n); err == nil {
			return n
		}
	}
	return d
}

func envInt(k string, d int64) int64 {
	if v := os.Getenv(k); v != "" {
		var n int64
		if _, err := fmt.Sscan(v, &n); err == nil {
			return n
		}
	}
	return d
}

// secret reads K from the environment, or from the file named by K_FILE (compose / Kubernetes secrets): the value then never sits in `docker inspect`.
func secret(k string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	if f := os.Getenv(k + "_FILE"); f != "" {
		if b, err := os.ReadFile(f); err == nil {
			return strings.TrimSpace(string(b))
		}
	}
	return ""
}

// dbURL is CP_DB with the password from CP_DB_PASSWORD_FILE filled in (postgres://user:@host/... -> postgres://user:pw@host/...).
func dbURL() string {
	u := os.Getenv("CP_DB")
	pw := secret("CP_DB_PASSWORD")
	if pw == "" || u == "" {
		return u
	}
	if i := strings.Index(u, "://"); i >= 0 {
		if at := strings.Index(u[i+3:], "@"); at >= 0 {
			cred := u[i+3 : i+3+at]
			if c := strings.Index(cred, ":"); c >= 0 && cred[c+1:] == "" {
				return u[:i+3] + cred[:c+1] + pw + u[i+3+at:]
			}
		}
	}
	return u
}

func webDir(base string) string {
	for _, sub := range []string{"dist", "legacy"} {
		if _, err := os.Stat(filepath.Join(base, sub, "index.html")); err == nil {
			return filepath.Join(base, sub)
		}
	}
	return base
}

func main() {
	stateDir := env("CP_STATE", "/var/lib/fc-cp")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	reg, err := registry.Open(os.Getenv("CP_REGISTRY"), stateDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := reg.Ping(); err != nil { // a replica without its registry would route from nothing
		fmt.Fprintln(os.Stderr, "registry unreachable:", err)
		os.Exit(1)
	}
	replica := os.Getenv("CP_REPLICA")
	if replica == "" {
		replica = "cp-" + registry.NewToken()[:6]
	}
	log, err := audit.Open(stateDir, replica)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	sqldb, err := db.Open(dbURL(), stateDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := sqldb.Migrate(); err != nil {
		fmt.Fprintln(os.Stderr, "database:", err)
		os.Exit(1)
	}
	tn := tenancy.New(sqldb, secret("CP_TOKEN"))
	if err := tn.Bootstrap(reg); err != nil {
		fmt.Fprintln(os.Stderr, "tenancy:", err)
		os.Exit(1)
	}
	st := state.Store{URL: strings.TrimRight(os.Getenv("STORE"), "/")}
	fl := fleet.New(reg, log)
	life := lifecycle.New(reg, fl, st, log)
	life.WatchFleet()
	var watcher *abuse.Watcher
	if cfg := (abuse.Config{CPUPct: envFloat("ABUSE_CPU_PCT", 90), EgressBytesPS: envFloat("ABUSE_EGRESS_BYTES_PS", 0), WindowS: envFloat("ABUSE_WINDOW_S", 600)}); cfg.CPUPct > 0 || cfg.EgressBytesPS > 0 { // D58: observe and flag, never act
		watcher = abuse.New(abuse.LifeSource{Life: life}, log, cfg)
		go watcher.Loop(time.Duration(envFloat("ABUSE_SCAN_S", 60) * float64(time.Second)))
	}
	if v, _ := strconv.ParseFloat(os.Getenv("AUTO_EVACUATE_AFTER_S"), 64); v > 0 { // D54: opt-in; must exceed the nodes' FENCE_AFTER_S
		go life.AutoEvacuate(time.Duration(v*float64(time.Second)), 10*time.Second)
	}
	meter := metering.New(sqldb, fl, life, metering.Proxy{URL: strings.TrimRight(os.Getenv("MODEL_PROXY_URL"), "/"), Token: secret("PROXY_ADMIN_TOKEN")}, log)
	vols := state.NewVolumes(sqldb, fl, log)
	life.WithQuotas(tn, meter, vols).WithCredit(meter).WithDefaults(tn).WithVolumeMover(vols)
	go meter.Loop(60 * time.Second)
	im := images.New(fl, log)
	srv := &httpapi.Server{Reg: reg, Tenancy: tn, Fleet: fl, Life: life, Images: im, Builds: images.NewBuilds(sqldb, im, life.NodeOf), Exec: exec.New(life, fl, log), Metering: meter, Volumes: vols, Creds: network.NewCredentials(fl, log), NetReq: network.NewRequests(sqldb), MaxBodyMiB: envInt("CP_MAX_BODY_MIB", 512), RatePerMin: float64(envInt("CP_RATE_PER_MIN", 600)), AuditPath: filepath.Join(stateDir, "audit.jsonl"),
		Store: st, EvalURL: strings.TrimRight(os.Getenv("EVAL_URL"), "/"), IntakeURL: strings.TrimRight(os.Getenv("INTAKE_URL"), "/"), WebDir: webDir(env("WEB_DIR", "/web")), ConsoleDir: env("CONSOLE_DIR", "/console"), PreviewDomain: os.Getenv("PREVIEW_DOMAIN"), Abuse: watcher, Audit: log, AuditExec: env("CP_AUDIT_EXEC", "1") != "0"}
	wh := webhook.New(sqldb, httpapi.WebhookResolver{Life: life, Builds: srv.Builds, Tenancy: tn}) // D72: signed deliveries of a project's events
	log.Tap(wh.Observe)
	wh.Run(4)
	srv.Webhooks = wh
	go srv.WatchCredit(60 * time.Second) // D59: a prepaid account at zero credit is suspended; a top-up lifts it
	var specs []fleet.Spec
	if err := json.Unmarshal([]byte(env("NODES", "[]")), &specs); err != nil {
		fmt.Fprintln(os.Stderr, "NODES:", err)
		os.Exit(1)
	}
	for _, sp := range specs {
		fl.Add(sp, false)
	}
	fl.Sync()
	go fl.Loop(nodeclient.Client{}, 2*time.Second)
	log.Write(map[string]any{"op": "cp_start", "nodes": fl.Names(), "routes": len(life.Routes()), "registry": reg.Kind(), "db": sqldb.Kind})
	if ep := os.Getenv("CP_E2B_PORT"); ep != "" { // the E2B-shaped surface on its own port (D40)
		go func() {
			es := &e2b.Server{Tenancy: tn, Life: life, Images: im, Default: os.Getenv("CP_E2B_DEFAULT_TEMPLATE"), Fleet: fl, Audit: log, Secret: envdSecret(), PreviewDomain: os.Getenv("PREVIEW_DOMAIN"), Builds: srv.Builds, UploadDir: os.Getenv("CP_E2B_UPLOAD_DIR"), PublicURL: os.Getenv("CP_E2B_PUBLIC_URL")}
			if err := (&http.Server{Addr: ":" + ep, Handler: es}).ListenAndServe(); err != nil {
				fmt.Fprintln(os.Stderr, "e2b surface:", err)
			}
		}()
	}
	port := env("CP_PORT", "8700")
	fmt.Printf("controlplane %s on :%s nodes=%v store=%s registry=%s db=%s\n", replica, port, fl.Names(), st.URL, reg.Kind(), sqldb.Kind)
	if err := (&http.Server{Addr: ":" + port, Handler: srv}).ListenAndServe(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// envdSecret: the key envd access tokens (E2B SDK -> sandbox) are HMACed under. CP_ENVD_SECRET if set, else derived from the admin token, so every replica
// that shares the token also shares the secret; with neither the tokens are disabled (the envd side then answers 401).
func envdSecret() []byte {
	if v := secret("CP_ENVD_SECRET"); v != "" {
		return []byte(v)
	}
	if v := secret("CP_TOKEN"); v != "" {
		h := sha256.Sum256([]byte("cairn-envd|" + v))
		return h[:]
	}
	return nil
}
