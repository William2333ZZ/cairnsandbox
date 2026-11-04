// Package webhook delivers a project's events to URLs its owner registered (D72). Events come from the audit log (a tap, so nothing else in the control plane
// changes); each is signed with the webhook's secret (HMAC-SHA256 over the body), retried a few times, and the webhook is switched off after a run of failures.
// The control plane sits inside the deployer's network, so the dialer refuses private, loopback and link-local addresses *after* DNS resolution (an SSRF guard
// that DNS rebinding cannot walk around), only https is accepted, and redirects are not followed.
package webhook

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
	"syscall"
	"time"

	"fcnode/cp/internal/db"
	"fcnode/cp/internal/registry"
)

// Events a webhook can subscribe to. The audit log has many more lines; these are the ones a customer can act on.
var Events = []string{"sandbox.created", "network.approval_requested", "build.finished", "account.suspended", "account.unsuspended", "account.credit", "ping"}

type Hook struct {
	ID       string   `json:"id"`
	Project  string   `json:"project"`
	URL      string   `json:"url"`
	Events   []string `json:"events"` // empty = all
	Created  float64  `json:"created"`
	Disabled bool     `json:"disabled"`
	Failures int      `json:"failures"`
	LastCode int      `json:"last_status"`
	LastAt   float64  `json:"last_at"`
	By       string   `json:"created_by"`
}

// Resolver answers the questions the audit lines leave open (which project does this sandbox / build / account belong to).
type Resolver interface {
	ProjectOfSandbox(id string) string
	ProjectOfBuild(id string) string
	ProjectsOfAccount(account string) []string
}

type Dispatcher struct {
	db            *db.DB
	res           Resolver
	q             chan item
	allowPrivate  bool // tests only
	client        *http.Client
	MaxPerProject int
	backoff       []time.Duration
}

type item struct {
	hook   Hook
	secret string
	event  string
	body   []byte
	id     string
}

const maxFailures = 10

func New(d *db.DB, res Resolver) *Dispatcher {
	x := &Dispatcher{db: d, res: res, q: make(chan item, 1024), MaxPerProject: 20, backoff: []time.Duration{0, 5 * time.Second, 30 * time.Second}}
	x.client = &http.Client{Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		Transport: &http.Transport{DialContext: (&net.Dialer{Timeout: 5 * time.Second, Control: x.guard}).DialContext, DisableKeepAlives: true}}
	return x
}

// AllowPrivateForTests lets a test deliver to httptest servers on 127.0.0.1.
func (x *Dispatcher) AllowPrivateForTests() { x.allowPrivate = true }

// Run starts the delivery workers.
func (x *Dispatcher) Run(workers int) {
	for i := 0; i < workers; i++ {
		go func() {
			for it := range x.q {
				x.deliver(it)
			}
		}()
	}
}

// guard runs when the connection's address is known (after DNS): refuse anything that is not a public unicast address.
func (x *Dispatcher) guard(network, address string, _ syscall.RawConn) error {
	if x.allowPrivate {
		return nil
	}
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return err
	}
	ip, err := netip.ParseAddr(host)
	if err != nil {
		return err
	}
	if forbidden(ip) {
		return fmt.Errorf("webhook target %s is not a public address", ip)
	}
	return nil
}

func forbidden(ip netip.Addr) bool {
	ip = ip.Unmap()
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsMulticast() || ip.IsUnspecified() || ip.IsInterfaceLocalMulticast() ||
		(ip.Is4() && ip.As4()[0] == 100 && ip.As4()[1]&0xc0 == 64) || // 100.64.0.0/10 carrier-grade NAT (shared address space)
		(ip.Is4() && ip.As4()[0] == 0) || (ip.Is4() && ip.As4()[0] >= 240) // "this network", reserved
}

// ValidURL: https only, a host, no credentials in the URL, no obviously private literal (the dial-time guard is the real protection). lax = tests: http and loopback allowed.
func ValidURL(raw string, lax bool) error {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return errors.New("url: a full https URL")
	}
	if u.Scheme != "https" && !(lax && u.Scheme == "http") {
		return errors.New("url: https only")
	}
	if u.User != nil {
		return errors.New("url: no credentials in the URL; the signature header is how you verify us")
	}
	if lax {
		return nil
	}
	if ip, err := netip.ParseAddr(u.Hostname()); err == nil && forbidden(ip) {
		return errors.New("url: a private or loopback address")
	}
	if strings.EqualFold(u.Hostname(), "localhost") {
		return errors.New("url: localhost")
	}
	return nil
}

