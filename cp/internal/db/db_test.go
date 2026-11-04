package db

import "testing"

func TestSQLiteOpensAndMigratesTwice(t *testing.T) {
	d, err := Open("", t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer d.Close()
	if err := d.Migrate(); err != nil {
		t.Fatal(err)
	}
	if err := d.Migrate(); err != nil {
		t.Fatal("second migrate must be a no-op:", err)
	}
	if _, err := d.Exec(`INSERT INTO accounts(id, name, created) VALUES ($1, $2, $3)`, "a", "A", 1.0); err != nil {
		t.Fatal("$1 placeholders must work on sqlite too:", err)
	}
	var n int
	if err := d.QueryRow(`SELECT count(*) FROM accounts WHERE id = $1`, "a").Scan(&n); err != nil || n != 1 {
		t.Fatal(n, err)
	}
	if _, err := Open("mysql://x", t.TempDir()); err == nil {
		t.Fatal("unsupported spec accepted")
	}
	if d, err := Open("postgres://u:p@h:5432/x", t.TempDir()); err != nil || d.Where != "postgres://…@h:5432/x" {
		t.Fatal(d, err)
	}
}
