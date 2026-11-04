package metering

import (
	"database/sql"

	"fcnode/cp/internal/registry"
)

// CreditEntry is one line of an account's credit ledger (D59): a top-up is positive, an adjustment or refund negative.
type CreditEntry struct {
	ID      string  `json:"id"`
	Account string  `json:"account"`
	USD     float64 `json:"usd"`
	Note    string  `json:"note"`
	By      string  `json:"by"`
	At      float64 `json:"at"`
}

// Credit appends to the ledger. The payment provider's webhook (or the deployer by hand) calls this; nothing else adds money.
func (m *Metering) Credit(account string, usd float64, note, by string) (CreditEntry, error) {
	e := CreditEntry{ID: "cr-" + registry.NewToken()[:10], Account: account, USD: usd, Note: note, By: by, At: now()}
	_, err := m.db.Exec(`INSERT INTO credits(id, account, usd, note, by_user, at_ts) VALUES ($1, $2, $3, $4, $5, $6)`, e.ID, e.Account, e.USD, e.Note, e.By, e.At)
	return e, err
}

// Credits: the ledger, newest first.
func (m *Metering) Credits(account string, limit int) ([]CreditEntry, error) {
	rows, err := m.db.Query(`SELECT id, account, usd, note, by_user, at_ts FROM credits WHERE account = $1 ORDER BY at_ts DESC LIMIT $2`, account, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []CreditEntry{}
	for rows.Next() {
		var e CreditEntry
		if err := rows.Scan(&e.ID, &e.Account, &e.USD, &e.Note, &e.By, &e.At); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, nil
}

// Balance: credits in, lifetime spend of every project of the account out.
func (m *Metering) Balance(account string) (credits, spent float64, err error) {
	var c, s sql.NullFloat64
	if err = m.db.QueryRow(`SELECT sum(usd) FROM credits WHERE account = $1`, account).Scan(&c); err != nil {
		return
	}
	if err = m.db.QueryRow(`SELECT sum(cost_usd) FROM usage_events WHERE project IN (SELECT id FROM projects WHERE account = $1)`, account).Scan(&s); err != nil {
		return
	}
	return c.Float64, s.Float64, nil
}

// BalanceUSD satisfies lifecycle.Credit. A database error reads as "plenty": a failing store must not refuse every customer.
func (m *Metering) BalanceUSD(account string) float64 {
	c, s, err := m.Balance(account)
	if err != nil {
		return 1e12
	}
	return c - s
}