// ---- registry of hooks ----

func (x *Dispatcher) Add(project, rawURL string, events []string, by string) (Hook, string, error) {
	if err := ValidURL(rawURL, x.allowPrivate); err != nil {
		return Hook{}, "", err
	}
	ok := map[string]bool{}
	for _, e := range Events {
		ok[e] = true
	}
	for _, e := range events {
		if !ok[e] {
			return Hook{}, "", fmt.Errorf("events: unknown %q (known: %s)", e, strings.Join(Events, ", "))
		}
	}
	var n int
	x.db.QueryRow(`SELECT count(*) FROM webhooks WHERE project = $1`, project).Scan(&n)
	if n >= x.MaxPerProject {
		return Hook{}, "", fmt.Errorf("at most %d webhooks per project", x.MaxPerProject)
	}
	h := Hook{ID: "wh-" + registry.NewToken()[:10], Project: project, URL: rawURL, Events: events, Created: now(), By: by}
	secret := "whsec_" + registry.NewToken() + registry.NewToken()[:16]
	if _, err := x.db.Exec(`INSERT INTO webhooks(id, project, url, secret, events, created, by_user) VALUES ($1,$2,$3,$4,$5,$6,$7)`, h.ID, h.Project, h.URL, secret, strings.Join(events, ","), h.Created, by); err != nil {
		return Hook{}, "", err
	}
	return h, secret, nil
}

