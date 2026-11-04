package tenancy

import "errors"

// AccountDelete removes an account and everything that only has meaning inside it: members, API keys, projects with their quotas and default networks,
// suspension, billing mode (D68). It keeps what is evidence: usage events, the credit ledger, settled statements and the audit log. The default account
// cannot be deleted. Callers must have dealt with the sandboxes and volumes first.
func (t *Tenancy) AccountDelete(account string) (map[string]int64, error) {
	if account == DefaultAccount {
		return nil, errors.New("the default account cannot be deleted")
	}
	if _, err := t.Account(account); err != nil {
		return nil, err
	}
	out := map[string]int64{}
	run := func(name, q string, args ...any) error {
		res, err := t.db.Exec(q, args...)
		if err != nil {
			return err
		}
		n, _ := res.RowsAffected()
		out[name] = n
		return nil
	}
	steps := []struct{ name, q string }{
		{"quotas", `DELETE FROM quotas WHERE project IN (SELECT id FROM projects WHERE account = $1)`},
		{"project_networks", `DELETE FROM project_network WHERE project IN (SELECT id FROM projects WHERE account = $1)`},
		{"api_keys", `DELETE FROM api_keys WHERE account = $1`},
		{"members", `DELETE FROM members WHERE account = $1`},
		{"projects", `DELETE FROM projects WHERE account = $1`},
		{"suspensions", `DELETE FROM suspensions WHERE account = $1`},
		{"billing", `DELETE FROM account_billing WHERE account = $1`},
		{"accounts", `DELETE FROM accounts WHERE id = $1`},
	}
	for _, s := range steps {
		if err := run(s.name, s.q, account); err != nil {
			return out, err
		}
	}
	// the name stays reserved: usage events and the credit ledger are keyed by it, and whoever registered it next would inherit them
	_, _ = t.db.Exec(`INSERT INTO account_tombstones(id, deleted) VALUES ($1, $2) ON CONFLICT(id) DO NOTHING`, account, now())
	t.susp.drop()
	return out, nil
}
