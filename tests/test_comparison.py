"""Comparison integrity and the generated report.

The comparison is only worth reading if each column is what that agent really received and
did. These tests check the recorded inputs against what the planner was handed, check that the
two agents are offered the same tools and rules, and check the HTML for honesty and safety.
"""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.helpers import (
    RecordingPlanner,
    ScriptedPlanner,
    install_planner,
    read_jsonl,
    run_session,
)
from workshop.models import AskUser, CallTool, PlanDraft, PresentPlan
from workshop.prompts import ONE_SHOT_INSTRUCTIONS, TASK_INSTRUCTIONS
from workshop.report import render_comparison, run_summary
from workshop.tools import AGENT_TOOLS, ONE_SHOT_TOOLS, TOOL_SPECS

HOSTILE = '<script>alert("pwned")</script><img src=x onerror=alert(1)>&amp;'


# ---------------------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------------------


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str, str | None]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend((tag, k, v) for k, v in attrs)

    def handle_data(self, data):
        self.text.append(data)


def parse(page: str) -> Tags:
    p = Tags()
    p.feed(page)
    return p


def text_of(page: str) -> str:
    return re.sub(r"\s+", " ", "".join(parse(page).text))


def report_html(runs, name: str) -> str:
    return (runs.service.path(name) / "report.html").read_text()


# ---------------------------------------------------------------------------------------
# The transcript agent receives everything
# ---------------------------------------------------------------------------------------


def test_every_transcript_input_is_the_full_retained_history_so_far(runs):
    name = runs.transcript
    transcript = runs.service.transcript(name)
    calls = runs.service.records(name, "model_calls")
    sizes = []
    for i, call in enumerate(calls):
        messages = call["input"]["messages"]
        assert messages == transcript[: len(messages)], f"call {i} is not a prefix of the retained transcript"
        sizes.append(len(messages))
        # Every prior user message, notification, and tool result is in this input.
        for entry in transcript:
            if entry["role"] in ("user", "notification") and entry["turn"] <= call["turn"] and (entry["normalized"] or {}).get("display"):
                assert entry in messages, (i, entry["turn"], entry["role"])
        for earlier in calls[:i]:
            ids = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
            assert earlier["action_call_id"] in ids, f"call {i} lacks the result of {earlier['action_call_id']}"
            assistant = [m for m in messages if m["role"] == "assistant" and m["tool_call"] and m["tool_call"]["id"] == earlier["action_call_id"]]
            assert len(assistant) == 1, "and the assistant message that asked for it"
    assert sizes == sorted(sizes) and len(set(sizes)) == len(sizes), "history grows on every call and is never truncated"


def test_the_transcript_agent_sees_the_expiry_notification_and_the_resolved_changes(runs):
    calls = [c for c in runs.service.records(runs.transcript, "model_calls") if c["turn"] == 3]
    assert calls
    for call in calls:
        notes = [m for m in call["input"]["messages"] if m["role"] == "notification"]
        assert len(notes) == 1 and "has expired" in notes[0]["content"]
        assert '"quote_id": "quote-h17-r1-2a1c"' in notes[0]["content"], "the application resolved the reference to an explicit quote"
    turn2 = next(c for c in runs.service.records(runs.transcript, "model_calls") if c["turn"] == 2)
    user = [m for m in turn2["input"]["messages"] if m["role"] == "user" and m["turn"] == 2][0]
    assert '"hotel_id": "h17"' in user["content"] and '"reference_arrival_local": "18:00"' in user["content"]


def test_the_transcript_agent_has_no_hidden_structured_memory(runs):
    path = runs.service.path(runs.transcript)
    assert not (path / "state_snapshots.jsonl").exists()
    assert runs.service.state(runs.transcript)["available"] is False
    assert runs.service.diff(runs.transcript)["available"] is False
    assert all(c["input"]["context"] is None for c in runs.service.records(runs.transcript, "model_calls"))
    assert json.loads((path / "manifest.json").read_text())["strategy"] is None


