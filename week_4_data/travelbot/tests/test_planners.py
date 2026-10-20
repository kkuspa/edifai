"""Planners: the simulator, the live adapters (offline), and the provider message shapes.

No test here opens a network connection or builds a real SDK client. The Anthropic and OpenAI
planners are built with ``object.__new__`` and handed fake clients, or the SDK constructors are
replaced before ``make_planner`` runs.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import TypeAdapter

import workshop.planners as planners_module
from tests.helpers import install_planner, run_session
from workshop.models import (
    AskUser,
    CallTool,
    ModelInput,
    PlannerAction,
    PlanDraft,
    PresentPlan,
    ToolCallRecord,
    TranscriptEntry,
    Usage,
)
from workshop.planners import (
    AnthropicPlanner,
    LivePlanner,
    OpenAIPlanner,
    ProviderTurn,
    SimulatedPlanner,
    action_to_tool_call,
    live_provider_status,
    make_planner,
    parse_tool_call,
    resolve_provider,
)
from workshop.prompts import TASK_INSTRUCTIONS
from workshop.tools import AGENT_TOOLS, TOOL_SPECS

# ======================================================================================
# Helpers
# ======================================================================================

ADAPTER = TypeAdapter(PlannerAction)


def tools() -> list:
    return [TOOL_SPECS[n] for n in AGENT_TOOLS]


def user(text: str = "Plan a trip.", turn: int = 0, role: str = "user") -> TranscriptEntry:
    return TranscriptEntry(turn=turn, role=role, content=text)  # type: ignore[arg-type]


def assistant(call_id: str, name: str = "search_hotels", args: dict | None = None, text: str = "", raw: dict | None = None) -> TranscriptEntry:
    call = ToolCallRecord(id=call_id, name=name, arguments=args if args is not None else {"adults": 2})
    return TranscriptEntry(turn=0, role="assistant", content=text, tool_call=call, provider_raw=raw)


def tool_result(call_id: str, content: str = '{"ok": true}') -> TranscriptEntry:
    return TranscriptEntry(turn=0, role="tool", tool_call_id=call_id, content=content)


def model_input(entries: list[TranscriptEntry]) -> ModelInput:
    return ModelInput(implementation="transcript", system=TASK_INSTRUCTIONS, messages=entries, tools=tools())


def rich_transcript() -> list[TranscriptEntry]:
    """Everything a provider has to replay: user text, calls, results, notification, a failed attempt, a correction."""
    return [
        user("Plan a three-night trip."),
        assistant("tc-1", "search_hotels", {"check_in": "2027-04-12", "check_out": "2027-04-15", "adults": 2, "children": 0}, text="[simulated] hotels"),
        tool_result("tc-1", '{"ok": true, "quotes": []}'),
        assistant("tc-2", "present_plan", {"flight_quote_id": "a", "hotel_quote_id": "b", "explanation": "ok"}),
        tool_result("tc-2", "Displayed to the user as plan-001."),
        user("Actually, we're bringing our child.", turn=1),
        assistant("toolu_bad", "book_hotel", {"hotel": "h17"}),  # a failed attempt
        tool_result("toolu_bad", "Error: invalid tool call: Unknown tool 'book_hotel'. Call exactly one tool with valid arguments."),
        TranscriptEntry(turn=1, role="assistant", content="I will just answer in prose."),  # a failed attempt with no tool call
        user("[Application] no tool call in response. Reply with exactly one tool call.", turn=1),
        assistant("tc-3", "search_flights", {"origin": "RDU"}),
        tool_result("tc-3"),
        user("Inventory notice: the quote for h17 has expired.", turn=3, role="notification"),
    ]


def assert_anthropic_shape(messages: list[dict[str, Any]]) -> None:
    assert messages and messages[0]["role"] == "user", "the conversation must open with a user message"
    pending: set[str] = set()
    previous = None
    for i, m in enumerate(messages):
        assert m["role"] in ("user", "assistant")
        assert m["role"] != previous, f"message {i}: roles must alternate"
        previous = m["role"]
        blocks = m["content"]
        assert isinstance(blocks, list) and blocks, f"message {i}: content must be a non-empty block list"
        if m["role"] == "assistant":
            assert not pending, f"message {i}: tool_use ids {pending} were never answered"
            pending = {b["id"] for b in blocks if b["type"] == "tool_use"}
            for b in blocks:
                if b["type"] == "text":
                    assert b["text"].strip(), "empty text block"
        else:
            kinds = [b["type"] for b in blocks]
            n = kinds.count("tool_result")
            assert kinds[:n] == ["tool_result"] * n, f"message {i}: tool_result blocks must come first"
            assert {b["tool_use_id"] for b in blocks if b["type"] == "tool_result"} == pending, f"message {i}: tool results must match tool_use ids"
            pending = set()
    assert previous == "user", "the next request must end on a user message"


def assert_openai_shape(messages: list[dict[str, Any]]) -> None:
    assert messages[0]["role"] == "system" and isinstance(messages[0]["content"], str)
    pending: set[str] = set()
    for i, m in enumerate(messages[1:], 1):
        if m["role"] == "assistant":
            assert not pending, f"message {i}: tool calls {pending} were never answered"
            calls = m.get("tool_calls") or []
            assert m.get("content") is not None or calls, f"message {i}: an assistant message needs content or tool_calls"
            pending = {c["id"] for c in calls}
            for c in calls:
                assert c["type"] == "function" and isinstance(c["function"]["arguments"], str)
        elif m["role"] == "tool":
            assert m["tool_call_id"] in pending, f"message {i}: tool result without a pending call"
            pending.discard(m["tool_call_id"])
        else:
            assert m["role"] == "user" and not pending, f"message {i}: user message while tool calls are pending"
            assert isinstance(m["content"], str) and m["content"].strip()
    assert not pending
    assert messages[-1]["role"] in ("user", "tool")


def check_arguments(name: str, arguments: dict[str, Any]) -> None:
    """A small JSON-schema check: required keys present, no unknown keys, basic types."""
    schema = TOOL_SPECS[name].parameters
    assert set(schema["required"]) <= set(arguments), (name, arguments)
    assert set(arguments) <= set(schema["properties"]), (name, arguments)
    types = {"string": str, "integer": int, "object": dict}
    for key, value in arguments.items():
        allowed = schema["properties"][key].get("type")
        allowed = allowed if isinstance(allowed, list) else [allowed]
        ok = (value is None and "null" in allowed) or any(isinstance(value, types[t]) and not isinstance(value, bool) for t in allowed if t in types)
        assert ok, (name, key, value, allowed)


# ======================================================================================
# The simulator
# ======================================================================================


def test_the_simulator_is_a_pure_function_of_the_input_it_is_given(runs):
    """Replaying every recorded input reproduces the recorded action, so the record is the real input."""
    for name in (runs.state, runs.transcript, runs.one_shot_restated, runs.state_nc):
        for call in runs.service.records(name, "model_calls"):
            mi = ModelInput.model_validate(call["input"])
            response = SimulatedPlanner().next_action(mi, call["action_call_id"])
            assert response.action.model_dump(mode="json") == call["action"], (name, call["index"])
            assert response.usage is None and response.latency_ms is None and response.provider_request is None


def test_the_simulator_reads_the_supplied_context_not_an_answer_key(runs):
    call = next(c for c in runs.service.records(runs.state, "model_calls") if c["turn"] == 2 and c["step"] == 0)
    original = ModelInput.model_validate(call["input"])
    assert call["action"]["arguments"]["arrive_before"] == "18:00"

    changed = original.model_copy(deep=True)
    prefs = changed.context.section("requirements").content["preferences"]["earlier_arrival"]
    prefs["arrive_before_local"] = "15:00"
    assert SimulatedPlanner().next_action(changed, "x").action.arguments["arrive_before"] == "15:00"

    without = original.model_copy(deep=True)
    without.context.sections = [s for s in without.context.sections if s.name != "requirements"]
    action = SimulatedPlanner().next_action(without, "x").action
    assert isinstance(action, AskUser) and "requirements" in action.question, "a missing section means nothing is known"


def test_the_simulator_trusts_a_stale_context_which_is_why_the_exercise_matters(runs):
    call = next(c for c in runs.service.records(runs.state, "model_calls") if c["turn"] == 1 and c["step"] == 1)
    mi = ModelInput.model_validate(call["input"])
    for s in mi.context.sections:
        if s.name == "current_quotes":
            s.content = []
    action = SimulatedPlanner().next_action(mi, "x").action
    assert isinstance(action, CallTool) and action.name in ("search_hotels", "search_flights", "refresh_quote"), "no quotes means it goes looking"


def test_the_simulator_module_never_imports_the_validator_scenarios_or_the_store():
    tree = ast.parse(Path(planners_module.__file__).read_text())
    package_imports = {"." * n.level + (n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level}
    assert package_imports <= {".models", ".tools"}, package_imports
    source = Path(planners_module.__file__).read_text()
    for forbidden in ("changing-trip", "no-compliant", "plan-004", "fixtures", "validate_plan(", "Authority"):
        assert forbidden not in source.replace('"validate_plan"', ""), forbidden


def test_the_simulator_is_deterministic(service):
    run_session(service, "one", "state")
    run_session(service, "two", "state")
    actions = lambda n: [(c["action"], c["action_call_id"], c["input_chars"]) for c in service.records(n, "model_calls")]  # noqa: E731
    assert actions("one") == actions("two")


@pytest.mark.parametrize("name_attr", ["state", "transcript", "one_shot_restated", "state_nc"])
def test_every_simulated_action_is_a_valid_call_against_the_shared_tool_definitions(runs, name_attr):
    seen = set()
    for call in runs.service.records(getattr(runs, name_attr), "model_calls"):
        action = ADAPTER.validate_python(call["action"])
        record = action_to_tool_call(action, call["action_call_id"])
        check_arguments(record.name, record.arguments)
        assert parse_tool_call(record.name, record.arguments) == action
        seen.add(record.name)
    assert {"present_plan"} <= seen


# ======================================================================================
# parse_tool_call
# ======================================================================================


def test_parse_tool_call_rejects_unknown_tools():
    with pytest.raises(ValueError, match="Unknown tool 'book_hotel'") as exc:
        parse_tool_call("book_hotel", {"hotel": "h17"})
    assert "search_flights" in str(exc.value), "the error lists the tools that do exist"


@pytest.mark.parametrize(
    "name, arguments, missing",
    [
        ("search_hotels", {}, "check_in, check_out, adults, children"),
        ("search_flights", {"origin": "RDU"}, "destination"),
        ("refresh_quote", {"item_id": "h17"}, "depart_date"),
        ("validate_plan", {"flight_quote_id": "a"}, "hotel_quote_id"),
        ("read_history", {}, "role"),
        ("present_plan", {"flight_quote_id": "a", "hotel_quote_id": "b"}, "explanation"),
        ("ask_user", {"question": "?"}, "unresolved_constraint"),
    ],
)
def test_parse_tool_call_rejects_missing_arguments(name, arguments, missing):
    with pytest.raises(ValueError, match="missing required arguments") as exc:
        parse_tool_call(name, arguments)
    assert missing in str(exc.value)


def test_parse_tool_call_builds_typed_actions():
    plan = parse_tool_call("present_plan", {"flight_quote_id": "a", "hotel_quote_id": "b", "explanation": "fine"})
    assert plan == PresentPlan(plan=PlanDraft(flight_quote_id="a", hotel_quote_id="b", claimed_total_cents=None), explanation="fine")
    claimed = parse_tool_call("present_plan", {"flight_quote_id": "a", "hotel_quote_id": "b", "claimed_total_cents": 120_000, "explanation": "x"})
    assert claimed.plan.claimed_total_cents == 120_000
    assert parse_tool_call("ask_user", {"question": "Which dates?", "unresolved_constraint": "dates"}) == AskUser(question="Which dates?", unresolved_constraint="dates")
    args = {"role": "any", "limit": 3}
    assert parse_tool_call("read_history", args) == CallTool(name="read_history", arguments=args)


@pytest.mark.parametrize("bad", [{"claimed_total_cents": "a lot"}, {"flight_quote_id": 7}, {"hotel_quote_id": None}])
def test_parse_tool_call_rejects_wrongly_typed_plan_fields(bad):
    arguments = {"flight_quote_id": "a", "hotel_quote_id": "b", "explanation": "x", **bad}
    with pytest.raises(ValueError):
        parse_tool_call("present_plan", arguments)


# ======================================================================================
# Anthropic message shapes
# ======================================================================================


def test_anthropic_messages_for_a_rich_transcript_are_valid():
    messages = AnthropicPlanner.to_messages(rich_transcript())
    assert_anthropic_shape(messages)
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant", "user", "assistant", "user", "assistant", "user"]


def test_tool_results_come_first_in_the_merged_user_message():
    entries = [user("Plan."), assistant("tc-1"), tool_result("tc-1"), user("Now with a child.", turn=1), user("Heads up.", turn=1, role="notification")]
    messages = AnthropicPlanner.to_messages(entries)
    assert_anthropic_shape(messages)
    merged = messages[-1]["content"]
    assert [b["type"] for b in merged] == ["tool_result", "text", "text"]
    assert merged[0] == {"type": "tool_result", "tool_use_id": "tc-1", "content": '{"ok": true}'}
    assert merged[2]["text"] == "[Notification] Heads up."


def test_a_failed_attempt_and_its_correction_are_replayed_in_order():
    messages = AnthropicPlanner.to_messages(rich_transcript())
    assert [b["type"] for b in messages[4]["content"]] == ["tool_result", "text"], "the child's message rides with the previous tool result"
    attempt = [b for b in messages[5]["content"] if b["type"] == "tool_use"]
    assert attempt and attempt[0]["name"] == "book_hotel" and attempt[0]["id"] == "toolu_bad"
    correction = messages[6]["content"][0]
    assert correction["type"] == "tool_result" and correction["tool_use_id"] == "toolu_bad" and correction["content"].startswith("Error: invalid tool call")
    assert messages[7] == {"role": "assistant", "content": [{"type": "text", "text": "I will just answer in prose."}]}
    assert messages[8]["content"][0]["text"].startswith("[Application] no tool call")
    assert messages[9]["content"][-1]["name"] == "search_flights"


RAW_ANTHROPIC = {
    "provider": "anthropic",
    "model": "claude-test",
    "stop_reason": "tool_use",
    "content": [
        {"type": "thinking", "thinking": "Let me think.", "signature": "SIG=="},
        {"type": "text", "text": "Checking hotels."},
        {"type": "tool_use", "id": "toolu_9", "name": "search_hotels", "input": {"adults": 2, "children": 1, "check_in": "2027-04-12", "check_out": "2027-04-15"}},
    ],
}


def test_anthropic_assistant_output_is_replayed_verbatim_when_the_raw_response_is_kept():
    entry = assistant("toolu_9", "search_hotels", {"adults": 99}, text="ignored when raw is present", raw=copy.deepcopy(RAW_ANTHROPIC))
    messages = AnthropicPlanner.to_messages([user(), entry, tool_result("toolu_9")])
    assert messages[1] == {"role": "assistant", "content": RAW_ANTHROPIC["content"]}, "signed thinking blocks and exact tool input survive"
    assert messages[1]["content"] is not entry.provider_raw["content"], "the stored record is not aliased"
    assert_anthropic_shape(messages)


def test_thinking_blocks_can_be_stripped_for_a_retry():
    entry = assistant("toolu_9", raw=copy.deepcopy(RAW_ANTHROPIC))
    stripped = AnthropicPlanner.to_messages([user(), entry, tool_result("toolu_9")], strip_thinking=True)
    assert [b["type"] for b in stripped[1]["content"]] == ["text", "tool_use"]
    only_thinking = copy.deepcopy(RAW_ANTHROPIC)
    only_thinking["content"] = [RAW_ANTHROPIC["content"][0]]
    placeholder = AnthropicPlanner.to_messages([user(), assistant("t", raw=only_thinking)], strip_thinking=True)
    assert placeholder[1]["content"] == [{"type": "text", "text": "(no content)"}]


def test_a_raw_response_from_another_provider_is_rebuilt_from_the_normalized_call():
    entry = assistant("call_7", "search_hotels", {"adults": 2}, raw={"provider": "openai", "arguments": '{"adults": 2}'})
    block = AnthropicPlanner.to_messages([user(), entry, tool_result("call_7")])[1]["content"]
    assert block == [{"type": "tool_use", "id": "call_7", "name": "search_hotels", "input": {"adults": 2}}]


def test_an_empty_assistant_message_gets_a_placeholder_for_anthropic():
    messages = AnthropicPlanner.to_messages([user(), TranscriptEntry(turn=0, role="assistant", content=""), user("again", turn=1)])
    assert messages[1]["content"] == [{"type": "text", "text": "(no content)"}]


# ======================================================================================
# OpenAI message shapes
# ======================================================================================


def test_openai_messages_for_a_rich_transcript_are_valid():
    messages = OpenAIPlanner.to_messages(TASK_INSTRUCTIONS, rich_transcript())
    assert_openai_shape(messages)
    assert messages[0] == {"role": "system", "content": TASK_INSTRUCTIONS}
    assert messages[-1] == {"role": "user", "content": "[Notification] Inventory notice: the quote for h17 has expired."}
    first_call = messages[2]
    assert first_call["content"] == "[simulated] hotels"
    assert first_call["tool_calls"][0]["function"]["name"] == "search_hotels"
    assert json.loads(first_call["tool_calls"][0]["function"]["arguments"]) == {"check_in": "2027-04-12", "check_out": "2027-04-15", "adults": 2, "children": 0}
    for m in messages:
        for c in m.get("tool_calls") or []:
            json.loads(c["function"]["arguments"])  # arguments the adapter built are valid JSON
    assert messages[3] == {"role": "tool", "tool_call_id": "tc-1", "content": '{"ok": true, "quotes": []}'}


def test_openai_replays_the_exact_argument_string_it_returned():
    raw_args = '{"adults":   2,  "children": 1}'
    entry = assistant("call_1", "search_hotels", {"adults": 2, "children": 1}, raw={"provider": "openai", "arguments": raw_args})
    messages = OpenAIPlanner.to_messages("sys", [user(), entry, tool_result("call_1")])
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == raw_args
    assert messages[2]["content"] is None, "a tool-call message with no text has null content"
    assert_openai_shape(messages)


def test_an_assistant_message_with_neither_text_nor_tool_call_is_still_valid_for_openai():
    """A refusal or an empty reply is recorded as an assistant entry with no content and no call.

    Replaying it as ``content: null`` with no ``tool_calls`` is not a valid chat message.
    """
    refusal = TranscriptEntry(turn=0, role="assistant", content="", provider_raw={"provider": "openai", "arguments": None, "finish_reason": "stop"})
    correction = user("[Application] model refused: I can't help with that. Reply with exactly one tool call.")
    messages = OpenAIPlanner.to_messages("sys", [user(), refusal, correction])
    assert_openai_shape(messages)


# ======================================================================================
# Real transcripts convert cleanly at every model call
# ======================================================================================


def transcript_inputs(service, name):
    for call in service.records(name, "model_calls"):
        yield [TranscriptEntry.model_validate(m) for m in call["input"]["messages"]], call["input"]["system"]


@pytest.mark.parametrize("name_attr", ["transcript", "transcript_nc", "state", "one_shot_restated"])
def test_every_recorded_input_converts_to_valid_provider_messages(runs, name_attr):
    count = 0
    for entries, system in transcript_inputs(runs.service, getattr(runs, name_attr)):
        assert_anthropic_shape(AnthropicPlanner.to_messages(entries))
        assert_openai_shape(OpenAIPlanner.to_messages(system, entries))
        count += 1
    assert count >= 4


def test_transcripts_with_rejections_and_exhausted_budgets_also_convert(service, monkeypatch):
    from tests.helpers import ScriptedPlanner

    over = PresentPlan(plan=PlanDraft(flight_quote_id="quote-f4-r1-2a0c", hotel_quote_id="quote-h30-r1-2a0c"), explanation="Premium.")
    install_planner(monkeypatch, ScriptedPlanner([CallTool(name="search_hotels", arguments={"check_in": "bad"}), over],
                                                 then=CallTool(name="read_history", arguments={"role": "any"})))
    run_session(service, "messy", "transcript", scenario="initial")
    count = 0
    for entries, system in transcript_inputs(service, "messy"):
        assert_anthropic_shape(AnthropicPlanner.to_messages(entries))
        assert_openai_shape(OpenAIPlanner.to_messages(system, entries))
        count += 1
    assert count == 10  # a bad search, a rejected plan, seven reads, and the ninth tool call that was refused
    final = service.transcript("messy")
    assert any("Validation failed" in e["content"] for e in final if e["role"] == "tool")
    assert any("budget for this turn is exhausted" in e["content"] for e in final if e["role"] == "tool")


# ======================================================================================
# LivePlanner: bounded retries and honest records
# ======================================================================================


def provider_turn(name: str | None, args: dict | None = None, call_id: str | None = "toolu_1", usage: tuple[int | None, int | None] = (10, 5),
                  error: str | None = None, text: str = "") -> ProviderTurn:
    call = ToolCallRecord(id=call_id, name=name, arguments=args or {}) if name and call_id else None
    entry = TranscriptEntry(turn=-1, role="assistant", content=text, tool_call=call, provider_raw={"provider": "fake", "id": call_id})
    return ProviderTurn(entry, name, args if name else None, call_id if name else None, Usage(input_tokens=usage[0], output_tokens=usage[1]),
                        error, request={"call": call_id})


VALID_ASK = {"question": "Which dates?", "unresolved_constraint": "dates"}


class FakeLive(LivePlanner):
    provider = "fake"

    def __init__(self, turns: list[Any], max_retries: int = 2):
        super().__init__("fake-model", 100, max_retries)
        self.turns = list(turns)
        self.calls: list[list[TranscriptEntry]] = []

    def _call(self, system, entries, tools):
        self.calls.append(list(entries))
        item = self.turns.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_a_valid_first_reply_costs_one_call():
    planner = FakeLive([provider_turn("ask_user", VALID_ASK)])
    mi = model_input([user()])
    r = planner.next_action(mi, "fallback-id")
    assert r.action == AskUser(**VALID_ASK) and r.attempts == 1 and r.errors == []
    assert r.action_call_id == "toolu_1", "the provider's own tool-call id is the one recorded"
    assert (r.usage.input_tokens, r.usage.output_tokens) == (10, 5)
    assert isinstance(r.latency_ms, float) and r.latency_ms >= 0
    assert len(r.transcript_additions) == 1 and r.provider_request == [{"call": "toolu_1"}]
    assert len(mi.messages) == 1, "the caller's message list is not mutated"


def test_a_malformed_reply_is_corrected_once_and_the_valid_retry_is_used():
    planner = FakeLive([provider_turn("book_hotel", {"hotel": "h17"}, call_id="toolu_bad", usage=(10, 5)),
                        provider_turn("ask_user", VALID_ASK, call_id="toolu_good", usage=(12, 6))])
    r = planner.next_action(model_input([user()]), "fb")
    assert r.action == AskUser(**VALID_ASK) and r.attempts == 2 and r.action_call_id == "toolu_good"
    assert len(r.errors) == 1 and r.errors[0].startswith("invalid tool call: Unknown tool 'book_hotel'")
    assert (r.usage.input_tokens, r.usage.output_tokens) == (22, 11), "usage adds up across attempts"
    roles = [e.role for e in r.transcript_additions]
    assert roles == ["assistant", "tool", "assistant"], "the failed attempt and the correction stay in the history"
    correction = r.transcript_additions[1]
    assert correction.tool_call_id == "toolu_bad" and correction.content.startswith("Error: invalid tool call")
    second_call = planner.calls[1]
    assert [e.role for e in second_call[-2:]] == ["assistant", "tool"], "the model sees its mistake before the retry"


def test_missing_arguments_and_text_only_replies_are_both_retried():
    planner = FakeLive([provider_turn("ask_user", {"question": "?"}, call_id="t1"),
                        provider_turn(None, text="Sure, I can help."),
                        provider_turn("ask_user", VALID_ASK, call_id="t3")])
    r = planner.next_action(model_input([user()]), "fb")
    assert r.attempts == 3 and r.action == AskUser(**VALID_ASK)
    assert "missing required arguments: unresolved_constraint" in r.errors[0] and r.errors[1] == "no tool call in response"
    assert [e.role for e in r.transcript_additions] == ["assistant", "tool", "assistant", "user", "assistant"]
    assert r.transcript_additions[3].content.startswith("[Application] no tool call in response")


def test_a_reply_flagged_with_an_error_is_not_used_even_if_it_contains_a_tool_call():
    planner = FakeLive([provider_turn("ask_user", VALID_ASK, call_id="t1", error="response hit max_tokens before finishing"),
                        provider_turn("ask_user", VALID_ASK, call_id="t2")])
    r = planner.next_action(model_input([user()]), "fb")
    assert r.attempts == 2 and r.action_call_id == "t2" and r.errors == ["response hit max_tokens before finishing"]


@pytest.mark.parametrize("max_retries", [0, 1, 2, 4])
def test_retries_are_bounded_when_every_reply_is_malformed(max_retries):
    planner = FakeLive([provider_turn("book_hotel", {}, call_id=f"bad-{i}") for i in range(20)], max_retries=max_retries)
    r = planner.next_action(model_input([user()]), "fb")
    assert len(planner.calls) == max_retries + 1 and r.attempts == max_retries + 1
    assert r.action is None and len(r.errors) == max_retries + 1
    assert all("Unknown tool" in e for e in r.errors)
    assert len(r.transcript_additions) == 2 * (max_retries + 1), "every failed attempt and correction is kept"
    assert (r.usage.input_tokens, r.usage.output_tokens) == (10 * (max_retries + 1), 5 * (max_retries + 1))


def test_a_provider_exception_ends_the_attempt_without_retrying_and_is_recorded():
    planner = FakeLive([RuntimeError("rate limited")])
    r = planner.next_action(model_input([user()]), "fb")
    assert len(planner.calls) == 1 and r.action is None and r.attempts == 1
    assert r.errors == ["provider error: RuntimeError: rate limited"]
    assert r.action_call_id == "fb"


def test_a_provider_exception_after_a_bad_reply_keeps_the_earlier_history():
    planner = FakeLive([provider_turn("book_hotel", {}, call_id="bad"), ConnectionError("reset")])
    r = planner.next_action(model_input([user()]), "fb")
    assert r.action is None and r.attempts == 2 and len(r.errors) == 2
    assert r.errors[1] == "provider error: ConnectionError: reset"
    assert [e.role for e in r.transcript_additions] == ["assistant", "tool"]


@pytest.mark.parametrize("bad_usage", [(None, None), (None, 7)])
def test_missing_usage_is_not_turned_into_zero(bad_usage):
    """A provider that does not report token counts must not appear to have reported zero."""
    planner = FakeLive([provider_turn("ask_user", VALID_ASK, usage=bad_usage)])
    r = planner.next_action(model_input([user()]), "fb")
    assert r.usage is None or r.usage.input_tokens is None, f"invented input_tokens={r.usage.input_tokens}"


def test_a_failed_provider_call_reports_no_token_usage():
    r = FakeLive([RuntimeError("boom")]).next_action(model_input([user()]), "fb")
    assert r.usage is None or (r.usage.input_tokens is None and r.usage.output_tokens is None), "nothing was billed or reported"


def test_live_settings_are_recorded_for_the_manifest():
    s = FakeLive([], max_retries=3).settings()
    assert s["planner_mode"] == "live" and s["provider"] == "fake" and s["model"] == "fake-model" and s["max_malformed_retries"] == 3


# ======================================================================================
# Live planner inside the engine (no network)
# ======================================================================================


class SimulatedModel(LivePlanner):
    """A 'live' planner whose provider is the rule-based simulator, so the live path runs offline."""

    provider = "fake"

    def __init__(self) -> None:
        super().__init__("fake-model-1", 1000, 2)
        self.n = 0

    def _call(self, system, entries, tools):
        self.n += 1
        call_id = f"toolu_{self.n:04d}"
        action = SimulatedPlanner().next_action(ModelInput(implementation="transcript", system=system, messages=entries, tools=tools), call_id).action
        record = action_to_tool_call(action, call_id)
        entry = TranscriptEntry(turn=-1, role="assistant", content="", tool_call=record, provider_raw={"provider": "fake", "content": []})
        return ProviderTurn(entry, record.name, record.arguments, call_id, Usage(input_tokens=1000 + len(entries), output_tokens=25),
                            request={"messages": len(entries)})


def test_a_live_labeled_run_reports_provider_usage_and_real_latency(service, monkeypatch, tmp_path):
    run_session(service, "sim", "transcript")
    install_planner(monkeypatch, SimulatedModel())
    run_session(service, "live", "transcript", planner="live")
    path = service.path("live")
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["planner_mode"] == "live" and manifest["provider"] == "fake" and manifest["model_settings"]["model"] == "fake-model-1"
    rows = service.records("live", "model_calls")
    assert rows and all(r["planner_mode"] == "live" and r["latency_ms"] is not None and r["usage"]["output_tokens"] == 25 for r in rows)
    assert all(r["provider_request"] for r in rows)
    expected_input = sum(r["usage"]["input_tokens"] for r in rows)
    summary = service.summary("live")
    assert summary["totals"]["input_tokens"] == expected_input and summary["totals"]["usage_reported"] is True
    page = (path / "report.html").read_text()
    assert "planner_mode=live" in page and "planner_mode=simulated" not in page and "(provider reported)" in page
    assert f"{expected_input:,} input" in page
    checks = {c["id"]: c["passed"] for c in json.loads((path / "checks.json").read_text())}
    assert checks["planner_mode_labeled"] is True and checks["turns_finished"] is True
    assert [p["computed_total_cents"] for p in [x["proposal"] for x in service.records("live", "plans") if x["displayed"]]] == [120_000, 162_000, 168_000, 174_000]
    out = tmp_path / "cmp.html"
    service.compare(["sim", "live"], out)
    assert "Model settings differ" in out.read_text(), "a simulated run and a live run are not presented as like for like"


def test_a_live_planner_that_never_produces_a_valid_call_ends_the_turn_visibly(service, monkeypatch):
    class Malformed(FakeLive):
        def _call(self, system, entries, tools):
            self.calls.append(list(entries))
            return provider_turn("book_hotel", {}, call_id=f"toolu_bad{len(self.calls)}")

    planner = install_planner(monkeypatch, Malformed([]))
    service.create("bad", implementation="transcript", scenario="initial", planner="live")
    service.run_to_completion("bad")
    [outcome] = service.records("bad", "outcomes")
    assert outcome["kind"] == "incomplete" and "did not return a valid action" in outcome["reason"] and "Unknown tool" in outcome["reason"]
    [call] = service.records("bad", "model_calls")
    assert call["attempts"] == 3 and len(call["errors"]) == 3 and call["action"] is None
    assert len(planner.calls) == 3
    transcript = [TranscriptEntry.model_validate(e) for e in service.transcript("bad")]
    assert [e.role for e in transcript] == ["user"] + ["assistant", "tool"] * 3, "failed attempts stay in the append-only history"
    assert_anthropic_shape(AnthropicPlanner.to_messages(transcript))
    assert_openai_shape(OpenAIPlanner.to_messages("sys", transcript))
    assert "invalid tool call" in (service.path("bad") / "report.html").read_text()
    assert service.records("bad", "tool_calls") == [] and service.records("bad", "plans") == []


# ======================================================================================
# Anthropic adapter against a fake client
# ======================================================================================


class FakeBadRequest(Exception):
    pass


class FakeBlock:
    def __init__(self, **data: Any):
        self.data = data

    def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict[str, Any]:
        return copy.deepcopy(self.data)


def anthropic_response(blocks: list[dict[str, Any]], stop_reason: str = "tool_use", usage: tuple[int, int] = (120, 30)) -> SimpleNamespace:
    return SimpleNamespace(content=[FakeBlock(**b) for b in blocks], usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]),
                           stop_reason=stop_reason, model="claude-test", stop_details=None)


class FakeAnthropicClient:
    def __init__(self, responses: list[Any]):
        self.requests: list[dict[str, Any]] = []
        self.responses = list(responses)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(copy.deepcopy(kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def build_anthropic(client: FakeAnthropicClient, *, effort: str | None = None, fallbacks: str | None = None, max_retries: int = 2) -> AnthropicPlanner:
    planner = object.__new__(AnthropicPlanner)
    LivePlanner.__init__(planner, "claude-test", 1000, max_retries)
    planner._anthropic = SimpleNamespace(BadRequestError=FakeBadRequest)
    planner.client = client
    planner.effort, planner.fallbacks = effort, fallbacks
    return planner


TOOL_USE = {"type": "tool_use", "id": "toolu_01", "name": "search_hotels",
            "input": {"check_in": "2027-04-12", "check_out": "2027-04-15", "adults": 2, "children": 1}}


def test_anthropic_request_carries_the_shared_tools_and_one_tool_per_reply():
    client = FakeAnthropicClient([anthropic_response([{"type": "thinking", "thinking": "hm", "signature": "S"}, {"type": "text", "text": "Looking."}, TOOL_USE])])
    planner = build_anthropic(client)
    r = planner.next_action(model_input([user()]), "fb")
    [request] = client.requests
    assert request["model"] == "claude-test" and request["max_tokens"] == 1000 and request["system"] == TASK_INSTRUCTIONS
    assert request["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert request["tools"] == [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools()]
    assert_anthropic_shape(request["messages"])
    assert request["extra_body"] is None and request["extra_headers"] is None
    assert r.action == CallTool(name="search_hotels", arguments=TOOL_USE["input"]) and r.action_call_id == "toolu_01"
    assert (r.usage.input_tokens, r.usage.output_tokens) == (120, 30) and r.latency_ms is not None
    raw = r.transcript_additions[0].provider_raw
    assert raw["provider"] == "anthropic" and raw["stop_reason"] == "tool_use" and [b["type"] for b in raw["content"]] == ["thinking", "text", "tool_use"]
    assert r.transcript_additions[0].content == "Looking."
    assert r.provider_request[0]["messages"] == request["messages"], "the recorded request is the one that was sent"


def test_anthropic_effort_and_server_side_fallbacks_are_sent_as_documented():
    client = FakeAnthropicClient([anthropic_response([TOOL_USE])])
    build_anthropic(client, effort="high", fallbacks="claude-other").next_action(model_input([user()]), "fb")
    [request] = client.requests
    assert request["extra_body"] == {"output_config": {"effort": "high"}, "fallbacks": "claude-other"}
    assert request["extra_headers"] == {"anthropic-beta": "server-side-fallback-2026-07-01"}


@pytest.mark.parametrize(
    "response, expected",
    [
        (anthropic_response([TOOL_USE], stop_reason="max_tokens"), "response hit max_tokens before finishing"),
        (anthropic_response([{"type": "text", "text": "No."}], stop_reason="refusal"), "model refused"),
        (anthropic_response([{"type": "text", "text": "Sure."}], stop_reason="end_turn"), "no tool call in response"),
    ],
)
def test_anthropic_truncation_refusal_and_missing_tool_use_are_errors_not_actions(response, expected):
    client = FakeAnthropicClient([response, anthropic_response([TOOL_USE])])
    r = build_anthropic(client).next_action(model_input([user()]), "fb")
    assert expected in r.errors[0] and r.attempts == 2 and r.action is not None
    assert len(client.requests) == 2
    assert_anthropic_shape(client.requests[1]["messages"])


def test_a_rejected_thinking_replay_is_retried_once_without_thinking_blocks():
    entry = assistant("toolu_9", raw=copy.deepcopy(RAW_ANTHROPIC))
    client = FakeAnthropicClient([FakeBadRequest("messages.1.content.0: invalid `signature` in `thinking` block"), anthropic_response([TOOL_USE])])
    r = build_anthropic(client).next_action(model_input([user(), entry, tool_result("toolu_9")]), "fb")
    assert r.action is not None and r.attempts == 1 and r.errors == []
    first, second = client.requests
    assert any(b["type"] == "thinking" for b in first["messages"][1]["content"])
    assert not any(b["type"] == "thinking" for b in second["messages"][1]["content"])


def test_other_bad_requests_are_not_retried():
    client = FakeAnthropicClient([FakeBadRequest("max_tokens too large")])
    r = build_anthropic(client).next_action(model_input([user()]), "fb")
    assert r.action is None and r.attempts == 1 and len(client.requests) == 1
    assert r.errors == ["provider error: FakeBadRequest: max_tokens too large"]


# ======================================================================================
# OpenAI adapter against a fake client
# ======================================================================================


def openai_response(name: str | None = None, arguments: str = "{}", call_id: str = "call_1", content: str | None = None,
                    refusal: str | None = None, usage: tuple[int, int] = (200, 20)) -> SimpleNamespace:
    call = SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments)) if name else None
    message = SimpleNamespace(tool_calls=[call] if call else None, content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if call else "stop")],
                           usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]), model="gpt-test")


class FakeOpenAIClient:
    def __init__(self, responses: list[Any]):
        self.requests: list[dict[str, Any]] = []
        self.responses = list(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


def build_openai(client: FakeOpenAIClient, *, effort: str | None = None, temperature: float | None = None, max_retries: int = 2) -> OpenAIPlanner:
    planner = object.__new__(OpenAIPlanner)
    LivePlanner.__init__(planner, "gpt-test", 1000, max_retries)
    planner.client, planner.reasoning_effort, planner.temperature = client, effort, temperature
    return planner


def test_openai_request_requires_exactly_one_tool_call():
    args = json.dumps(TOOL_USE["input"])
    client = FakeOpenAIClient([openai_response("search_hotels", args)])
    r = build_openai(client).next_action(model_input([user()]), "fb")
    [request] = client.requests
    assert request["tool_choice"] == "required" and request["parallel_tool_calls"] is False and request["max_completion_tokens"] == 1000
    assert request["tools"] == [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools()]
    assert "reasoning_effort" not in request and "temperature" not in request, "unset options are not sent"
    assert_openai_shape(request["messages"])
    assert r.action == CallTool(name="search_hotels", arguments=TOOL_USE["input"]) and r.action_call_id == "call_1"
    assert (r.usage.input_tokens, r.usage.output_tokens) == (200, 20)
    assert r.transcript_additions[0].provider_raw["arguments"] == args


def test_openai_options_are_sent_when_set():
    client = FakeOpenAIClient([openai_response("ask_user", json.dumps(VALID_ASK))])
    build_openai(client, effort="low", temperature=0.2).next_action(model_input([user()]), "fb")
    assert client.requests[0]["reasoning_effort"] == "low" and client.requests[0]["temperature"] == 0.2


def test_openai_invalid_json_arguments_are_retried_as_errors():
    client = FakeOpenAIClient([openai_response("ask_user", "{not json"), openai_response("ask_user", json.dumps(VALID_ASK))])
    r = build_openai(client).next_action(model_input([user()]), "fb")
    assert r.attempts == 2 and r.action == AskUser(**VALID_ASK) and "not valid JSON" in r.errors[0]
    retry = client.requests[1]["messages"]
    assert_openai_shape(retry)
    assert retry[-2]["tool_calls"][0]["function"]["arguments"] == "{not json", "the bad reply is replayed as received"
    assert retry[-1]["role"] == "tool" and retry[-1]["content"].startswith("Error: tool arguments were not valid JSON")


def test_openai_refusals_are_recorded_and_retried_within_the_bound():
    client = FakeOpenAIClient([openai_response(refusal="I can't help with that.") for _ in range(5)])
    r = build_openai(client, max_retries=2).next_action(model_input([user()]), "fb")
    assert r.action is None and r.attempts == 3 and len(client.requests) == 3
    assert all("model refused: I can't help with that." in e for e in r.errors)


def test_the_request_after_an_openai_refusal_is_a_valid_message_list():
    client = FakeOpenAIClient([openai_response(refusal="I can't help with that."), openai_response("ask_user", json.dumps(VALID_ASK))])
    r = build_openai(client).next_action(model_input([user()]), "fb")
    assert r.action == AskUser(**VALID_ASK)
    assert_openai_shape(client.requests[1]["messages"])


# ======================================================================================
# Provider selection and credentials
# ======================================================================================


def test_make_planner_defaults_to_the_simulator_and_rejects_unknown_modes():
    assert isinstance(make_planner("simulated"), SimulatedPlanner) and make_planner("simulated").mode == "simulated"
    with pytest.raises(ValueError, match="Unknown planner mode"):
        make_planner("telepathy")


def test_live_mode_without_credentials_fails_clearly_and_offline():
    with pytest.raises(ValueError, match="No live provider configured"):
        make_planner("live")
    assert live_provider_status() == {"anthropic": False, "openai": False}


def test_provider_resolution_rules(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert resolve_provider(None) == "anthropic"
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert resolve_provider(None) == "anthropic", "both configured: anthropic unless told otherwise"
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert resolve_provider(None) == "openai" and resolve_provider("anthropic") == "anthropic", "an explicit request beats the environment"
    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="Unknown provider"):
        resolve_provider(None)
    monkeypatch.delenv("LLM_PROVIDER")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert resolve_provider(None) == "openai"


def test_the_anthropic_cli_profile_counts_as_configured_without_any_environment_variable(monkeypatch):
    monkeypatch.setattr("workshop.planners._ant_profile_present", lambda: True)
    assert live_provider_status() == {"anthropic": True, "openai": False}


def test_provider_status_reports_booleans_never_the_secret(monkeypatch):
    secret = "sk-ant-api03-SECRET-VALUE-1234567890"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-SECRET-VALUE")
    status = live_provider_status()
    assert status == {"anthropic": True, "openai": True} and all(isinstance(v, bool) for v in status.values())
    assert secret not in json.dumps(status)


def test_make_planner_reads_the_documented_environment_and_builds_the_sdk_client_without_a_request(monkeypatch):
    built = []
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: built.append(("anthropic", a, k)) or SimpleNamespace(messages=None))
    monkeypatch.setattr("openai.OpenAI", lambda *a, **k: built.append(("openai", a, k)) or SimpleNamespace(chat=None))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-env-model")
    monkeypatch.setenv("ANTHROPIC_EFFORT", "medium")
    monkeypatch.setenv("LLM_MAX_TOKENS", "1234")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    planner = make_planner("live", "anthropic")
    assert isinstance(planner, AnthropicPlanner) and planner.mode == "live"
    s = planner.settings()
    assert (s["model"], s["effort"], s["max_tokens"], s["max_malformed_retries"]) == ("claude-env-model", "medium", 1234, 1)
    assert "sk-ant-x" not in json.dumps(s) and built == [("anthropic", (), {})], "the key is left for the SDK to read; this code never handles it"

    monkeypatch.setenv("OPENAI_MODEL", "gpt-env")
    monkeypatch.setenv("OPENAI_TEMPERATURE", "0.3")
    openai_planner = make_planner("live", "openai")
    assert isinstance(openai_planner, OpenAIPlanner) and openai_planner.settings()["temperature"] == 0.3 and openai_planner.settings()["model"] == "gpt-env"
    assert make_planner("live", "anthropic").model == "claude-env-model"


def test_credentials_never_reach_any_stored_artifact(service, monkeypatch):
    secret = "sk-ant-api03-THIS-IS-A-TEST-SECRET-VALUE"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    client = FakeAnthropicClient([anthropic_response([{"type": "tool_use", "id": "toolu_01", "name": "ask_user", "input": VALID_ASK}], usage=(321, 45)),
                                  anthropic_response([{"type": "tool_use", "id": "toolu_02", "name": "ask_user", "input": VALID_ASK}])])
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: client)
    service.create("secret", implementation="one-shot", scenario="initial", planner="live", provider="anthropic")
    service.run_to_completion("secret")
    [outcome] = service.records("secret", "outcomes")
    assert outcome["kind"] == "question_asked" and outcome["question"] == "Which dates?"
    manifest = json.loads((service.path("secret") / "manifest.json").read_text())
    assert manifest["planner_mode"] == "live" and manifest["provider"] == "anthropic"
    [row] = service.records("secret", "model_calls")
    assert row["usage"] == {"input_tokens": 321, "output_tokens": 45} and row["provider_request"][0]["tool_choice"]["type"] == "auto"
    leaked = [p.name for p in Path(service.path("secret")).rglob("*") if p.is_file() and secret.encode() in p.read_bytes()]
    assert leaked == [], f"the API key was written to {leaked}"
    assert len(client.requests) == 1 and client.requests[0]["system"] and "x-api-key" not in json.dumps(client.requests[0]).lower()


def test_session_config_records_provider_and_settings_but_no_credentials(service, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-TEST-SECRET")
    monkeypatch.setattr("openai.OpenAI", lambda *a, **k: FakeOpenAIClient([]))
    service.create("cfg", implementation="transcript", planner="live", provider="openai")
    status = service.status("cfg")
    assert status["config"]["planner"] == "live" and status["config"]["provider"] == "openai"
    assert "sk-openai" not in json.dumps(status) and status["config"]["planner_settings"]["tool_choice"] == "required"
