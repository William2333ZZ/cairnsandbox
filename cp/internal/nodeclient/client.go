// Package nodeclient talks to a node agent (CONTRACT §2) with its bearer. Used by every context that reaches a node; it knows nothing about routes.
package nodeclient

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sync"
	"time"

	"fcnode/cp/internal/fleet"
)

type Client struct{}

// TLS to nodes (D62): a node URL that starts with https:// is verified against NODE_CA_FILE (a PEM bundle: the deployer's CA, or the node's own certificate)
// and falls back to the system roots when the variable is not set. Plain http:// URLs are unchanged.
var (
	tlsOnce sync.Once
	tlsConf *tls.Config
	tr      *http.Transport
)

func TLSConfig() *tls.Config {
	tlsOnce.Do(func() {
		tlsConf = &tls.Config{MinVersion: tls.VersionTLS12}
		if f := os.Getenv("NODE_CA_FILE"); f != "" {
			if pem, err := os.ReadFile(f); err == nil {
				pool := x509.NewCertPool()
				if pool.AppendCertsFromPEM(pem) {
					tlsConf.RootCAs = pool
				}
			}
		}
		tr = &http.Transport{TLSClientConfig: tlsConf, MaxIdleConnsPerHost: 8, IdleConnTimeout: 60 * time.Second}
	})
	return tlsConf
}

// SetCA replaces the trusted CA bundle (tests, or a reload).
func SetCA(pem []byte) bool {
	TLSConfig()
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return false
	}
	tlsConf.RootCAs = pool
	tr.CloseIdleConnections()
	return true
}

// Do is one request; the caller reads and closes the body. timeout 0 = none (streams).
func (Client) Do(n *fleet.Node, method, path string, body []byte, timeout time.Duration, hdr http.Header) (*http.Response, error) {
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, n.URL+path, rd)
	if err != nil {
		return nil, err
	}
	for k, vs := range hdr {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	req.Header.Set("Authorization", "Bearer "+n.Token())
	if body != nil && req.Header.Get("Content-Type") == "" {
		req.Header.Set("Content-Type", "application/json")
	}
	TLSConfig()
	return (&http.Client{Timeout: timeout, Transport: tr}).Do(req)
}

// DoStream is Do with a streamed request body (the E2B envd relay: a Connect request may be chunked and a response may run for hours). contentLength <= 0
// with a body means "unknown": the transport sends it chunked. The caller reads and closes the response body.
func (Client) DoStream(n *fleet.Node, method, path string, body io.Reader, contentLength int64, hdr http.Header) (*http.Response, error) {
	req, err := http.NewRequest(method, n.URL+path, body)
	if err != nil {
		return nil, err
	}
	if body != nil && contentLength > 0 {
		req.ContentLength = contentLength
	}
	for k, vs := range hdr {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	req.Header.Set("Authorization", "Bearer "+n.Token())
	TLSConfig()
	return (&http.Client{Transport: tr}).Do(req)
}

// JSON is Do for JSON in, JSON out.
func (c Client) JSON(n *fleet.Node, method, path string, body any, timeout time.Duration) (int, map[string]any, error) {
	var b []byte
	if body != nil {
		b, _ = json.Marshal(body)
	}
	resp, err := c.Do(n, method, path, b, timeout, nil)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	if len(data) > 0 {
		if err := json.Unmarshal(data, &out); err != nil {
			out = map[string]any{"raw": string(data[:min(200, len(data))])}
		}
	}
	return resp.StatusCode, out, nil
}

// Health satisfies fleet.Poller.
func (c Client) Health(n *fleet.Node) (map[string]any, error) {
	st, h, err := c.JSON(n, "GET", "/health", nil, 3*time.Second)
	if err != nil {
		return nil, err
	}
	if st != 200 {
		return nil, fmt.Errorf("health %d", st)
	}
	return h, nil
}
