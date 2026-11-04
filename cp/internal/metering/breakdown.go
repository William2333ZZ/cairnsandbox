package metering

import (
	"fmt"
	"sort"
)

// BreakdownRow is one line of a usage breakdown: what a sandbox (or a UTC day) cost, split by kind.
type BreakdownRow struct {
	Key     string             `json:"key"` // sandbox id, or the day's first second (unix) as text
	CostUSD float64            `json:"cost_usd"`
	Kinds   map[string]float64 `json:"kinds"`
}

// Breakdown (D66): the project's cost between from and to (to 0 = now) grouped by sandbox (the top `limit` by cost; the rest are summed into "other") or by UTC day
// (oldest first, empty days left out; the caller fills the gaps). The day bucket is integer division on the timestamp, which both SQLite and Postgres do the same way.
func (m *Metering) Breakdown(project, by string, from, to float64, limit int) ([]BreakdownRow, error) {
	if to == 0 {
		to = now()
	}
	var q string
	switch by {
	case "sandbox":
		q = `SELECT sandbox, kind, sum(cost_usd) FROM usage_events WHERE project = $1 AND ts >= $2 AND ts < $3 GROUP BY sandbox, kind`
	case "day":
		q = `SELECT (CAST(ts AS BIGINT) / 86400) * 86400, kind, sum(cost_usd) FROM usage_events WHERE project = $1 AND ts >= $2 AND ts < $3 GROUP BY (CAST(ts AS BIGINT) / 86400) * 86400, kind`
	default:
		return nil, fmt.Errorf("by: sandbox or day")
	}
	rows, err := m.db.Query(q, project, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	acc := map[string]*BreakdownRow{}
	for rows.Next() {
		var key, kind string
		var cost float64
		if by == "day" {
			var d int64
			if err := rows.Scan(&d, &kind, &cost); err != nil {
				return nil, err
			}
			key = fmt.Sprint(d)
		} else if err := rows.Scan(&key, &kind, &cost); err != nil {
			return nil, err
		}
		r := acc[key]
		if r == nil {
			r = &BreakdownRow{Key: key, Kinds: map[string]float64{}}
			acc[key] = r
		}
		r.Kinds[kind] = round6(r.Kinds[kind] + cost)
		r.CostUSD = round6(r.CostUSD + cost)
	}
	out := make([]BreakdownRow, 0, len(acc))
	for _, r := range acc {
		out = append(out, *r)
	}
	if by == "day" {
		sort.Slice(out, func(i, j int) bool {
			return len(out[i].Key) < len(out[j].Key) || (len(out[i].Key) == len(out[j].Key) && out[i].Key < out[j].Key)
		})
		return out, nil
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CostUSD > out[j].CostUSD })
	if limit > 0 && len(out) > limit {
		rest := BreakdownRow{Key: "other", Kinds: map[string]float64{}}
		for _, r := range out[limit:] {
			rest.CostUSD = round6(rest.CostUSD + r.CostUSD)
			for k, v := range r.Kinds {
				rest.Kinds[k] = round6(rest.Kinds[k] + v)
			}
		}
		out = append(out[:limit], rest)
	}
	return out, nil
}
