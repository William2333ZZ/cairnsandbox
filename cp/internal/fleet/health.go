package fleet

import (
	"fmt"
	"os"
	"strings"
	"time"
)

// Poller is what the health loop needs from the node client (nodeclient satisfies it; fleet does not import it to keep the direction fleet <- nodeclient).
type Poller interface {
	Health(n *Node) (map[string]any, error)
}

// PollOnce is one pass over every node: sync registrations, poll health, fire the callbacks.
func (f *Fleet) PollOnce(p Poller) {
	f.Sync()
	for _, n := range f.All() {
		h, err := p.Health(n)
		if err != nil {
			msg := truncate(err.Error(), 120)
			if n.SetDown(msg) {
				f.audit.Write(map[string]any{"op": "node_down", "node": n.Name, "error": msg})
			}
			continue
		}
		was, polls := n.SetUp(h)
		if !was {
			f.audit.Write(map[string]any{"op": "node_up", "node": n.Name})
		}
		for _, cb := range f.onUp {
			cb(n, was, polls)
		}
	}
}

func (f *Fleet) Loop(p Poller, every time.Duration) {
	for {
		f.PollOnce(p)
		time.Sleep(every)
	}
}

func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

func readFile(p string) ([]byte, error) { return os.ReadFile(p) }
func trim(b []byte) string              { return strings.TrimSpace(string(b)) }
func trimRight(s, cut string) string    { return strings.TrimRight(s, cut) }

var _ = fmt.Sprint
