"""eval/harvest.py: collecting papers and open-source harness changes without a model. No network: listings and the GitHub API are fixtures."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "eval"))
import harvest as H  # noqa: E402

FIX = open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "arxiv_new_listing.html"), encoding="utf-8").read()


def test_the_new_submissions_of_a_listing_page_become_items_with_title_and_abstract():
    items = H.parse_arxiv_listing(FIX)
    assert len(items) == 3 and all(i["id"].startswith("arxiv:26") and i["title"] and len(i["text"]) > 50 and i["url"].startswith("https://arxiv.org/abs/") for i in items)
    assert "Title:" not in items[0]["title"]


def test_what_matters_to_a_coding_agent_harness_scores_and_the_rest_does_not():
    s, m = H.score("Agentic harness engineering: context compaction and middleware for coding agents on Terminal-Bench")
    assert s >= 12 and any("harness" in x for x in m)
    assert H.score("A study of binary reverse engineering with transformers")[0] == 0


def test_a_listing_is_deduplicated_across_categories_and_a_failing_category_does_not_stop_the_rest():
    calls = []
    def get(url):
        calls.append(url)
        if "cs.MA" in url: raise OSError("boom")
        return FIX
    items = H.papers(get=get, listings=["cs.SE", "cs.AI", "cs.MA"])
    assert len(items) == 3 and len(calls) == 3                                        # the same three papers listed in two categories are three items


def gh(repo_files):
    def get(url):
        if "/releases" in url: return json.dumps([{"tag_name": "v1.2", "name": "1.2", "body": "Adds context compaction middleware and a progress reminder.", "html_url": "u", "published_at": "2026-10-30"}])
        if "/commits?" in url: return json.dumps([{"sha": "a" * 40, "parents": [1], "html_url": "u1", "commit": {"message": "compact old tool output\n\nbody", "author": {"date": "2026-10-29"}}},
                                                    {"sha": "b" * 40, "parents": [1], "html_url": "u2", "commit": {"message": "fix typo", "author": {"date": "2026-10-29"}}},
                                                    {"sha": "c" * 40, "parents": [1, 2], "html_url": "u3", "commit": {"message": "Merge branch", "author": {"date": "2026-10-29"}}}])
        sha = url.rsplit("/", 1)[1]
        return json.dumps({"files": [{"filename": f, "additions": 5, "deletions": 1} for f in repo_files[sha[0]]]})
    return get


def test_commits_that_touch_prompts_tools_or_context_handling_are_kept_and_the_rest_and_merges_are_not():
    items = H.github_items("o/r", "2026-10-01T00:00:00Z", get=gh({"a": ["src/agent/context_compaction.py", "tests/test_x.py"], "b": ["README.md", "docs/a.md"]}))
    kinds = sorted((i["kind"], i["id"].split("@")[1][:3]) for i in items)
    assert kinds == [("commit", "aaa"), ("release", "v1.")]                           # the typo fix touches nothing relevant; the merge is skipped
    c = next(i for i in items if i["kind"] == "commit"); assert c["files"] == ["src/agent/context_compaction.py"] and c["score"] >= 4 and "compact old tool output" in c["title"]


def test_a_run_writes_new_items_once_a_digest_and_does_not_repeat_them_the_next_time(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr(H, "repos", lambda st, get=None, repo_list=None: H.github_items("o/r", "", get=gh({"a": ["src/agent/context_compaction.py"], "b": ["x.py"]})))
    first = H.run("repos", d, min_score=3); second = H.run("repos", d, min_score=3)
    assert len(first) >= 1 and second == [] and os.path.exists(os.path.join(d, "items.jsonl"))
    assert any(f.startswith("digest-") for f in os.listdir(d)) and len(open(os.path.join(d, "items.jsonl")).read().splitlines()) == len(first)