def test_the_state_agent_receives_one_built_message_not_the_history(runs):
    calls = runs.service.records(runs.state, "model_calls")
    assert all(len(c["input"]["messages"]) == 1 and c["input"]["context"] for c in calls)
    assert (runs.service.path(runs.state) / "state_snapshots.jsonl").exists()
    assert len(runs.service.transcript(runs.state)) > len(calls[-1]["input"]["messages"]), "the transcript is retained, just not sent"


# ---------------------------------------------------------------------------------------
# Same rules, tools, world, and settings
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name_attr", ["transcript", "state"])
def test_both_agents_get_the_same_tools_and_task_instructions_on_every_call(runs, name_attr):
    expected_tools = [TOOL_SPECS[n].model_dump(mode="json") for n in AGENT_TOOLS]
    calls = runs.service.records(getattr(runs, name_attr), "model_calls")
    assert calls
    for call in calls:
        assert call["input"]["tools"] == expected_tools
        assert call["input"]["system"] == TASK_INSTRUCTIONS


def test_the_one_shot_prompt_shares_the_task_rules_and_the_tool_definitions(runs):
    def rules(text: str) -> str:
        return text[text.index("Task rules"): text.index("How to respond")]

    call = runs.service.records(runs.one_shot, "model_calls")[0]["input"]
    assert rules(call["system"]) == rules(TASK_INSTRUCTIONS) == rules(ONE_SHOT_INSTRUCTIONS)
    assert [t["name"] for t in call["tools"]] == ONE_SHOT_TOOLS
    assert call["tools"] == [TOOL_SPECS[n].model_dump(mode="json") for n in ONE_SHOT_TOOLS], "same definitions, fewer tools"


def test_both_agents_see_the_same_world_and_run_with_the_same_settings(runs):
    results: dict[str, dict[str, object]] = {}
    for key in ("transcript", "state"):
        for t in runs.service.records(getattr(runs, key), "tool_calls"):
            if t["name"] in ("search_flights", "search_hotels", "refresh_quote") and t["result"].get("ok"):
                normalized = json.loads(json.dumps(t["result"]))
                normalized.pop("observed_at", None)
                for q in normalized.get("quotes", []):
                    # The agents ask at different moments, so issue and expiry times legitimately differ.
                    for volatile in ("tool_call_id", "observed_at", "expires_at"):
                        q.pop(volatile, None)
                results.setdefault(f"{t['name']}:{json.dumps(t['arguments'], sort_keys=True)}", {})[key] = normalized
    shared = {k: v for k, v in results.items() if len(v) == 2}
    assert len(shared) >= 5
    for key, pair in shared.items():
        assert pair["transcript"] == pair["state"], key

    manifests = {k: json.loads((runs.service.path(getattr(runs, k)) / "manifest.json").read_text()) for k in ("transcript", "state")}
    for field in ("planner_mode", "model_settings", "fixture_revision", "clock", "scenario", "limits", "provider"):
        assert manifests["transcript"][field] == manifests["state"][field], field
    first = {k: runs.service.records(getattr(runs, k), "model_calls")[0]["settings"] for k in ("transcript", "state")}
    assert first["transcript"] == first["state"]


# ---------------------------------------------------------------------------------------
# Recorded executions match what actually happened
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("implementation", ["transcript", "state", "one-shot"])
def test_every_recorded_model_input_is_what_the_planner_received(service, monkeypatch, implementation):
    planner = install_planner(monkeypatch, RecordingPlanner())
    run_session(service, "rec", implementation, restated_brief=implementation == "one-shot")
    calls = service.records("rec", "model_calls")
    assert len(calls) == len(planner.inputs) == len(planner.responses) >= 4
    for call, received, response in zip(calls, planner.inputs, planner.responses):
        assert call["input"] == received.model_dump(mode="json")
        assert call["action"] == response.action.model_dump(mode="json") and call["action_call_id"] == response.action_call_id
        assert call["input_chars"] == len(received.system) + sum(len(m.content) for m in received.messages)
        assert call["usage"] is None and call["latency_ms"] is None


