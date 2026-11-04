import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "node"))
import egress_proxy as ep


def setup_function(_):
    ep.POLICY.clear()


def test_unknown_ip_is_denied():
    assert ep.allowed("10.200.0.2", "mirrors.aliyun.com") == (False, None)


def test_exact_and_wildcard_hosts():
    ep.set_policy("10.200.0.2", "sb-1", ["mirrors.aliyun.com", "*.example.com"])
    assert ep.allowed("10.200.0.2", "mirrors.aliyun.com") == (True, "sb-1")
    assert ep.allowed("10.200.0.2", "MIRRORS.ALIYUN.COM")[0] is True          # case-insensitive
    assert ep.allowed("10.200.0.2", "api.example.com")[0] is True
    assert ep.allowed("10.200.0.2", "example.com")[0] is False               # wildcard needs a subdomain
    assert ep.allowed("10.200.0.2", "pypi.org")[0] is False


def test_empty_list_denies_and_star_allows_all():
    ep.set_policy("10.200.0.6", "sb-2", [])
    assert ep.allowed("10.200.0.6", "anything")[0] is False
    ep.set_policy("10.200.0.6", "sb-2", ["*"])
    assert ep.allowed("10.200.0.6", "anything")[0] is True


def test_clear_policy():
    ep.set_policy("10.200.0.10", "sb-3", ["*"]); ep.clear_policy("10.200.0.10")
    assert ep.allowed("10.200.0.10", "x")[0] is False


def test_regex_hosts_match_the_whole_name_and_bad_regexes_are_refused():
    import pytest
    rules = ep.validate_rules([{"name": "gh", "match": {"host": "~(api|raw)\\.github\\.com"}, "action": {"allow": True}}])
    ep.set_policy("10.200.0.14", "sb-r", [], rules=rules)
    assert ep.decide("10.200.0.14", "https", "api.github.com", "CONNECT", "")[0] is True
    assert ep.decide("10.200.0.14", "https", "RAW.GitHub.com", "CONNECT", "")[0] is True        # case-insensitive
    assert ep.decide("10.200.0.14", "https", "evil-api.github.com", "CONNECT", "")[0] is False  # fullmatch, not search
    assert ep.decide("10.200.0.14", "https", "api.github.com.evil", "CONNECT", "")[0] is False
    with pytest.raises(ValueError): ep.validate_rules([{"name": "bad", "match": {"host": "~("}, "action": {"allow": True}}])


def test_limits_refuse_past_the_request_rate_or_the_byte_budget():
    import pytest
    assert ep.validate_limits({}) == {"requests_per_min": 0, "max_bytes": 0}
    assert ep.validate_limits({"requests_per_min": 2, "max_bytes": 100}) == {"requests_per_min": 2, "max_bytes": 100}
    for bad in ({"requests_per_min": -1}, {"max_bytes": 1.5}, {"nope": 1}, {"requests_per_min": True}):
        with pytest.raises(ValueError): ep.validate_limits(bad)
    ep.set_policy("10.200.0.20", "sb-l", ["*"], limits={"requests_per_min": 2, "max_bytes": 100})
    assert ep.limited("10.200.0.20", t=1000) is None and ep.limited("10.200.0.20", t=1001) is None
    assert "requests per minute" in ep.limited("10.200.0.20", t=1002)                          # third within the minute
    assert ep.limited("10.200.0.20", t=1061) is None                                            # the window moved on
    ep._count("10.200.0.20", "in", 60); ep._count("10.200.0.20", "out", 40)
    assert "byte budget" in ep.limited("10.200.0.20", t=1062)
    ep.set_policy("10.200.0.21", "sb-n", ["*"])                                                  # no limits: never limited
    assert all(ep.limited("10.200.0.21", t=2000 + i) is None for i in range(50))
    ep.clear_policy("10.200.0.20"); assert "10.200.0.20" not in ep._REQS


def test_the_denial_says_which_host_and_how_to_ask_and_cannot_break_the_status_line():
    m = ep.deny_message("sb-1234abcd", "goproxy.cn")
    assert "goproxy.cn" in m and "PUT /sandboxes/sb-1234abcd/network" in m and '"allow_hosts":["goproxy.cn"]' in m
    evil = ep.deny_message("sb-1\r\nX: y", "a.com\r\nSet-Cookie: x")
    assert "\r" not in evil and "\n" not in evil and ep.deny_message(None, None).count("<id>") == 1
