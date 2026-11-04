package tenancy

import (
	"errors"

	"fcnode/cp/internal/registry"
)

// Invite (D60): a one-time code that lets someone create their own account. It carries what the new account starts with, so a closed beta can onboard
// people without the deployer doing it by hand, and nobody can sign up without one.
type Invite struct {
	ID           string  `json:"id"`
	Note         string  `json:"note"`
	CreditUSD    float64 `json:"credit_usd"`
	MaxSandboxes int64   `json:"max_sandboxes"`
	DailyUSD     float64 `json:"daily_usd"`
	Created      float64 `json:"created"`
	Expires      float64 `json:"expires"`
	UsedBy       string  `json:"used_by"`
	UsedAt       float64 `json:"used_at"`
	CreatedBy    string  `json:"created_by"`
}

var ErrInvite = errors.New("invite code is not valid")

// InviteAdd returns the code once; only its hash is kept.
func (t *Tenancy) InviteAdd(in Invite, ttlS float64, by string) (map[string]any, error) {
	if in.CreditUSD < 0 || in.MaxSandboxes < 0 || in.DailyUSD < 0 {
		return nil, errors.New("credit_usd, max_sandboxes, daily_usd: zero or positive")
	}
	code := "inv_" + registry.NewToken() + registry.NewToken()[:8]
	in.ID, in.Created, in.CreatedBy = "iv-"+registry.NewToken()[:8], now(), by
	if ttlS > 0 {
		in.Expires = in.Created + ttlS
	}
	if _, err := t.db.Exec(`INSERT INTO invites(code_sha256, id, note, credit_usd, max_sandboxes, daily_usd, created, expires, created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
		sha(code), in.ID, in.Note, in.CreditUSD, in.MaxSandboxes, in.DailyUSD, in.Created, in.Expires, in.CreatedBy); err != nil {
		return nil, err
	}
	return map[string]any{"invite": in, "code": code}, nil
}

func (t *Tenancy) Invites() ([]Invite, error) {
	rows, err := t.db.Query(`SELECT id, note, credit_usd, max_sandboxes, daily_usd, created, expires, used_by, used_at, created_by FROM invites ORDER BY created DESC LIMIT 200`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Invite{}
	for rows.Next() {
		var v Invite
		if err := rows.Scan(&v.ID, &v.Note, &v.CreditUSD, &v.MaxSandboxes, &v.DailyUSD, &v.Created, &v.Expires, &v.UsedBy, &v.UsedAt, &v.CreatedBy); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, nil
}

// Signup claims the code, then creates the account and its owner. The claim is one UPDATE (two people racing for one code: one wins); if the names are
// taken the claim is released so the person can try again with another name. Returns the invite (for its starting credit / quota) and the owner's token.
func (t *Tenancy) Signup(code, account, user, name string) (Invite, map[string]any, error) {
	var v Invite
	res, err := t.db.Exec(`UPDATE invites SET used_by = $1, used_at = $2 WHERE code_sha256 = $3 AND used_by = '' AND (expires = 0 OR expires > $2)`, user, now(), sha(code))
	if err != nil {
		return v, nil, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return v, nil, ErrInvite
	}
	release := func() {
		_, _ = t.db.Exec(`UPDATE invites SET used_by = '', used_at = 0 WHERE code_sha256 = $1`, sha(code))
	}
	if err := t.db.QueryRow(`SELECT id, note, credit_usd, max_sandboxes, daily_usd FROM invites WHERE code_sha256 = $1`, sha(code)).Scan(&v.ID, &v.Note, &v.CreditUSD, &v.MaxSandboxes, &v.DailyUSD); err != nil {
		release()
		return v, nil, err
	}
	if _, err := t.AccountAdd(account, name); err != nil {
		release()
		return v, nil, err
	}
	tok, err := t.MemberAdd(account, user, name, RoleOwner)
	if err != nil {
		release()
		_, _ = t.db.Exec(`DELETE FROM projects WHERE account = $1`, account)
		_, _ = t.db.Exec(`DELETE FROM accounts WHERE id = $1`, account)
		return v, nil, err
	}
	return v, tok, nil
}