@pytest.mark.parametrize("implementation", ["transcript", "state"])
def test_every_displayed_call_traces_back_to_a_model_action(service, monkeypatch, implementation):
    install_planner(monkeypatch, RecordingPlanner())
    run_session(service, "trace", implementation)
    calls = service.records("trace", "model_calls")
    from_model = [(c["action_call_id"], c["action"]["name"], c["action"]["arguments"]) for c in calls if c["action"]["kind"] == "call_tool"]
    executed = [(t["tool_call_id"], t["name"], t["arguments"]) for t in service.records("trace", "tool_calls") if t["caller"] == "agent"]
    assert executed == from_model, "tool_calls.jsonl lists exactly the calls the model made"
    presented = {c["action_call_id"]: c["action"]["plan"] for c in calls if c["action"]["kind"] == "present_plan"}
    plans = service.records("trace", "plans")
    assert {p["tool_call_id"] for p in plans} == set(presented)
    for p in plans:
        quotes = (p["proposal"]["flight_quote_id"], p["proposal"]["hotel_quote_id"]) if p["displayed"] else (p["draft"]["flight_quote_id"], p["draft"]["hotel_quote_id"])
        assert quotes == (presented[p["tool_call_id"]]["flight_quote_id"], presented[p["tool_call_id"]]["hotel_quote_id"])


def test_artifacts_on_disk_are_the_recorded_execution(runs):
    path = runs.service.path(runs.state)
    assert {p.name for p in path.iterdir() if not p.name.startswith(("session.sqlite", "."))} == {
        "manifest.json", "changes.jsonl", "state_snapshots.jsonl", "transcript.json", "model_calls.jsonl", "tool_calls.jsonl",
        "plans.jsonl", "checks.json", "report.html"}
    assert read_jsonl(path / "model_calls.jsonl") == runs.service.records(runs.state, "model_calls")
    assert read_jsonl(path / "tool_calls.jsonl") == runs.service.records(runs.state, "tool_calls")
    assert read_jsonl(path / "plans.jsonl") == runs.service.records(runs.state, "plans")
    assert json.loads((path / "transcript.json").read_text()) == runs.service.transcript(runs.state)
    snapshots = read_jsonl(path / "state_snapshots.jsonl")
    assert snapshots[0]["change_id"] == "initial" and snapshots[0]["before"] is None
    assert [s["change_id"] for s in snapshots][:3] == ["initial", "obs-tc-0001", "obs-tc-0002"]
    for s in snapshots[1:]:
        assert s["before"] is not None


# ---------------------------------------------------------------------------------------
# Labels and numbers that must not be invented
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name_attr", ["one_shot", "transcript", "state", "state_nc"])
def test_simulated_outputs_are_labeled_and_carry_no_invented_numbers(runs, name_attr):
    name = getattr(runs, name_attr)
    path = runs.service.path(name)
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["planner_mode"] == "simulated" and manifest["provider"] is None
    assert manifest["model_settings"]["planner_mode"] == "simulated" and manifest["model_settings"]["model"] is None
    assert manifest["fixture_revision"] == "inventory-2027-04-v1" and manifest["clock"]["start"] == "2027-03-15T14:00:00+00:00"
    assert manifest["clock"]["time_zone"] == "America/Chicago"
    rows = read_jsonl(path / "model_calls.jsonl")
    assert rows
    for row in rows:
        assert row["planner_mode"] == "simulated" and row["settings"]["planner_mode"] == "simulated"
        assert row["usage"] is None, "the simulator has no token counts to report"
        assert row["latency_ms"] is None, "the simulator has no model latency to report"
        assert row["provider_request"] is None
    summary = run_summary(path)
    assert summary["planner_mode"] == "simulated" and summary["totals"]["usage_reported"] is False
    assert summary["totals"]["input_tokens"] is None and summary["totals"]["output_tokens"] is None
    page = text_of(report_html(runs, name))
    assert "planner_mode=simulated" in page and "not an LLM measurement" in page
    assert "token usage not reported (simulated planner)" in page


def test_report_counts_match_the_recorded_calls(runs):
    page = text_of(report_html(runs, runs.state))
    model_calls = len(runs.service.records(runs.state, "model_calls"))
    agent_tools = len([t for t in runs.service.records(runs.state, "tool_calls") if t["caller"] == "agent"])
    chars = sum(c["input_chars"] for c in runs.service.records(runs.state, "model_calls"))
    assert f"{model_calls} model calls, {agent_tools} agent tool calls" in page and f"{chars:,} input characters" in page


