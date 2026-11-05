// Package exec is context 8 (docs/002 沙箱侧的领域 执行面与 agent 接口) as the control plane sees it: every /sandboxes/<id>/... call goes to the
// owning node, chunked responses pass through chunk by chunk; a fork's clones become routes. The E2B-shaped surface will be another skin here.
package exec

import (
	"bufio"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/lifecycle"
	"fcnode/cp/internal/nodeclient"
)

type Auditor interface{ Write(map[string]any) }

type Exec struct {
	life  *lifecycle.Lifecycle
	fleet *fleet.Fleet
	nc    nodeclient.Client
	audit Auditor
}

func New(l *lifecycle.Lifecycle, f *fleet.Fleet, audit Auditor) *Exec {
	return &Exec{life: l, fleet: f, audit: audit}
}

type jsonWriter func(w http.ResponseWriter, code int, v any)

// Forward serves one /sandboxes/<sid><sub> request against the owning node.
func (e *Exec) Forward(w http.ResponseWriter, r *http.Request, sid, sub string, raw []byte, body map[string]any, send jsonWriter) {
	rt, rerr := e.life.RouteErr(sid)
	if rerr != nil {
		send(w, 503, map[string]any{"error": "registry unavailable: " + rerr.Error(), "sandbox": sid}) // not "no such sandbox": the route may well exist
		return
	}
	if rt == nil {
		send(w, 404, map[string]any{"error": "no sandbox " + sid})
		return
	}
	node := e.fleet.Get(rt.Node)
	if node == nil {
		e.fleet.Sync()
		node = e.fleet.Get(rt.Node)
	}
	if node == nil {
		send(w, 503, map[string]any{"error": "node " + rt.Node + " is not known to this replica", "sandbox": sid})
		return
	}
	if !node.Alive() {
		send(w, 503, map[string]any{"error": "node " + node.Name + " is down", "sandbox": sid, "durable": rt.Durable})
		return
	}
	if sub == "/fork" && r.Method == "POST" { // the clones are new sandboxes: route them like created ones
		st, resp, err := e.nc.JSON(node, "POST", r.URL.RequestURI(), body, 300*time.Second)
		if err != nil {
			send(w, 502, map[string]any{"error": err.Error()})
			return
		}
		if st == 201 {
			if arr, ok := resp["clones"].([]any); ok {
				for _, x := range arr {
					if c, ok := x.(map[string]any); ok {
						id, _ := c["id"].(string)
						cls, _ := c["class"].(string)
						dur, _ := c["durable"].(bool)
						e.life.SetRoute(id, lifecycle.Route{Node: node.Name, Class: cls, Durable: dur, Created: float64(time.Now().UnixNano()) / 1e9, Project: rt.Project})
					}
				}
			}
		}
		send(w, st, resp)
		return
	}
	if strings.EqualFold(r.Header.Get("Upgrade"), "websocket") && strings.Contains(sub, "/ports/") {
		e.tunnel(w, r, node, sid)
		return
	}
	hdr := http.Header{}
	ct := r.Header.Get("Content-Type")
	if ct == "" {
		ct = "application/json"
	}
	hdr.Set("Content-Type", ct)
	if v := r.Header.Get("Idempotency-Key"); v != "" {
		hdr.Set("Idempotency-Key", v)
	}
	resp, err := e.nc.Do(node, r.Method, r.URL.RequestURI(), raw, 0, hdr)
	if err != nil {
		send(w, 502, map[string]any{"error": "node unreachable: " + err.Error()})
		return
	}
	defer resp.Body.Close()
	if r.Method == "DELETE" && resp.StatusCode == 200 && sub == "" {
		e.life.Forget(sid, node) // before the client can see the 200: a caller that checks right after must find the route gone
	}
	for _, k := range []string{"Content-Type", "Content-Length"} {
		if v := resp.Header.Get(k); v != "" {
			w.Header().Set(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	flusher, _ := w.(http.Flusher)
	buf := make([]byte, 65536)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				e.audit.Write(map[string]any{"op": "forward_cut", "path": r.URL.Path, "error": werr.Error()})
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if rerr != nil {
			if rerr != io.EOF {
				e.audit.Write(map[string]any{"op": "forward_cut", "path": r.URL.Path, "error": rerr.Error()})
			}
			break
		}
	}
}

// tunnel carries a WebSocket (any Upgrade) on an exposed port end to end (D39 补): the client's connection is hijacked, the node is dialed with the
// node bearer, the handshake is relayed as bytes, and from then on the two sockets are copied into each other until one side closes.
func (e *Exec) tunnel(w http.ResponseWriter, r *http.Request, node *fleet.Node, sid string) {
	u, err := url.Parse(node.URL)
	if err != nil {
		http.Error(w, "bad node url", 502)
		return
	}
	host := u.Host
	if !strings.Contains(host, ":") {
		host += map[bool]string{true: ":443", false: ":80"}[u.Scheme == "https"]
	}
	var up net.Conn
	if u.Scheme == "https" { // D62: the same trust as nodeclient
		up, err = tls.DialWithDialer(&net.Dialer{Timeout: 10 * time.Second}, "tcp", host, nodeclient.TLSConfig().Clone())
	} else {
		up, err = net.DialTimeout("tcp", host, 10*time.Second)
	}
	if err != nil {
		http.Error(w, "node unreachable: "+err.Error(), 502)
		return
	}
	var req strings.Builder
	fmt.Fprintf(&req, "%s %s HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer %s\r\n", r.Method, r.URL.RequestURI(), host, node.Token())
	for k, vs := range r.Header {
		if lk := strings.ToLower(k); lk == "host" || lk == "authorization" || lk == "proxy-authorization" {
			continue
		}
		for _, v := range vs {
			fmt.Fprintf(&req, "%s: %s\r\n", k, v)
		}
	}
	req.WriteString("\r\n")
	if _, err := up.Write([]byte(req.String())); err != nil {
		up.Close()
		http.Error(w, "node unreachable: "+err.Error(), 502)
		return
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		up.Close()
		http.Error(w, "upgrade not supported here", 500)
		return
	}
	cc, buf, err := hj.Hijack()
	if err != nil {
		up.Close()
		return
	}
	e.audit.Write(map[string]any{"op": "port_tunnel", "sandbox": sid, "path": r.URL.Path, "node": node.Name})
	done := make(chan struct{}, 2)
	go func() { // node -> client (the handshake answer first, then frames)
		io.Copy(cc, up)
		done <- struct{}{}
	}()
	go func() { // client -> node, starting with whatever the server already buffered past the request head
		if n := buf.Reader.Buffered(); n > 0 {
			b, _ := buf.Reader.Peek(n)
			up.Write(b)
			buf.Reader.Discard(n)
		}
		io.Copy(up, cc)
		done <- struct{}{}
	}()
	<-done
	up.Close()
	cc.Close()
	<-done
}

var _ = bufio.NewReader
