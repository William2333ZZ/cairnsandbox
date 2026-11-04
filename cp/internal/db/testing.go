package db

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"net/url"
	"testing"
)

// OpenForTest: a migrated database for one test. Default SQLite in a temp dir; with CAIRN_TEST_PG (an admin DSN such as postgres://u:p@host:5432/postgres)
// the test gets its own throw-away Postgres database instead, so the same suite proves the SQL works on both (the deployment runs Postgres).
func OpenForTest(t testing.TB) *DB {
	t.Helper()
	admin := getenv("CAIRN_TEST_PG")
	if admin == "" {
		d, err := Open("", t.TempDir())
		if err != nil {
			t.Fatal(err)
		}
		if err := d.Migrate(); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { d.Close() })
		return d
	}
	var b [6]byte
	rand.Read(b[:])
	name := "t_" + hex.EncodeToString(b[:])
	a, err := sql.Open("pgx", admin)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := a.Exec("CREATE DATABASE " + name); err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(admin)
	if err != nil {
		t.Fatal(err)
	}
	u.Path = "/" + name
	d, err := Open(u.String(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Migrate(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		d.Close()
		a.Exec("DROP DATABASE IF EXISTS " + name + " WITH (FORCE)")
		a.Close()
	})
	return d
}