# ---------------------------------------------------------------------------------------
# The report shows the pieces separately
# ---------------------------------------------------------------------------------------


def test_the_report_separates_constraints_freshness_preferences_and_confirmation(runs):
    page = text_of(report_html(runs, runs.state))
    for label in ("Hard constraints:", "Quote freshness:", "Preferences:", "Confirmation:"):
        assert label in page
    last_card = page[page.index("plan-004: flight f3"):]
    assert "Computed total $1,740" in last_card
    assert "Hard constraints: pass" in last_card and "Quote freshness: fresh" in last_card and "Preferences: met" in last_card
    assert "Confirmation: reconfirmation_required" in last_card
    assert "hard_constraint" not in last_card.split("Reason codes")[1].split("Explanation")[0], "a confirmation is not listed as a hard constraint"
    assert "confirmation: confirmation_required" in last_card
    first_card = page[page.index("plan-001: flight f1"): page.index("plan-002: flight")]
    assert "Confirmation: pending_user_review" in first_card


def test_the_state_report_shows_transitions_and_the_retained_decision(runs):
    raw = report_html(runs, runs.state)
    page = text_of(raw)
    for turn in range(4):
        assert f"Turn {turn}" in page
    assert page.count("normalized changes") == 3
    assert page.count("exact model input") == len(runs.service.records(runs.state, "model_calls"))
    assert "Retained hotel decision" in page and '"hotel_id": "h17"' in page and "needs_reconfirmation" in page
    assert "chg-t3-0" in page and "quotes.quote-h17-r1-2a1c.status" in page
    assert "stale" in page and "expired" in page, "invalidated observations are visible in the transitions"
    assert "tool result" in page


def test_the_transcript_report_does_not_invent_state_snapshots(runs):
    page = text_of(report_html(runs, runs.transcript))
    assert "keeps no structured task state" in page
    assert "State transitions" not in page and "Retained hotel decision" not in page


def test_reports_open_offline(runs):
    for name in (runs.state, runs.transcript, runs.one_shot, runs.state_nc):
        page = report_html(runs, name)
        parsed = parse(page)
        assert parsed.tags[0] == "html" and "meta" in parsed.tags and "style" in parsed.tags
        assert not {"script", "link", "img", "iframe", "object", "embed", "video", "audio", "source"} & set(parsed.tags), name
        for tag, attr, value in parsed.attrs:
            assert attr not in ("src", "href", "action") or not (value or "").startswith(("http:", "https:", "//")), (tag, attr, value)
        assert "@import" not in page and "url(" not in page


# ---------------------------------------------------------------------------------------
# Comparing two successful runs
# ---------------------------------------------------------------------------------------


def comparison_page(runs, tmp_path: Path, *names: str) -> str:
    out = tmp_path / "comparison.html"
    runs.service.compare(list(names), out)
    return out.read_text()


def test_two_successful_agents_are_reported_as_matching_without_a_manufactured_failure(runs, tmp_path):
    for name in (runs.transcript, runs.state):
        checks = json.loads((runs.service.path(name) / "checks.json").read_text())
        assert checks and all(c["passed"] is not False for c in checks), [c for c in checks if c["passed"] is False]
    page = comparison_page(runs, tmp_path, runs.transcript, runs.state)
    assert 'class="bad"' not in page, "nothing in a matching comparison is marked as a failure"
    text = text_of(page)
    assert "Model settings differ" not in text
    assert text.count("Computed total $1,740") == 2 and text.count("Computed total $1,200") == 2
    assert text.count("plan-004: flight f3") == 2
    assert "transcript" in text and "state" in text
    assert "one run is one sample" in text.lower() and "does not establish reliability or superiority" in text
    assert text.count("PASS") >= 2 * 8 and "FAIL" not in text


def test_differing_model_settings_are_called_out(service, monkeypatch, tmp_path):
    a = RecordingPlanner()
    b = RecordingPlanner()
    b.settings = lambda: {"planner_mode": "simulated", "model": "a-different-model", "note": "test"}  # type: ignore[method-assign]
    install_planner(monkeypatch, a)
    run_session(service, "a", "transcript")
    install_planner(monkeypatch, b)
    run_session(service, "b", "transcript")
    out = tmp_path / "c.html"
    service.compare(["a", "b"], out)
    assert "Model settings differ between these runs" in out.read_text()


