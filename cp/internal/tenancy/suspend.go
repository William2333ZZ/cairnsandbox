package tenancy

import (
	"sync"
	"time"
)

// Suspension is the deployer's stop button for one account (013: abuse): the account's members can still read and clean up, but not start or change anything.
type Suspension struct {
	Account string  `json:"account"`
	Reason  string  `json:"reason"`
	By      string  `json:"by"`
	At      float64 `json:"at"`
}

type suspCache struct {
	mu   sync.Mutex
	at   time.Time
	rows map[string]Suspension
}

// Suspend records (or re-records) the account's suspension. Callers pause its sandboxes; this only keeps the fact.
func (t *Tenancy) Suspend(account, reason, by string) error {
	if _, err := t.Account(account); err != nil {
		return err
	}
	if _, err := t.db.Exec(`INSERT INTO suspensions(account, reason, by_user, since) VALUES ($1, $2, $3, $4)
		ON CONFLICT(account) DO UPDATE SET reason = EXCLUDED.reason, by_user = EXCLUDED.by_user, since = EXCLUDED.since`, account, reason, by, now()); err != nil {
		return err
	}
	t.susp.drop()
	return nil
}

// Unsuspend lifts it; ErrNotFound when the account was not suspended.
func (t *Tenancy) Unsuspend(account string) error {
	res, err := t.db.Exec(`DELETE FROM suspensions WHERE account = $1`, account)
	if err != nil {
		return err
	}
	t.susp.drop()
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrNotFound
	}
	return nil
}

// SuspensionOf: the account's suspension, if any. Cached for a few seconds (every request asks); a database that cannot be reached
// is treated as "not suspended" for at most that long, never as a reason to refuse the request.
func (t *Tenancy) SuspensionOf(account string) (Suspension, bool) {
	rows := t.susp.get(t)
	s, ok := rows[account]
	return s, ok
}

// Suspensions lists every suspended account (the deployer's view).
func (t *Tenancy) Suspensions() []Suspension {
	out := []Suspension{}
	for _, s := range t.susp.get(t) {
		out = append(out, s)
	}
	return out
}

func (c *suspCache) drop() { c.mu.Lock(); c.rows = nil; c.mu.Unlock() }

func (c *suspCache) get(t *Tenancy) map[string]Suspension {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.rows != nil && time.Since(c.at) < 3*time.Second {
		return c.rows
	}
	rows := map[string]Suspension{}
	q, err := t.db.Query(`SELECT account, reason, by_user, since FROM suspensions`)
	if err != nil {
		if c.rows != nil {
			return c.rows
		}
		return rows
	}
	defer q.Close()
	for q.Next() {
		var s Suspension
		if q.Scan(&s.Account, &s.Reason, &s.By, &s.At) == nil {
			rows[s.Account] = s
		}
	}
	c.rows, c.at = rows, time.Now()
	return rows
}
