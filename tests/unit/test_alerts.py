"""deploy/alerts.yml may only name metrics the control plane really exposes (cp/internal/httpapi/server.go metrics()), and every alert says what to do."""
import os, re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _exposed():
    src = open(os.path.join(ROOT, "cp", "internal", "httpapi", "server.go"), encoding="utf-8").read()
    body = src[src.index("func (s *Server) metrics("):]
    return set(re.findall(r"(?:\\n|\b)(cairn_[a-z0-9_]+)", body))   # a metric printed after a \n escape in the same Fprintf has no word boundary before it


def test_every_metric_in_the_alert_rules_is_exposed():
    rules = open(os.path.join(ROOT, "deploy", "alerts.yml"), encoding="utf-8").read()
    used = set(re.findall(r"\b(cairn_[a-z0-9_]+)", re.sub(r"(?m)^\s*#.*$", "", rules)))
    # annotations may mention names like aios-cp (job) or doc anchors; the underscore form is a metric
    missing = sorted(m for m in used if m not in _exposed())
    assert not missing, f"alert rules name metrics that /metrics does not expose: {missing}"


def test_every_alert_has_a_severity_a_duration_and_an_action():
    rules = open(os.path.join(ROOT, "deploy", "alerts.yml"), encoding="utf-8").read()
    blocks = re.split(r"(?m)^\s*- alert: ", rules)[1:]
    assert len(blocks) >= 10
    for b in blocks:
        name = b.split("\n", 1)[0].strip()
        assert "severity:" in b and "for:" in b and "action:" in b and "expr:" in b, f"{name} is incomplete"


def test_the_grafana_dashboard_only_uses_exposed_metrics_and_is_valid_json():
    import json
    d = json.load(open(os.path.join(ROOT, "deploy", "grafana", "cairn-overview.json"), encoding="utf-8"))
    exprs = [t["expr"] for p in d["panels"] for t in p["targets"]]
    assert len(d["panels"]) >= 10 and len({p["id"] for p in d["panels"]}) == len(d["panels"])
    missing = sorted({m for e in exprs for m in re.findall(r"\b(cairn_[a-z0-9_]+)", e)} - _exposed())
    assert not missing, f"the dashboard queries metrics that /metrics does not expose: {missing}"
