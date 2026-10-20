"""The action-observation loop, as a resumable engine.

A session moves through three phases:

    ready            the turn's input has been applied; the agent may take a step
    awaiting_change  the turn ended (plan shown, question asked, or incomplete);
                     the next scenario change can be applied
    complete         every scenario turn has been processed

One *step* is one planner decision plus its effect:

    1. Build the model input (transcript, built context, or one-shot request).
    2. Ask the planner for one action.
    3. call_tool    -> execute the read-only tool, record the observation, stay in the turn.
       present_plan -> the runner validates independently. Compliant: show it and end the
                       turn. Not compliant: return reason codes to the planner, stay in the turn.
       ask_user     -> show the question and end the turn.

A turn ends as incomplete if the planner fails, uses more than MAX_TOOL_CALLS_PER_TURN tool
calls, or takes more than MAX_STEPS_PER_TURN steps. The engine never invents a plan.

All engine state lives in the session's SQLite file, so any process can resume it.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import Field

from .context import proposal_view, render_context
from .models import (
    AskUser,
    CallTool,
    Change,
    Model,
    ModelInput,
    PlanningStep,
    PresentPlan,
    Proposal,
    ProposalPresented,
    QuestionAsked,
    TaskState,
    ToolObservationReceived,
    TranscriptEntry,
    dollars,
)
from .planners import Planner
from .prompts import ONE_SHOT_INSTRUCTIONS, TASK_INSTRUCTIONS
from .state import apply_logged, initial_state
from .store import RunStore
from .tools import AGENT_TOOLS, FIXTURES, ONE_SHOT_TOOLS, TOOL_SPECS, ToolBox, World
from .validation import Authority, validate_plan

MAX_TOOL_CALLS_PER_TURN = 8
MAX_STEPS_PER_TURN = 12

Implementation = Literal["one-shot", "transcript", "state"]


class EngineError(Exception):
    """An operation that does not fit the session's current phase."""


class SessionConfig(Model):
    session_id: str
    run_id: str
    implementation: Implementation
    scenario: str
    planner: Literal["simulated", "live"] = "simulated"
    provider: str | None = None
    strategy: Literal["student", "reference"] = "student"
    restated_brief: bool = False
    planner_settings: dict[str, Any] = Field(default_factory=dict)


class TurnOutcome(Model):
    turn: int
    kind: Literal["plan_presented", "question_asked", "incomplete"]
    proposal_id: str | None = None
    question: str | None = None
    unresolved_constraint: str | None = None
    reason: str | None = None
    steps: int = 0
    tool_calls: int = 0


class Cursor(Model):
    phase: Literal["ready", "awaiting_change", "complete"] = "ready"
    turn: int = 0
    next_turn_index: int = 0
    step_in_turn: int = 0
    tool_calls_in_turn: int = 0
    seq: int = 0
    tool_call_counter: int = 0
    model_call_counter: int = 0
    proposal_counter: int = 0
    purpose: Literal["respond_to_change", "reconsider_after_tool_result", "reconsider_after_rejection"] = "respond_to_change"
    trigger: str = "initial request"
    displayed_proposal: Proposal | None = None
    version: int = 0


def load_scenario(name: str) -> dict[str, Any]:
    path = FIXTURES / "scenarios" / f"{name}.json"
    if not path.exists():
        available = sorted(p.stem for p in (FIXTURES / "scenarios").glob("*.json"))
        raise ValueError(f"Unknown scenario {name!r}. Available: {', '.join(available)}")
    return json.loads(path.read_text())


def list_scenarios() -> list[dict[str, Any]]:
    out = []
    for p in sorted((FIXTURES / "scenarios").glob("*.json")):
        data = json.loads(p.read_text())
        out.append({"name": data["name"], "description": data["description"],
                    "turns": [{"turn": t["turn"], "source": t["source"], "text": t["text"]} for t in data["turns"]]})
    return out