def test_a_failed_run_is_shown_as_failed_next_to_a_successful_one(service, monkeypatch, tmp_path):
    run_session(service, "good", "transcript")
    install_planner(monkeypatch, ScriptedPlanner([], then=CallTool(name="read_history", arguments={"role": "any"})))
    run_session(service, "loops", "transcript")
    out = tmp_path / "c.html"
    service.compare(["good", "loops"], out)
    page = out.read_text()
    assert text_of(page).count("Incomplete: Tool-call limit (8)") >= 1 and 'class="bad"' in page


def test_comparison_output_is_not_written_into_an_existing_directory_by_accident(runs, tmp_path):
    out = tmp_path / "nested" / "dir" / "comparison.html"
    runs.service.compare([runs.transcript, runs.state], out)
    assert out.exists() and out.read_text().startswith("<!doctype html>")


def test_render_comparison_accepts_one_to_three_runs(runs):
    summaries = [run_summary(runs.service.path(n)) for n in (runs.one_shot, runs.transcript, runs.state)]
    page = render_comparison(summaries)
    assert text_of(page).count("rule-based; not an LLM measurement") == 3 and "not reached" in page, "the one-shot run has fewer turns"


# ---------------------------------------------------------------------------------------
# Hostile strings
# ---------------------------------------------------------------------------------------


def hostile_runs(service, monkeypatch):
    shown = PresentPlan(plan=PlanDraft(flight_quote_id="quote-f1-r1-2a0c", hotel_quote_id="quote-h11-r1-2a0c", claimed_total_cents=120_000),
                        explanation=HOSTILE)
    install_planner(monkeypatch, ScriptedPlanner([shown]))
    run_session(service, "evil<b>plan", "one-shot", scenario="initial")
    install_planner(monkeypatch, ScriptedPlanner([AskUser(question=HOSTILE, unresolved_constraint=HOSTILE)]))
    run_session(service, "evil-question", "transcript", scenario="initial")
    bad_args = CallTool(name="search_hotels", arguments={"check_in": HOSTILE, "check_out": HOSTILE, "adults": 2, "children": 0})
    install_planner(monkeypatch, ScriptedPlanner([bad_args, AskUser(question="done", unresolved_constraint="x")]))
    run_session(service, "evil-tool", "transcript", scenario="initial")
    return ["evil<b>plan", "evil-question", "evil-tool"]


def test_hostile_strings_are_escaped_in_every_report(service, monkeypatch, tmp_path):
    names = hostile_runs(service, monkeypatch)
    escaped = html.escape(HOSTILE)
    pages = {n: (service.path(n) / "report.html").read_text() for n in names}
    out = tmp_path / "c.html"
    service.compare(names, out)
    pages["comparison"] = out.read_text()
    for label, page in pages.items():
        tags = parse(page).tags
        assert "script" not in tags and "img" not in tags and "b" not in tags, f"{label}: hostile markup became real elements"
        assert HOSTILE not in page and "<script" not in page.lower() and "<img" not in page.lower()
    assert escaped in pages["evil<b>plan"], "the explanation appears, escaped"
    assert escaped in pages["evil-question"] and escaped in pages["comparison"]
    assert "evil&lt;b&gt;plan" in pages["evil<b>plan"], "even the session name is escaped"
    assert "error:" in text_of(pages["evil-tool"]) and "&lt;img src=x" in pages["evil-tool"]


def test_hostile_text_is_kept_verbatim_in_the_evidence_files(service, monkeypatch):
    hostile_runs(service, monkeypatch)
    plan = service.records("evil<b>plan", "plans")[0]
    assert plan["proposal"]["explanation"] == HOSTILE, "escaping happens at render time; the record is untouched"
    assert read_jsonl(service.path("evil<b>plan") / "plans.jsonl")[0]["proposal"]["explanation"] == HOSTILE
    [outcome] = service.records("evil-question", "outcomes")
    assert outcome["question"] == HOSTILE
