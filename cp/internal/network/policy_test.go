package network

import "testing"

func TestValidate(t *testing.T) {
	good := map[string]any{"mode": "audited", "allow_hosts": []any{"*.example.com"}, "rules": []any{
		map[string]any{"name": "a", "match": map[string]any{"scheme": "http", "host": "x", "method": []any{"GET"}, "path": "/v1/*"}, "action": map[string]any{"allow": true, "inject": "key1"}},
		map[string]any{"match": map[string]any{}, "action": map[string]any{"allow": false}},
		map[string]any{"name": "ds", "match": map[string]any{"scheme": "http", "host": "api.deepseek.com"}, "action": map[string]any{"allow": true, "inject": "ds", "upstream": "https"}},
	}}
	if err := Validate(good); err != nil {
		t.Fatal(err)
	}
	if err := Validate(nil); err != nil {
		t.Fatal(err)
	}
	bad := []map[string]any{
		{"mode": "open"},
		{"allow_hosts": "x"},
		{"allow_direct": []any{1}},
		{"rules": "x"},
		{"rules": []any{"x"}},
		{"rules": []any{map[string]any{"name": "Bad Name", "action": map[string]any{"allow": true}}}},
		{"rules": []any{map[string]any{"match": map[string]any{"scheme": "ftp"}, "action": map[string]any{"allow": true}}}},
		{"rules": []any{map[string]any{"match": map[string]any{"path": "v1"}, "action": map[string]any{"allow": true}}}},
		{"rules": []any{map[string]any{"match": map[string]any{"method": "GET"}, "action": map[string]any{"allow": true}}}},
		{"rules": []any{map[string]any{"match": map[string]any{}}}},
		{"rules": []any{map[string]any{"match": map[string]any{}, "action": map[string]any{}}}},
		{"rules": []any{map[string]any{"match": map[string]any{}, "action": map[string]any{"allow": false, "inject": "k"}}}},
		{"rules": []any{map[string]any{"match": map[string]any{}, "action": map[string]any{"allow": true, "inject": "Bad Name"}}}},
		{"rules": []any{map[string]any{"match": map[string]any{"scheme": "http"}, "action": map[string]any{"allow": true, "upstream": "tls"}}}},
		{"rules": []any{map[string]any{"match": map[string]any{"scheme": "http"}, "action": map[string]any{"allow": false, "upstream": "https"}}}},
		{"rules": []any{map[string]any{"match": map[string]any{"host": "x"}, "action": map[string]any{"allow": true, "upstream": "https"}}}},
		{"rules": []any{map[string]any{"match": map[string]any{"scheme": "https"}, "action": map[string]any{"allow": true, "upstream": "https"}}}},
	}
	for i, b := range bad {
		if err := Validate(b); err == nil {
			t.Fatalf("bad %d accepted: %v", i, b)
		}
	}
}