@dataclass
class Strategy:
    name: str
    apply_change: Callable[[TaskState, Any], TaskState]
    build_context: Callable[[TaskState, PlanningStep], Any]
    module_file: str


def load_strategy(name: str) -> Strategy:
    """Import the student's or the reference functions, re-reading the file every time.

    Reloading means a long-running GUI server always runs the student's latest code.
    """
    module_name = {"student": "workshop.exercises", "reference": "solutions.reference"}[name]
    module = importlib.import_module(module_name)
    module = importlib.reload(module)
    return Strategy(name, module.apply_change, module.build_context, module.__file__ or module_name)


class Engine:
    def __init__(self, store: RunStore, config: SessionConfig, planner: Planner | None = None, strategy: Strategy | None = None):
        self.store = store
        self.config = config
        self.scenario = load_scenario(config.scenario)
        self.planner = planner
        self.strategy = strategy
        self.world = World.from_json(store.get("world")) if store.get("world") else World()
        self.cursor = Cursor.model_validate(store.get("cursor")) if store.get("cursor") else Cursor()
        self.state: TaskState | None = store.load_state()
        self.toolbox = ToolBox(self.world, self._validate_tool, self._read_history)

    # -- lifecycle --------------------------------------------------------------------

    def save(self) -> None:
        self.store.put("world", self.world.to_json())
        self.store.put("cursor", self.cursor.model_dump(mode="json"))
        self.store.put("config", self.config.model_dump(mode="json"))
        if self.state is not None:
            self.store.save_state(self.state)

    def start(self) -> dict[str, Any]:
        req = self.scenario["initial_request"]
        self.cursor = Cursor(trigger="initial request")
        if self.config.implementation != "one-shot":
            self._append_transcript(
                TranscriptEntry(turn=0, role="user", content=req["text"],
                                normalized={"kind": "initial_request", "requirements": req["requirements"], "display": req["text"]})
            )
        if self.config.implementation == "state":
            self.state = initial_state(self.config.run_id, req["requirements"])
            self.store.save_snapshot("initial", None, self.state)
        self.store.put("manifest_settings", self.config.planner_settings)
        self.save()
        return {"phase": self.cursor.phase, "turn": 0, "message": "Initial request applied."}

    # -- helpers ----------------------------------------------------------------------

    def authority(self) -> Authority:
        return Authority.from_changes(self.scenario["initial_request"]["requirements"], self.store.changes(), self.world)

    def _validate_tool(self, flight_quote_id: str, hotel_quote_id: str, claimed: int | None) -> dict[str, Any]:
        return validate_plan(self.world, self.authority(), flight_quote_id, hotel_quote_id, claimed).model_dump(mode="json")

    def _read_history(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        role, turn, contains = args.get("role", "any"), args.get("turn"), (args.get("contains") or "").lower()
        limit = int(args.get("limit") or 10)
        hits = []
        for e in self.store.transcript():
            if role != "any" and e.role != role:
                continue
            if turn is not None and e.turn != turn:
                continue
            if contains and contains not in e.content.lower():
                continue
            hits.append({"index": e.index, "turn": e.turn, "role": e.role, "content": e.content[:2000]})
        return hits[-limit:]

    def _append_transcript(self, entry: TranscriptEntry) -> TranscriptEntry:
        return self.store.append_transcript(entry)

    def _next_seq(self) -> int:
        self.cursor.seq += 1
        return self.cursor.seq

    def _record_and_apply(self, change: Change) -> None:
        self.store.record_change(change)
        if self.config.implementation == "state" and self.state is not None:
            assert self.strategy is not None, "state implementation needs a strategy"
            before = self.state
            self.state, applied = apply_logged(self.state, change, self.strategy.apply_change)
            if applied:
                self.store.save_snapshot(change.change_id, before, self.state)

    def _end_turn(self, outcome: TurnOutcome) -> None:
        outcome.steps = self.cursor.step_in_turn
        outcome.tool_calls = self.cursor.tool_calls_in_turn
        self.store.append_record("outcomes", outcome.model_dump(mode="json"))
        more = self.cursor.next_turn_index < len(self.scenario["turns"])
        self.cursor.phase = "awaiting_change" if more else "complete"

    def next_change(self) -> dict[str, Any] | None:
        if self.cursor.next_turn_index >= len(self.scenario["turns"]):
            return None
        return self.scenario["turns"][self.cursor.next_turn_index]

    # -- applying scenario changes ------------------------------------------------------

    def apply_next_change(self) -> dict[str, Any]:
        if self.cursor.phase != "awaiting_change":
            raise EngineError(f"Cannot apply a change while the session is {self.cursor.phase!r}.")
        spec = self.scenario["turns"][self.cursor.next_turn_index]
        turn = spec["turn"]
        self.world.clock.set_offset(spec.get("clock_offset_minutes", 0))
        expired: list[str] = []
        for event in spec.get("world_events", []):
            expired += self.world.apply_event(event)
        resolved, unresolved, notes = [], [], []
        displayed = self.cursor.displayed_proposal
        for raw in spec["changes"]:
            t = raw["type"]
            if t == "PartySizeChanged":
                resolved.append({"type": t, "adults": raw["adults"], "children": raw["children"]})
            elif t in ("HotelAccepted", "EarlierFlightRequested"):
                if displayed is None:
                    unresolved.append({"type": t, "reason": "no proposal was displayed in this run, so the reference cannot be resolved"})
                    continue
                if t == "HotelAccepted":
                    if raw.get("expected_hotel_id") and raw["expected_hotel_id"] != displayed.hotel_id:
                        notes.append(f"Scenario expected hotel {raw['expected_hotel_id']}; this run displayed {displayed.hotel_id}. Resolved to the displayed hotel.")
                    resolved.append({"type": t, "hotel_id": displayed.hotel_id, "proposal_id": displayed.proposal_id, "quote_id": displayed.hotel_quote_id})
                else:
                    arrival = self.world.quotes[displayed.flight_quote_id].arrive_local
                    resolved.append({"type": t, "reference_proposal_id": displayed.proposal_id, "reference_flight_id": displayed.flight_id,
                                     "reference_arrival_local": arrival})
            elif t == "QuoteExpired":
                for qid in sorted(q for q in expired if self.world.quotes[q].item_id == raw["item_id"]):
                    resolved.append({"type": t, "quote_id": qid, "item_id": raw["item_id"]})
            else:
                raise ValueError(f"Unknown scenario change type {t!r}")

        changes = []
        for k, ev in enumerate(resolved):
            changes.append(
                Change.model_validate({
                    "change_id": f"chg-t{turn}-{k}", "seq": self._next_seq(), "turn": turn, "source": spec["source"],
                    "message_id": f"msg-t{turn}", "text": spec["text"], "at": self.world.clock.now, "event": ev,
                })
            )
        normalized = {
            "kind": "changes",
            "display": spec["text"],
            "changes": [{"change_id": c.change_id, **c.event.model_dump(mode="json")} for c in changes],
        }
        if unresolved:
            normalized["unresolved"] = unresolved
        if notes:
            normalized["notes"] = notes
        content = spec["text"]
        if changes:
            content += "\n\nNormalized changes (resolved by the application):\n" + json.dumps(normalized["changes"], indent=2)
        self._append_transcript(TranscriptEntry(turn=turn, role="user" if spec["source"] == "user" else "notification",
                                                content=content, normalized=normalized))
        # Changes from one message are applied in order with no planning step between them.
        for change in changes:
            self._record_and_apply(change)
        for note in notes:
            self.store.append_record("notes", {"turn": turn, "note": note})
        self.cursor.next_turn_index += 1
        self.cursor.turn = turn
        self.cursor.phase = "ready"
        self.cursor.step_in_turn = 0
        self.cursor.tool_calls_in_turn = 0
        self.cursor.purpose = "respond_to_change"
        self.cursor.trigger = f"{spec['source']} message turn {turn}: " + ", ".join(c.change_id for c in changes)
        self.save()
        return {"turn": turn, "applied": [c.change_id for c in changes], "unresolved": unresolved, "notes": notes,
                "world_events": spec.get("world_events", [])}

    # -- model input ------------------------------------------------------------------

    def build_model_input(self) -> ModelInput:
        impl = self.config.implementation
        if impl == "transcript":
            return ModelInput(implementation=impl, system=TASK_INSTRUCTIONS, messages=self.store.transcript(),
                              tools=[TOOL_SPECS[n] for n in AGENT_TOOLS])
        if impl == "state":
            assert self.state is not None and self.strategy is not None
            step = PlanningStep(purpose=self.cursor.purpose, turn=self.cursor.turn, step=self.cursor.step_in_turn,
                                trigger=self.cursor.trigger, tool_calls_remaining=MAX_TOOL_CALLS_PER_TURN - self.cursor.tool_calls_in_turn)
            ctx = self.strategy.build_context(self.state, step)
            message = TranscriptEntry(turn=self.cursor.turn, role="user", content=render_context(ctx))
            return ModelInput(implementation=impl, system=TASK_INSTRUCTIONS, messages=[message],
                              tools=[TOOL_SPECS[n] for n in AGENT_TOOLS], context=ctx)
        return ModelInput(implementation=impl, system=ONE_SHOT_INSTRUCTIONS, messages=[self._one_shot_request()],
                          tools=[TOOL_SPECS[n] for n in ONE_SHOT_TOOLS])

    def _one_shot_request(self) -> TranscriptEntry:
        """Build the single message a one-shot call receives, and retain it in the transcript."""
        turn = self.cursor.turn
        existing = [e for e in self.store.transcript() if e.turn == turn and (e.normalized or {}).get("kind") == "one_shot_request"]
        if existing:
            return existing[-1]
        if turn == 0:
            text = self.scenario["initial_request"]["text"]
            requirements: dict[str, Any] | None = dict(self.scenario["initial_request"]["requirements"])
            arrive_before = None
        else:
            spec = self.scenario["turns"][self.cursor.next_turn_index - 1]
            text = spec["text"]
            requirements, arrive_before = None, None
            if self.config.restated_brief:
                # An external caller reconstructs every current requirement. Somebody has to.
                a = self.authority()
                requirements = {
                    "origin": a.origin, "destination": a.destination, "depart_date": str(a.depart_date), "return_date": str(a.return_date),
                    "nights": self.scenario["initial_request"]["requirements"]["nights"],
                    "party": a.party.model_dump() if a.party else None,
                    "budget": a.budget.model_dump() if a.budget else None,
                    "walk_preference": a.walk.model_dump() if a.walk else None,
                }
                if a.earlier_reference_arrival:
                    arrive_before = a.earlier_reference_arrival
                    requirements["earlier_arrival"] = {"arrive_before_local": a.earlier_reference_arrival,
                                                       "reference_proposal_id": a.earlier_reference_proposal_id}
                if a.accepted_hotel:
                    requirements["accepted_hotel"] = {"hotel_id": a.accepted_hotel.hotel_id, "accepted_total_cents": a.accepted_hotel.accepted_total_cents,
                                                      "accepted_quote_id": a.accepted_hotel.quote_id, "status": "accepted"}
                text = "[Brief restated by the calling application] " + text
        # Turn 0's request is the user's own words; later requests repeat a user message the transcript already shows.
        normalized: dict[str, Any] = {"kind": "one_shot_request", "requirements": requirements,
                                      ("display" if turn == 0 else "caller_note"): text}
        if requirements and requirements.get("party"):
            from .models import Party

            party = Party(**requirements["party"])
            self.cursor.tool_call_counter += 1
            call_id = f"app-{self.cursor.tool_call_counter:04d}"
            a = self.authority()
            snap = self.toolbox.snapshot(a.origin or "", a.destination or "", party, a.depart_date, a.return_date, arrive_before, call_id)
            self.store.append_record("tool_calls", {"tool_call_id": call_id, "turn": turn, "step": 0, "caller": "application",
                                                    "name": "inventory_snapshot", "arguments": {"party": party.model_dump(), "arrive_before": arrive_before},
                                                    "result": snap, "at": self.world.clock.now.isoformat()})
            normalized["inventory_snapshot"] = {"quotes": snap["flights"]["quotes"] + snap["hotels"]["quotes"],
                                                "excluded": snap["flights"]["excluded"] + snap["hotels"]["ineligible"]}
        else:
            normalized["changes"] = [
                {"change_id": c.change_id, **c.event.model_dump(mode="json")} for c in self.store.changes() if c.turn == turn and c.source in ("user", "world")
            ]
        content = text + "\n\n" + json.dumps({k: v for k, v in normalized.items() if k not in ("kind", "display")}, indent=2, default=str)
        return self._append_transcript(TranscriptEntry(turn=turn, role="user", content=content, normalized=normalized))

    # -- stepping ---------------------------------------------------------------------

    def step(self) -> dict[str, Any]:
        if self.cursor.phase != "ready":
            raise EngineError(f"Cannot step while the session is {self.cursor.phase!r}.")
        if self.planner is None:
            raise EngineError("No planner configured.")
        turn = self.cursor.turn
        if self.cursor.step_in_turn >= MAX_STEPS_PER_TURN:
            self._end_turn(TurnOutcome(turn=turn, kind="incomplete", reason=f"Step limit ({MAX_STEPS_PER_TURN}) reached without a validated plan or question."))
            self.save()
            return {"turn": turn, "result": "incomplete", "reason": "step limit"}

        model_input = self.build_model_input()
        self.cursor.model_call_counter += 1
        self.cursor.tool_call_counter += 1
        default_call_id = f"tc-{self.cursor.tool_call_counter:04d}"
        response = self.planner.next_action(model_input, default_call_id)
        step_index = self.cursor.step_in_turn
        self.cursor.step_in_turn += 1

        rendered_chars = len(model_input.system) + sum(len(m.content) for m in model_input.messages)
        self.store.append_record("model_calls", {
            "index": self.cursor.model_call_counter, "turn": turn, "step": step_index,
            "implementation": self.config.implementation, "planner_mode": self.planner.mode, "settings": self.planner.settings(),
            "purpose": self.cursor.purpose, "trigger": self.cursor.trigger,
            "input": model_input.model_dump(mode="json"), "input_chars": rendered_chars,
            "provider_request": response.provider_request,
            "action": response.action.model_dump(mode="json") if response.action else None,
            "action_call_id": response.action_call_id, "attempts": response.attempts, "errors": response.errors,
            "usage": response.usage.model_dump() if response.usage else None, "latency_ms": response.latency_ms,
        })
        for entry in response.transcript_additions:
            self._append_transcript(entry.model_copy(update={"turn": turn}))

        action = response.action
        call_id = response.action_call_id
        if action is None:
            self._end_turn(TurnOutcome(turn=turn, kind="incomplete", reason="Planner did not return a valid action: " + "; ".join(response.errors)))
            self.save()
            return {"turn": turn, "result": "incomplete", "errors": response.errors}

        if self.config.implementation == "one-shot" and isinstance(action, CallTool):
            self._end_turn(TurnOutcome(turn=turn, kind="incomplete", reason=f"One-shot planner requested tool {action.name!r}; no tool loop exists in this mode."))
            self.save()
            return {"turn": turn, "result": "incomplete"}

        if isinstance(action, CallTool):
            return self._do_tool(action, call_id, turn, step_index, model_input)
        if isinstance(action, PresentPlan):
            return self._do_present(action, call_id, turn, step_index)
        return self._do_ask(action, call_id, turn, step_index)

    def _do_tool(self, action: CallTool, call_id: str, turn: int, step_index: int, model_input: ModelInput) -> dict[str, Any]:
        if self.cursor.tool_calls_in_turn >= MAX_TOOL_CALLS_PER_TURN:
            self._append_transcript(TranscriptEntry(turn=turn, role="tool", tool_call_id=call_id, content="Error: tool-call budget for this turn is exhausted."))
            self._end_turn(TurnOutcome(turn=turn, kind="incomplete", reason=f"Tool-call limit ({MAX_TOOL_CALLS_PER_TURN}) exhausted without a validated plan or question."))
            self.save()
            return {"turn": turn, "result": "incomplete", "reason": "tool limit"}
        self.cursor.tool_calls_in_turn += 1
        allowed = {t.name for t in model_input.tools}
        if action.name not in allowed:
            result = {"ok": False, "tool": action.name, "error": f"Tool {action.name!r} is not available in this mode."}
        else:
            result = self.toolbox.execute(action.name, action.arguments, call_id)
        self.store.append_record("tool_calls", {"tool_call_id": call_id, "turn": turn, "step": step_index, "caller": "agent",
                                                "name": action.name, "arguments": action.arguments, "result": result,
                                                "at": self.world.clock.now.isoformat()})
        if action.name == "validate_plan" and result.get("ok"):
            self.store.append_record("validations", {"source": "agent_tool", "tool_call_id": call_id, "turn": turn, "result": result["validation"]})
        self._append_transcript(TranscriptEntry(turn=turn, role="tool", tool_call_id=call_id, content=json.dumps(result, default=str), normalized=result))
        self._record_and_apply(Change.model_validate({
            "change_id": f"obs-{call_id}", "seq": self._next_seq(), "turn": turn, "source": "tool", "at": self.world.clock.now,
            "event": ToolObservationReceived(tool_call_id=call_id, tool_name=action.name, arguments=action.arguments, payload=result).model_dump(mode="json"),
        }))
        self.cursor.purpose = "reconsider_after_tool_result"
        self.cursor.trigger = f"tool result {call_id} ({action.name})"
        self.save()
        return {"turn": turn, "result": "tool_called", "tool": action.name, "tool_call_id": call_id, "ok": result.get("ok")}

    def _do_present(self, action: PresentPlan, call_id: str, turn: int, step_index: int) -> dict[str, Any]:
        authority = self.authority()
        v = validate_plan(self.world, authority, action.plan.flight_quote_id, action.plan.hotel_quote_id, action.plan.claimed_total_cents)
        self.store.append_record("validations", {"source": "runner_gate", "tool_call_id": call_id, "turn": turn, "result": v.model_dump(mode="json")})
        if v.compliant:
            self.cursor.proposal_counter += 1
            proposal = Proposal(
                proposal_id=f"plan-{self.cursor.proposal_counter:03d}", turn=turn,
                flight_id=v.flight_id or "", hotel_id=v.hotel_id or "",
                flight_quote_id=v.flight_quote_id, hotel_quote_id=v.hotel_quote_id,
                requirement_revision=authority.revision, computed_total_cents=v.computed_total_cents,
                claimed_total_cents=action.plan.claimed_total_cents, validation=v,
                confirmation_status="reconfirmation_required" if v.confirmation_required else "pending_user_review",
                explanation=action.explanation, presented_at=self.world.clock.now,
            )
            self.cursor.displayed_proposal = proposal
            self.store.append_record("plans", {"turn": turn, "step": step_index, "tool_call_id": call_id, "displayed": True, "proposal": proposal.model_dump(mode="json")})
            summary = (f"Displayed to the user as {proposal.proposal_id}. Computed total {dollars(v.computed_total_cents)}. "
                       f"Confirmation status: {proposal.confirmation_status}. Reason codes: {', '.join(v.codes()) or 'none'}.")
            self._append_transcript(TranscriptEntry(turn=turn, role="tool", tool_call_id=call_id, content=summary,
                                                    normalized={"displayed_proposal": proposal_view(proposal), "validation": v.model_dump(mode="json")}))
            self._record_and_apply(Change.model_validate({
                "change_id": f"presented-{proposal.proposal_id}", "seq": self._next_seq(), "turn": turn, "source": "agent",
                "at": self.world.clock.now, "event": ProposalPresented(proposal=proposal).model_dump(mode="json"),
            }))
            self._end_turn(TurnOutcome(turn=turn, kind="plan_presented", proposal_id=proposal.proposal_id))
            self.save()
            return {"turn": turn, "result": "plan_presented", "proposal_id": proposal.proposal_id, "total_cents": v.computed_total_cents,
                    "confirmation_status": proposal.confirmation_status}

        self.store.append_record("plans", {"turn": turn, "step": step_index, "tool_call_id": call_id, "displayed": False,
                                           "draft": action.plan.model_dump(), "explanation": action.explanation, "validation": v.model_dump(mode="json")})
        summary = "Not shown to the user. Validation failed: " + "; ".join(f"{r.code.value}: {r.message}" for r in v.reasons)
        self._append_transcript(TranscriptEntry(turn=turn, role="tool", tool_call_id=call_id, content=summary,
                                                normalized={"validation": v.model_dump(mode="json"), "rejected_plan": True}))
        if self.config.implementation == "one-shot":
            self._end_turn(TurnOutcome(turn=turn, kind="incomplete", reason="One-shot plan failed validation: " + ", ".join(v.codes())))
            self.save()
            return {"turn": turn, "result": "incomplete", "codes": v.codes()}
        self._record_and_apply(Change.model_validate({
            "change_id": f"obs-{call_id}", "seq": self._next_seq(), "turn": turn, "source": "tool", "at": self.world.clock.now,
            "event": ToolObservationReceived(tool_call_id=call_id, tool_name="present_plan", arguments=action.plan.model_dump(),
                                             payload={"ok": True, "tool": "present_plan", "validation": v.model_dump(mode="json")}).model_dump(mode="json"),
        }))
        self.cursor.purpose = "reconsider_after_rejection"
        self.cursor.trigger = f"plan rejected by validator ({', '.join(v.codes())})"
        self.save()
        return {"turn": turn, "result": "plan_rejected", "codes": v.codes()}

    def _do_ask(self, action: AskUser, call_id: str, turn: int, step_index: int) -> dict[str, Any]:
        self._append_transcript(TranscriptEntry(turn=turn, role="tool", tool_call_id=call_id, content="Question shown to the user. Waiting for their reply.",
                                                normalized={"question": action.question, "unresolved_constraint": action.unresolved_constraint}))
        self._record_and_apply(Change.model_validate({
            "change_id": f"question-t{turn}-s{step_index}", "seq": self._next_seq(), "turn": turn, "source": "agent",
            "at": self.world.clock.now, "event": QuestionAsked(question=action.question, unresolved_constraint=action.unresolved_constraint).model_dump(mode="json"),
        }))
        self._end_turn(TurnOutcome(turn=turn, kind="question_asked", question=action.question, unresolved_constraint=action.unresolved_constraint))
        self.save()
        return {"turn": turn, "result": "question_asked", "question": action.question}

    # -- composite operations ----------------------------------------------------------

    def run_turn(self) -> list[dict[str, Any]]:
        results = []
        while self.cursor.phase == "ready":
            results.append(self.step())
        return results

    def run_to_completion(self, pause: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        while self.cursor.phase != "complete":
            if self.cursor.phase == "awaiting_change":
                applied = self.apply_next_change()
                results.append({"result": "change_applied", **applied})
                if pause:
                    pause(applied)
            results.extend(self.run_turn())
        return results