func (x *Dispatcher) List(project string) ([]Hook, error) {
	rows, err := x.db.Query(`SELECT id, project, url, events, created, disabled, failures, last_status, last_at, by_user FROM webhooks WHERE project = $1 ORDER BY created`, project)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Hook{}
	for rows.Next() {
		h, err := scan(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, nil
}

func scan(rows interface{ Scan(...any) error }) (Hook, error) {
	var h Hook
	var ev string
	var dis int
	if err := rows.Scan(&h.ID, &h.Project, &h.URL, &ev, &h.Created, &dis, &h.Failures, &h.LastCode, &h.LastAt, &h.By); err != nil {
		return h, err
	}
	h.Disabled = dis != 0
	h.Events = []string{}
	if ev != "" {
		h.Events = strings.Split(ev, ",")
	}
	return h, nil
}

func (x *Dispatcher) Delete(project, id string) bool {
	res, err := x.db.Exec(`DELETE FROM webhooks WHERE id = $1 AND project = $2`, id, project)
	if err != nil {
		return false
	}
	n, _ := res.RowsAffected()
	return n > 0
}

// Enable switches a disabled hook back on and clears its failure count.
func (x *Dispatcher) Enable(project, id string) bool {
	res, err := x.db.Exec(`UPDATE webhooks SET disabled = 0, failures = 0 WHERE id = $1 AND project = $2`, id, project)
	if err != nil {
		return false
	}
	n, _ := res.RowsAffected()
	return n > 0
}

// Ping queues a `ping` event to one hook (the "is my endpoint wired up" button).
func (x *Dispatcher) Ping(project, id string) bool {
	var h Hook
	var secret string
	row := x.db.QueryRow(`SELECT id, project, url, events, created, disabled, failures, last_status, last_at, by_user, secret FROM webhooks WHERE id = $1 AND project = $2`, id, project)
	var ev string
	var dis int
	if err := row.Scan(&h.ID, &h.Project, &h.URL, &ev, &h.Created, &dis, &h.Failures, &h.LastCode, &h.LastAt, &h.By, &secret); err != nil {
		return false
	}
	x.enqueue(h, secret, "ping", map[string]any{"message": "hello from the Cairn sandbox cloud"})
	return true
}

// ---- from audit lines to deliveries ----

// Observe is the audit tap: it maps a line to a public event and queues deliveries. It must not block.
func (x *Dispatcher) Observe(ev map[string]any) {
	event, data, projects := x.classify(ev)
	if event == "" {
		return
	}
	for _, p := range projects {
		rows, err := x.db.Query(`SELECT id, project, url, events, created, disabled, failures, last_status, last_at, by_user, secret FROM webhooks WHERE project = $1 AND disabled = 0`, p)
		if err != nil {
			continue
		}
		var hooks []Hook
		var secrets []string
		for rows.Next() {
			var h Hook
			var ev string
			var dis int
			var secret string
			if rows.Scan(&h.ID, &h.Project, &h.URL, &ev, &h.Created, &dis, &h.Failures, &h.LastCode, &h.LastAt, &h.By, &secret) == nil {
				h.Events = []string{}
				if ev != "" {
					h.Events = strings.Split(ev, ",")
				}
				hooks, secrets = append(hooks, h), append(secrets, secret)
			}
		}
		rows.Close()
		for i, h := range hooks {
			if len(h.Events) == 0 || contains(h.Events, event) {
				x.enqueue(h, secrets[i], event, data)
			}
		}
	}
}

func contains(l []string, s string) bool {
	for _, x := range l {
		if x == s {
			return true
		}
	}
	return false
}

func str(ev map[string]any, k string) string { s, _ := ev[k].(string); return s }

func (x *Dispatcher) classify(ev map[string]any) (event string, data map[string]any, projects []string) {
	switch str(ev, "op") {
	case "create":
		if code, _ := ev["status"].(int); code == 201 {
			return "sandbox.created", map[string]any{"sandbox": ev["sandbox"], "node": ev["node"]}, []string{str(ev, "project")}
		}
	case "network_relax_requested":
		if p := x.res.ProjectOfSandbox(str(ev, "sandbox")); p != "" {
			return "network.approval_requested", map[string]any{"sandbox": ev["sandbox"], "request": ev["request"]}, []string{p}
		}
	case "build_done", "build_failed":
		if p := x.res.ProjectOfBuild(str(ev, "build")); p != "" {
			return "build.finished", map[string]any{"build": ev["build"], "kind": ev["kind"], "ok": str(ev, "op") == "build_done", "error": ev["error"]}, []string{p}
		}
	case "account_suspend", "account_unsuspend", "credit":
		name := map[string]string{"account_suspend": "account.suspended", "account_unsuspend": "account.unsuspended", "credit": "account.credit"}[str(ev, "op")]
		d := map[string]any{"account": ev["account"]}
		if name == "account.suspended" {
			d["reason"] = ev["reason"]
		}
		if name == "account.credit" {
			d["usd"] = ev["usd"]
		}
		return name, d, x.res.ProjectsOfAccount(str(ev, "account"))
	}
	return "", nil, nil
}

func (x *Dispatcher) enqueue(h Hook, secret, event string, data map[string]any) {
	id := "dl-" + registry.NewToken()[:12]
	body, _ := json.Marshal(map[string]any{"id": id, "type": event, "project": h.Project, "created": now(), "data": data})
	select {
	case x.q <- item{hook: h, secret: secret, event: event, body: body, id: id}:
	default: // the queue is full: drop rather than slow the control plane
	}
}

// Sign is the value of the X-Cairn-Signature header: "sha256=" + hex(HMAC-SHA256(secret, body)).
func Sign(secret string, body []byte) string {
	m := hmac.New(sha256.New, []byte(secret))
	m.Write(body)
	return "sha256=" + hex.EncodeToString(m.Sum(nil))
}

func (x *Dispatcher) deliver(it item) {
	var code int
	for i, wait := range x.backoff {
		if wait > 0 {
			time.Sleep(wait)
		}
		req, err := http.NewRequestWithContext(context.Background(), "POST", it.hook.URL, bytes.NewReader(it.body))
		if err != nil {
			break
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("User-Agent", "cairn-webhook/1")
		req.Header.Set("X-Cairn-Event", it.event)
		req.Header.Set("X-Cairn-Delivery", it.id)
		req.Header.Set("X-Cairn-Signature", Sign(it.secret, it.body))
		resp, err := x.client.Do(req)
		if err == nil {
			code = resp.StatusCode
			resp.Body.Close()
			if code >= 200 && code < 300 {
				x.record(it.hook.ID, code, true)
				return
			}
		} else {
			code = 0
		}
		_ = i
	}
	x.record(it.hook.ID, code, false)
}

func (x *Dispatcher) record(id string, code int, ok bool) {
	if ok {
		x.db.Exec(`UPDATE webhooks SET failures = 0, last_status = $1, last_at = $2 WHERE id = $3`, code, now(), id)
		return
	}
	x.db.Exec(`UPDATE webhooks SET failures = failures + 1, last_status = $1, last_at = $2, disabled = CASE WHEN failures + 1 >= $3 THEN 1 ELSE disabled END WHERE id = $4`, code, now(), maxFailures, id)
}

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }
