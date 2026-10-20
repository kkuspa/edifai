"""Live planner adapters, exercised with fake SDK clients (no network, no keys)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import openai
import pytest

from agent_state_workshop.planners import LivePlanner, PlannerError, resolve_provider
from agent_state_workshop.telemetry import read_spans
from conftest import ok

GOOD = {"action": "report_test_failure", "commit": "b2", "environment": "staging",
        "evidence_event_ids": ["evt-021", "evt-032"], "blockers": ["tests_failed"], "explanation": "b2 failed."}


class Dumpable(SimpleNamespace):
    def model_dump(self, **_):
        return {k: v for k, v in vars(self).items() if not callable(v)}


def fake_anthropic(outputs, calls):
    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            out = outputs.pop(0)
            block = Dumpable(type="tool_use", id=f"tu-{len(calls)}", name="propose_next_action", input=out)
            return SimpleNamespace(id=f"msg-{len(calls)}", model="claude-test", stop_reason="tool_use", content=[block],
                                   usage=SimpleNamespace(input_tokens=100, output_tokens=20))

    class Client:
        def __init__(self, **_):
            self.messages = Messages()

    return Client


def fake_openai(outputs, calls):
    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            out = outputs.pop(0)
            call = SimpleNamespace(id=f"call-{len(calls)}", function=SimpleNamespace(name="propose_next_action",
                                                                                     arguments=json.dumps(out)))
            message = Dumpable(role="assistant", content=None, tool_calls=[call])
            message.model_dump = lambda **_: {"role": "assistant", "tool_calls": [{"id": call.id}]}
            return SimpleNamespace(id=f"chat-{len(calls)}", model="gpt-test",
                                   choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
                                   usage=SimpleNamespace(prompt_tokens=90, completion_tokens=15))

    class Client:
        def __init__(self, **_):
            self.chat = SimpleNamespace(completions=Completions())

    return Client


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_live_run_records_model_calls_and_retries(prepared, monkeypatch, provider):
    calls: list[dict] = []
    outputs = [{"action": "ship it"}, GOOD]  # first output is malformed, second is valid
    if provider == "anthropic":
        monkeypatch.setattr(anthropic, "Anthropic", fake_anthropic(outputs, calls))
    else:
        monkeypatch.setattr(openai, "OpenAI", fake_openai(outputs, calls))
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test-key-not-real")

    r = ok(prepared.execute("run", {"db": ".workshop/s.sqlite", "strategy": "reference", "planner": "live",
                                    "provider": provider, "model": "test-model", "out": "runs/live"}))
    assert r["proposal"]["action"] == "report_test_failure"
    assert len(calls) == 2 and all(c["model"] == "test-model" for c in calls)
    run = prepared.root / "runs/live"
    spans = read_spans(run / "spans.jsonl")
    gens = [s for s in spans if s["name"] == "model.generate"]
    propose = next(s for s in spans if s["name"] == "planner.propose")
    assert len(gens) == 2 and all(g["parent_span_id"] == propose["span_id"] for g in gens)
    assert gens[0]["attributes"]["workshop.llm.output_valid"] is False
    assert gens[1]["attributes"]["workshop.llm.output_valid"] is True
    assert propose["attributes"]["workshop.llm.attempts"] == 2
    request = (run / "llm-request.json").read_text()
    assert "test-key-not-real" not in request and "test-key-not-real" not in (run / "spans.jsonl").read_text()
    assert json.loads(request)["system_prompt"] == json.loads((run / "planner-input.json").read_text())["system_prompt"]


def test_bounded_retries(prepared, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(anthropic, "Anthropic", fake_anthropic([{"bad": 1}] * 5, calls))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    rec = prepared.execute("run", {"db": ".workshop/s.sqlite", "strategy": "reference", "planner": "live",
                                   "provider": "anthropic", "out": "runs/bad"})
    assert rec["status"] == "done" and rec["result"]["status"] == "failed"
    assert len(calls) == 3


def test_provider_resolution(monkeypatch):
    for key in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(PlannerError):
        resolve_provider(None)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert resolve_provider(None) == "openai"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert resolve_provider(None) == "anthropic"
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert resolve_provider(None) == "openai"
    assert LivePlanner("anthropic", "m").describe()["provider"] == "anthropic"
