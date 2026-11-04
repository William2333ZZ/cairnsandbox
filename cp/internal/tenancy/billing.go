package tenancy

import "errors"

// Billing modes (D59): postpaid is what every account was until now (spend is reported, nothing stops); prepaid stops creating at zero credit and suspends the account.
const (
	Postpaid = "postpaid"
	Prepaid  = "prepaid"
)

func (t *Tenancy) BillingMode(account string) string {
	var m string
	if err := t.db.QueryRow(`SELECT mode FROM account_billing WHERE account = $1`, account).Scan(&m); err != nil || m != Prepaid {
		return Postpaid
	}
	return m
}

func (t *Tenancy) SetBillingMode(account, mode string) error {
	if mode != Prepaid && mode != Postpaid {
		return errors.New("mode: prepaid or postpaid")
	}
	if _, err := t.Account(account); err != nil {
		return err
	}
	_, err := t.db.Exec(`INSERT INTO account_billing(account, mode) VALUES ($1, $2) ON CONFLICT(account) DO UPDATE SET mode = EXCLUDED.mode`, account, mode)
	return err
}

// PrepaidAccounts: the accounts the credit watcher looks at.
func (t *Tenancy) PrepaidAccounts() []string {
	rows, err := t.db.Query(`SELECT account FROM account_billing WHERE mode = 'prepaid'`)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var a string
		if rows.Scan(&a) == nil {
			out = append(out, a)
		}
	}
	return out
}
