"""dsh-superagent 004: the team's own words, versioned, advice only."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from test_intake_engine import engine, wait, I  # noqa: E402
from aios import run as R  # noqa: E402


def test_a_section_starts_as_the_built_in_text_and_a_save_is_a_new_version_that_can_be_reset():
    e = engine(); v = e.prompts_view()["sections"]["communication"]
    assert not v["overridden"] and v["version"] == 0 and v["text"] == v["default"] and e.prompt_overrides() == {}
    v = e.prompts_save("communication", "Write in Chinese, two sentences at most.", base=0)["sections"]["communication"]
    assert v["overridden"] and v["version"] == 1 and v["text"].startswith("Write in Chinese") and e.prompt_overrides() == {"communication": "Write in Chinese, two sentences at most."}
    e.prompts_save("scope", "Never touch the CI files.", base=0); assert set(e.prompt_overrides()) == {"communication", "scope"}
    v = e.prompts_reset("communication")["sections"]["communication"]
    assert not v["overridden"] and v["version"] == 2 and [h["reset"] for h in v["history"]] == [False, True] and "communication" not in e.prompt_overrides()


def test_a_save_on_top_of_someone_elses_is_refused_and_so_are_unknown_sections_and_too_much_text():
    e = engine(); e.prompts_save("delivery", "Open a PR with the test output.", base=0)
    for args, why in ((("delivery", "x", 0), "conflict"), (("nope", "x", None), "unknown section"), (("scope", "y" * 4000, None), "too long")):
        try: e.prompts_save(*args); raise AssertionError("accepted")
        except ValueError as err: assert why in str(err), err
    assert e.prompts_view()["sections"]["delivery"]["version"] == 1                                       # a refused save changed nothing
    e.prompts_save("scope", "a" * 3000); e.prompts_save("communication", "b" * 3000)
    try: e.prompts_save("delivery", "c" * 3000); raise AssertionError("accepted")
    except ValueError as err: assert "together" in str(err)


def test_a_text_identical_to_the_built_in_one_is_no_override():
    e = engine(); default = e.prompts_view()["sections"]["communication"]["default"]
    assert not e.prompts_save("communication", default)["sections"]["communication"]["overridden"] and e.prompt_overrides() == {}


def test_the_team_words_reach_the_agents_ticket_and_the_built_in_text_stays_when_there_is_none():
    base = {"text": "do it", "cwd": "/w", "branch": "b"}
    assert R.COMMUNICATION_DEFAULT in R.ticket_text(base) and "Team rules" not in R.ticket_text(base)
    t = R.ticket_text({**base, "prompts": {"communication": "Reply in Chinese.", "scope": "Stay inside pkg/.", "delivery": "Attach the test output."}})
    assert "Reply in Chinese." in t and R.COMMUNICATION_DEFAULT not in t and "Team rules for scope: Stay inside pkg/." in t and "Team rules for delivery: Attach the test output." in t
    assert "APPROVE: send|budget|routine" in t and "QUESTION:" in t                                      # what the host depends on is not in the team's reach


def test_a_default_that_moves_under_an_override_is_marked(monkeypatch):
    import prompts as P
    e = engine(); e.prompts_save("communication", "Short.", base=0)
    monkeypatch.setitem(P.SECTIONS, "communication", ("沟通", "A different built-in text."))
    assert e.prompts_view()["sections"]["communication"]["default_updated"] is True
