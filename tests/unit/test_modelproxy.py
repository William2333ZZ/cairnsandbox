"""The model proxy prices what the provider really charges (docs/007 效率): tokens served from the provider's prefix cache cost a fraction of a miss.
An agent loop resends its whole history every step, so most input is cache hits; charging all of it at the miss price made every run look many times dearer
than it was and stopped runs at a cost cap long before the work was done."""
import json, os, sys, tempfile, threading, urllib.request
os.environ["PROXY_STATE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "node"))
import modelproxy as mp  # noqa: E402


def test_a_cache_hit_costs_a_fraction_of_a_miss():
    full = mp.price("deepseek-flash", 1_000_000, 0)
    cached = mp.price("deepseek-flash", 1_000_000, 0, cached=1_000_000)
    assert abs(full - 0.30) < 1e-9 and abs(cached - 0.006) < 1e-9
    assert abs(mp.price("deepseek-flash", 1_000_000, 100_000, cached=900_000) - (0.1 * 0.30 + 0.9 * 0.006 + 0.1 * 1.2)) < 1e-9


def test_bad_or_absurd_cache_numbers_never_make_a_call_cost_less_than_nothing_or_more_than_a_miss():
    assert mp.price("deepseek-flash", 1000, 0, cached=5000) == mp.price("deepseek-flash", 1000, 0, cached=1000)      # more hits than tokens: clamp
    assert mp.price("deepseek-flash", 1000, 0, cached=-5) == mp.price("deepseek-flash", 1000, 0)
    assert mp.price("nobody", 1000, 10, cached=10) is None                                                            # unknown model stays unpriced


def test_a_price_without_a_hit_price_charges_every_input_token_the_same():
    mp.PRICES["flat"] = [1.0, 2.0]
    assert mp.price("flat", 1_000_000, 0, cached=1_000_000) == 1.0


def test_both_providers_ways_of_reporting_cache_hits_are_read():
    assert mp.cached_tokens({"prompt_cache_hit_tokens": 700, "prompt_cache_miss_tokens": 300}) == 700
    assert mp.cached_tokens({"prompt_tokens_details": {"cached_tokens": 40}}) == 40
    assert mp.cached_tokens({}) == 0 and mp.cached_tokens({"prompt_cache_hit_tokens": "x"}) == 0 and mp.cached_tokens(None) == 0


def test_a_call_through_the_proxy_is_metered_with_its_cache_hits_and_reports_them_in_a_header():
    from http.server import ThreadingHTTPServer
    mp.BACKEND, mp.UPSTREAM = "upstream", "http://unused"
    mp.call_upstream = lambda body, timeout=300, account=None: (200, {"choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
                                                        "usage": {"prompt_tokens": 100_000, "completion_tokens": 1000, "prompt_cache_hit_tokens": 90_000}})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mp.H); srv.daemon_threads = True; threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    def post(path, body, token=None):
        r = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        return urllib.request.urlopen(r)
    tok = json.loads(post("/tokens", {"sandbox": "sb-t", "budget_usd": 0.05}).read())["token"]
    for _ in range(3):                                                    # three calls of 100k tokens: at the miss price 3 x $0.031 would exhaust a $0.05 budget after two
        r = post("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, tok)
        assert r.headers["X-Tokens-Cached"] == "90000"
    u = json.loads(urllib.request.urlopen(base + "/usage/sb-t").read())
    expect = 3 * (10_000 * 0.30 + 90_000 * 0.006 + 1000 * 1.20) / 1e6
    assert u["tokens_cached"] == 270_000 and u["calls"] == 3 and abs(u["cost_usd"] - round(expect, 6)) < 1e-6 and u["refused"] == 0
    srv.shutdown()


def test_a_provider_that_leaves_the_usage_out_does_not_make_a_call_free_and_an_unpriced_model_is_refused():
    body = {"messages": [{"role": "user", "content": "x" * 300}]}
    usage, tin, tout, cached = mp.usage_of_response(body, {"choices": [{"message": {"content": "y" * 90}}]})
    assert tin >= 100 and tout > 0 and cached == 0                                      # estimated, over rather than under
    usage, tin, tout, cached = mp.usage_of_response(body, {"usage": {"prompt_tokens": None, "completion_tokens": "x", "prompt_cache_hit_tokens": 5}, "choices": []})
    assert tin >= 100 and cached == 5
    assert mp.usage_of_response(body, {"usage": {"prompt_tokens": 7, "completion_tokens": 3}})[1:3] == (7, 3)


def test_each_account_pays_with_its_own_key_when_it_has_one_and_with_the_shared_key_when_it_does_not():
    import tempfile
    d = tempfile.mkdtemp(); open(os.path.join(d, "eval.key"), "w").write("sk-eval\n")
    mp.UPSTREAM_KEY = "sk-shared"; mp.KEY_FILES = {"eval": os.path.join(d, "eval.key"), "ghost": os.path.join(d, "missing.key")}
    assert mp.account_key("eval") == "sk-eval" and mp.account_key("live") == "sk-shared" and mp.account_key(None) == "sk-shared"
    assert mp.account_key("ghost") == "sk-shared"                                                                  # a configured key that cannot be read does not break the calls
    v = mp.accounts_view(); assert v["eval"] == "own key" and v["live"] == "shared key" and v["ghost"] == "shared key"
    seen = []
    mp.BACKEND, mp.UPSTREAM = "upstream", "http://unused"
    from http.server import ThreadingHTTPServer
    import threading, json as _j, urllib.request as _u
    def fake(body, timeout=300, account=None): seen.append(account); return 200, {"choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
    mp.call_upstream = fake
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mp.H); srv.daemon_threads = True; threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    def post(path, body, tok=None):
        r = _u.Request(base + path, data=_j.dumps(body).encode(), headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {})}); return _j.loads(_u.urlopen(r).read())
    for acct, want in (("eval", "eval"), (None, "live")):
        tok = post("/tokens", {"sandbox": f"sb-{want}", "budget_usd": 1.0, **({"account": acct} if acct else {})})["token"]
        post("/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]}, tok)
    assert seen == ["eval", "live"]
    srv.shutdown()
