// Package db is the SQL store for what cannot be rebuilt from the nodes (docs/002 沙箱侧的领域: contexts 1 and 7 落 SQL): accounts, projects,
// members, quotas, usage events, prices. One replica: SQLite under CP_STATE; replicas: Postgres (CP_DB=postgres://...). The SQL is written in the
// dialect both accept ($1 placeholders, TEXT / DOUBLE PRECISION / BIGINT, ON CONFLICT DO UPDATE), so the tests run on SQLite and production on Postgres.
package db

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	_ "github.com/jackc/pgx/v5/stdlib"
	_ "modernc.org/sqlite"
)

type DB struct {
	*sql.DB
	Kind  string // sqlite | postgres
	Where string
}

func Open(spec, stateDir string) (*DB, error) {
	switch {
	case spec == "":
		p := filepath.Join(stateDir, "cp.sqlite")
		d, err := sql.Open("sqlite", p+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(ON)")
		if err != nil {
			return nil, err
		}
		d.SetMaxOpenConns(1) // SQLite: one writer; the WAL lets readers through
		return &DB{DB: d, Kind: "sqlite", Where: p}, nil
	case strings.HasPrefix(spec, "postgres://") || strings.HasPrefix(spec, "postgresql://"):
		d, err := sql.Open("pgx", spec)
		if err != nil {
			return nil, err
		}
		return &DB{DB: d, Kind: "postgres", Where: redact(spec)}, nil
	}
	return nil, fmt.Errorf("CP_DB: unsupported %q (empty = sqlite in CP_STATE, or postgres://...)", spec)
}

func redact(u string) string {
	if i := strings.Index(u, "@"); i > 0 {
		if j := strings.Index(u, "://"); j > 0 && j < i {
			return u[:j+3] + "…@" + u[i+1:]
		}
	}
	return u
}

// Migrate creates what is missing; every statement is idempotent, so replicas can race on it.
func (d *DB) Migrate() error {
	for _, stmt := range schema {
		if _, err := d.Exec(stmt); err != nil {
			return fmt.Errorf("migrate: %w: %s", err, firstLine(stmt))
		}
	}
	for _, stmt := range additions { // columns added later: the error when the column exists is the normal case
		d.Exec(stmt)
	}
	return nil
}

var additions = []string{
	`ALTER TABLE members ADD COLUMN expires DOUBLE PRECISION NOT NULL DEFAULT 0`,
}

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i > 0 {
		return s[:i]
	}
	return s
}

