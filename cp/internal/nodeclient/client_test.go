package nodeclient

import (
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"fcnode/cp/internal/audit"
	"fcnode/cp/internal/fleet"
	"fcnode/cp/internal/registry"
)

func TestAnHTTPSNodeIsReachedOnlyThroughATrustedCertificate(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tok" {
			http.Error(w, "no", 401)
			return
		}
		w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()
	reg, _ := registry.NewFileRegistry(t.TempDir())
	fl := fleet.New(reg, audit.Discard("t"))
	fl.Add(fleet.Spec{Name: "n", URL: srv.URL, Token: "tok"}, false)
	n := fl.Get("n")
	c := Client{}
	if _, _, err := c.JSON(n, "GET", "/health", nil, 5*time.Second); err == nil {
		t.Fatal("an untrusted certificate was accepted")
	}
	crt := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	if !SetCA(crt) {
		t.Fatal("CA not accepted")
	}
	st, out, err := c.JSON(n, "GET", "/health", nil, 5*time.Second)
	if err != nil || st != 200 || out["ok"] != true {
		t.Fatalf("trusted: %d %v %v", st, out, err)
	}
}