var schema = []string{
	`CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, name TEXT NOT NULL, created DOUBLE PRECISION NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, account TEXT NOT NULL, name TEXT NOT NULL, created DOUBLE PRECISION NOT NULL)`,
	`CREATE INDEX IF NOT EXISTS projects_account ON projects(account)`,
	`CREATE TABLE IF NOT EXISTS members (id TEXT PRIMARY KEY, account TEXT NOT NULL, usr TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL, token_sha256 TEXT NOT NULL, created DOUBLE PRECISION NOT NULL)`,
	`CREATE UNIQUE INDEX IF NOT EXISTS members_usr ON members(usr)`,
	`CREATE INDEX IF NOT EXISTS members_token ON members(token_sha256)`,
	`CREATE TABLE IF NOT EXISTS quotas (project TEXT PRIMARY KEY, max_sandboxes BIGINT NOT NULL DEFAULT 0, max_storage_gib DOUBLE PRECISION NOT NULL DEFAULT 0, daily_usd DOUBLE PRECISION NOT NULL DEFAULT 0, updated DOUBLE PRECISION NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS usage_events (id TEXT PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, project TEXT NOT NULL, sandbox TEXT NOT NULL, kind TEXT NOT NULL, amount DOUBLE PRECISION NOT NULL, unit TEXT NOT NULL, cost_usd DOUBLE PRECISION NOT NULL, source TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '')`,
	`CREATE INDEX IF NOT EXISTS usage_project_ts ON usage_events(project, ts)`,
	`CREATE INDEX IF NOT EXISTS usage_sandbox ON usage_events(sandbox)`,
	`CREATE TABLE IF NOT EXISTS prices (version BIGINT PRIMARY KEY, body TEXT NOT NULL, created DOUBLE PRECISION NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS meter_cursor (sandbox TEXT PRIMARY KEY, cost_usd DOUBLE PRECISION NOT NULL, tokens_in BIGINT NOT NULL, tokens_out BIGINT NOT NULL, tokens_cached BIGINT NOT NULL, seen DOUBLE PRECISION NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS meter_state (k TEXT PRIMARY KEY, v DOUBLE PRECISION NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS meter_egress (sandbox TEXT PRIMARY KEY, bytes BIGINT NOT NULL, seen DOUBLE PRECISION NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS project_network (project TEXT PRIMARY KEY, body TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS network_requests (id TEXT PRIMARY KEY, sandbox TEXT NOT NULL, project TEXT NOT NULL, by_user TEXT NOT NULL, body TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, status TEXT NOT NULL, decided_by TEXT NOT NULL DEFAULT '', decided DOUBLE PRECISION NOT NULL DEFAULT 0)`,
	`CREATE INDEX IF NOT EXISTS network_requests_project ON network_requests(project, status)`,
	`CREATE TABLE IF NOT EXISTS meter_statements (id TEXT PRIMARY KEY, project TEXT NOT NULL, from_ts DOUBLE PRECISION NOT NULL, to_ts DOUBLE PRECISION NOT NULL, total_usd DOUBLE PRECISION NOT NULL, sandboxes BIGINT NOT NULL, lines TEXT NOT NULL, created DOUBLE PRECISION NOT NULL)`,
	`CREATE INDEX IF NOT EXISTS meter_statements_project ON meter_statements(project, from_ts)`,
	`CREATE TABLE IF NOT EXISTS volumes (id TEXT PRIMARY KEY, project TEXT NOT NULL, node TEXT NOT NULL, name TEXT NOT NULL, size_mib BIGINT NOT NULL, created DOUBLE PRECISION NOT NULL)`,
	`CREATE INDEX IF NOT EXISTS volumes_project ON volumes(project)`,
	`CREATE TABLE IF NOT EXISTS builds (id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL, input TEXT NOT NULL, node TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', created DOUBLE PRECISION NOT NULL, finished DOUBLE PRECISION NOT NULL DEFAULT 0)`,
	`CREATE INDEX IF NOT EXISTS builds_project ON builds(project, created)`,
	`CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY, account TEXT NOT NULL, project TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL, token_sha256 TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, expires DOUBLE PRECISION NOT NULL DEFAULT 0, last_used DOUBLE PRECISION NOT NULL DEFAULT 0, created_by TEXT NOT NULL)`,
	`CREATE UNIQUE INDEX IF NOT EXISTS api_keys_token ON api_keys(token_sha256)`,
	`CREATE INDEX IF NOT EXISTS api_keys_project ON api_keys(project)`,
	`CREATE TABLE IF NOT EXISTS meter_cpu (sandbox TEXT PRIMARY KEY, usec BIGINT NOT NULL, seen DOUBLE PRECISION NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS account_tombstones (id TEXT PRIMARY KEY, deleted DOUBLE PRECISION NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS webhooks (id TEXT PRIMARY KEY, project TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL, events TEXT NOT NULL DEFAULT '', created DOUBLE PRECISION NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, last_status INTEGER NOT NULL DEFAULT 0, last_at DOUBLE PRECISION NOT NULL DEFAULT 0, by_user TEXT NOT NULL)`,
	`CREATE INDEX IF NOT EXISTS webhooks_project ON webhooks(project)`,
	`CREATE TABLE IF NOT EXISTS credits (id TEXT PRIMARY KEY, account TEXT NOT NULL, usd DOUBLE PRECISION NOT NULL, note TEXT NOT NULL, by_user TEXT NOT NULL, at_ts DOUBLE PRECISION NOT NULL)`,
	`CREATE INDEX IF NOT EXISTS credits_account ON credits(account)`,
	`CREATE TABLE IF NOT EXISTS account_billing (account TEXT PRIMARY KEY, mode TEXT NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS invites (code_sha256 TEXT PRIMARY KEY, id TEXT NOT NULL, note TEXT NOT NULL, credit_usd DOUBLE PRECISION NOT NULL DEFAULT 0, max_sandboxes BIGINT NOT NULL DEFAULT 0, daily_usd DOUBLE PRECISION NOT NULL DEFAULT 0, created DOUBLE PRECISION NOT NULL, expires DOUBLE PRECISION NOT NULL DEFAULT 0, used_by TEXT NOT NULL DEFAULT '', used_at DOUBLE PRECISION NOT NULL DEFAULT 0, created_by TEXT NOT NULL)`,
	`CREATE TABLE IF NOT EXISTS suspensions (account TEXT PRIMARY KEY, reason TEXT NOT NULL, by_user TEXT NOT NULL, since DOUBLE PRECISION NOT NULL)`,
}

func getenv(k string) string { return os.Getenv(k) }
