"""Native limit recovery with isolated histories; no real sessions or model calls."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import core, native_control, tmux_transport, tui_runtime
from claude_context_continuity.context_runtime import observe_budget
from claude_context_continuity.notes import NotesStore
from claude_context_continuity.tui_runtime import TuiRuntime


@dataclass
class RuntimeFixture:
    root: Path
    cwd: Path
    runtime: TuiRuntime
    sid: str
    source: Path
    configuration: dict
    clear: Mock
    continuation: Mock

    def append(self, *records):
        with self.source.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def user(self, text, **fields):
        return {"type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
                "message": {"role": "user", "content": text}, **fields}

    def assistant(self, tokens=125, *, cache=0, output=0, tools=()):
        content = ([{"type": "tool_use", "id": tool, "name": "Read", "input": {}} for tool in tools]
                   if tools else [{"type": "text", "text": "fixture response"}])
        return {"type": "assistant", "uuid": str(uuid4()), "sessionId": self.sid,
                "cwd": str(self.cwd), "message": {"id": str(uuid4()), "role": "assistant",
                    "content": content, "model": "fixture-native-model", "usage": {
                        "input_tokens": tokens, "cache_read_input_tokens": cache,
                        "cache_creation_input_tokens": 0, "output_tokens": output}}}

    def failure(self, text="Prompt is too long", error="invalid_request"):
        record = self.assistant(0)
        record.update(isApiErrorMessage=True, error=error)
        record["message"].update(model="<synthetic>", content=[{"type": "text", "text": text}])
        return record

    def hook(self, name, **fields):
        return {"hook_event_name": name, "session_id": self.sid, "cwd": str(self.cwd),
                "transcript_path": str(self.source), **fields}

    def rejected(self):
        return self.runtime.on_hook(self.hook("StopFailure", error="invalid_request",
                                             last_assistant_message="Prompt is too long"))

    def clear_epoch(self):
        old_sid, old_source = self.sid, self.source
        self.sid = str(uuid4())
        self.source = self.root / f"{self.sid}.jsonl"
        self.source.write_text("", encoding="utf-8")
        self.runtime.on_hook(self.hook("SessionStart", source="clear"))
        return old_sid, old_source

    def sidecar(self, agent, *, sid=None, stopped=False):
        path = self.root / (sid or self.sid) / "subagents" / f"agent-{agent}.meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"agentType": "implementation-worker", "model": "fixture-worker-model",
            "toolUseId": "fixture-launch", "spawnDepth": 1, "requestShape": "background",
            "requestNonInteractive": True, "stoppedByUser": stopped}), encoding="utf-8")
        return path


@pytest.fixture
def managed_runtime(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    cwd = root / "workspace"
    cwd.mkdir()
    monkeypatch.setattr(core, "HOME", root / "state")
    configuration = {"hash": "isolated-rollover", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
    sid = str(uuid4())
    runtime = TuiRuntime.create(cwd=cwd, session_id=sid, configuration=configuration,
                                configuration_reader=lambda _: configuration)
    source = root / f"{sid}.jsonl"
    clear, continuation = Mock(), Mock()
    fixture = RuntimeFixture(root, cwd, runtime, sid, source, configuration, clear, continuation)
    fixture.append(fixture.user("Continue only the authorized fixture; preserve completed work."), fixture.assistant())
    runtime.on_hook(fixture.hook("SessionStart", source="startup"))
    binding = {"pane_pid": os.getpid()}
    control = {"protocol": native_control.PROTOCOL, "context_id": runtime.conversation_id,
               "socket_path": str(root / "isolated-control.sock")}
    with core.lock(runtime.lock_path):
        state = runtime._state()
        state.update(transport="tmux_tui", tmux=binding, native_control=control)
        runtime._save(state)
    runtime._control_auth = "isolated-fixture-control-authentication-123456"
    monkeypatch.setattr(tmux_transport, "inspect", lambda _: binding)
    monkeypatch.setattr(native_control, "ready", lambda _: True)
    monkeypatch.setattr(native_control, "send_clear", clear)
    monkeypatch.setattr(native_control, "send_continuation", continuation)
    return fixture


def test_configured_window_reserves_a_quarter_without_claiming_actual_capacity():
    result = observe_budget(None, {"total_input_tokens": 511635, "output_tokens": 1222,
                                  "cache_fields_complete": True}, 600000, 12)
    assert result["near_limit"]
    assert result["guard_tokens"] == 150000
    assert result["threshold_tokens"] == 450000
    assert result["remaining_tokens"] == 87143
    assert not result["window_is_actual"]
    assert "max_positive_growth_tokens" not in result["observations"]


def test_observed_native_window_is_not_replaced_by_a_larger_estimate(managed_runtime):
    f = managed_runtime
    f.configuration["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "600000"
    f.append(f.assistant(125000, cache=25000, output=1000))
    result = f.runtime.on_hook(f.hook("PostToolBatch", contextWindow=200000, tool_calls=[]))
    assert result["continue"] is False
    receipt = f.runtime.receipt()
    assert receipt["usage"]["native_context_window"] == 200000
    assert receipt["usage"]["total_context_tokens"] == 151000
    assert receipt["budget"]["window_is_actual"] == 1
    assert f.runtime.advance()["phase"] == "clear_sent"
    f.clear.assert_called_once()


def test_reported_failure_batch_rotates_before_another_model_request(managed_runtime):
    f = managed_runtime
    f.configuration["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "600000"
    call = "skill-expansion"
    request = f.assistant(4755, cache=506880, output=1222, tools=[call])
    request["message"]["content"][0]["name"] = "Skill"
    expansion = f.user("fixture skill material\n" * 14000, isMeta=True, sourceToolUseID=call)
    f.append(request, f.user([{"type": "tool_result", "tool_use_id": call, "content": "Skill launched"}]), expansion)
    f.runtime.on_hook(f.hook("SubagentStart", agent_id="a1234567890abcdef"))
    result = f.runtime.on_hook(f.hook("PostToolBatch", tool_calls=[{"tool_use_id": call}]))
    assert result["continue"] is False
    assert f.runtime.receipt()["usage"]["total_context_tokens"] == 512857
    assert f.runtime.advance()["phase"] == "clear_sent"
    assert f.runtime.receipt()["active_child_handles"] == ["a1234567890abcdef"]
    f.clear.assert_called_once()


def test_stop_failure_is_loaded_in_the_session_plugin():
    cid = str(uuid4())
    assert "StopFailure" in tui_runtime._plugin_manifest(cid)["hooks"]


def test_limit_failure_hook_uses_the_single_existing_observer_repair_entry(managed_runtime, monkeypatch):
    f = managed_runtime
    with core.lock(f.runtime.lock_path):
        state = f.runtime._state()
        state["controller_managed"] = True
        f.runtime._save(state)
    repair = Mock(return_value={"fixture": "observer repaired"})
    monkeypatch.setattr(tui_runtime, "_start_controller", repair)
    outcome = tui_runtime.repair_controller_from_hook(f.runtime.conversation_id,
        f.hook("StopFailure", error="invalid_request", last_assistant_message="Prompt is too long"))
    assert outcome == {"fixture": "observer repaired"}
    assert repair.call_count == 1
    assert repair.call_args.kwargs["automatic"] is True
    f.clear.assert_not_called()


def test_controller_version_receipt_is_bound_to_the_registered_pid(managed_runtime, monkeypatch):
    from claude_context_continuity import __version__
    f = managed_runtime
    monkeypatch.setattr(TuiRuntime, "advance", lambda _: {"phase": "closed"})
    tui_runtime.serve(f.runtime.conversation_id)
    assert f.runtime.receipt()["controller_package"] == {"pid": os.getpid(), "version": __version__}
    with core.lock(f.runtime.lock_path):
        state = f.runtime._state()
        state["controller_pid"] = os.getpid() + 1
        f.runtime._save(state)
    assert f.runtime.receipt()["controller_package"] is None


def test_verified_native_limit_failure_rotates_below_estimated_threshold(managed_runtime):
    f = managed_runtime
    failed = f.failure()
    f.append(failed)
    f.rejected()
    outcome = f.runtime.advance()
    assert outcome["phase"] == "clear_sent"
    assert outcome["automatic_rotations"] == 1
    assert outcome["usage"]["total_context_tokens"] == 125
    detail = json.loads(NotesStore(f.runtime.directory).read("details-1")["text"])
    assert detail["trigger"] == "native_context_rejection"
    assert detail["rejection_locator"]["message_id"] == failed["uuid"]
    f.runtime.advance()
    f.clear.assert_called_once()
    f.clear_epoch()
    assert f.runtime.advance()["phase"] == "awaiting_continuation"
    text = f.continuation.call_args.args[2]
    assert text.startswith("<continuity-host-event>\n")
    assert "not a new human instruction" in text
    f.append(f.user(text))
    assert f.runtime.advance()["phase"] == "running"
    assert f.runtime.receipt()["continuation_observed"]
    f.continuation.assert_called_once()


def test_failure_boundary_waits_for_its_complete_native_record(managed_runtime):
    f = managed_runtime
    failed = f.failure()
    raw = (json.dumps(failed) + "\n").encode()
    f.rejected()
    assert f.runtime.advance()["phase"] == "running"
    f.clear.assert_not_called()
    with f.source.open("ab") as handle:
        handle.write(raw[:80])
    f.runtime.advance()
    f.clear.assert_not_called()
    with f.source.open("ab") as handle:
        handle.write(raw[80:])
    assert f.runtime.advance()["phase"] == "clear_sent"
    f.clear.assert_called_once()


@pytest.mark.parametrize("error,text", [
    ("rate_limit", "Rate limit reached"),
    ("invalid_request", "Invalid request header"),
    ("authentication_failed", "Prompt is too long"),
])
def test_unrelated_failures_never_clear_the_session(managed_runtime, error, text):
    f = managed_runtime
    f.append(f.failure(text, error))
    f.runtime.on_hook(f.hook("StopFailure", error=error, last_assistant_message=text))
    f.runtime.advance()
    f.clear.assert_not_called()
    assert not f.runtime.receipt()["rotation_pending"]


def test_failure_hook_alone_does_not_prove_a_safe_clear(managed_runtime):
    f = managed_runtime
    f.rejected()
    f.runtime.advance()
    f.clear.assert_not_called()
    f.append(f.failure(), f.assistant(250))
    f.runtime.advance()
    f.clear.assert_not_called()


def test_fresh_input_invalidates_a_prior_failure_boundary(managed_runtime):
    f = managed_runtime
    f.append(f.failure())
    f.rejected()
    f.runtime.on_hook(f.hook("UserPromptSubmit", prompt="A new correction"))
    f.append(f.user("A new correction"))
    f.runtime.advance()
    f.clear.assert_not_called()


@pytest.mark.parametrize("prompt_ids", [False, True])
def test_late_failure_cannot_clear_input_before_its_history_append(managed_runtime, prompt_ids):
    f = managed_runtime
    f.append(f.failure())
    f.rejected()
    fresh = {"prompt_id": "fresh-native-prompt"} if prompt_ids else {}
    stale = {"prompt_id": "old-native-prompt"} if prompt_ids else {}
    f.runtime.on_hook(f.hook("UserPromptSubmit", prompt="Unflushed correction", **fresh))
    f.runtime.on_hook(f.hook("StopFailure", error="invalid_request",
                            last_assistant_message="Prompt is too long", **stale))
    f.runtime.advance()
    f.clear.assert_not_called()


def test_failure_recovery_waits_for_foreground_tools_but_not_background_agents(managed_runtime):
    f = managed_runtime
    call = "foreground"
    f.append(f.assistant(tools=[call]))
    f.runtime.on_hook(f.hook("PreToolUse", tool_use_id=call))
    f.runtime.on_hook(f.hook("SubagentStart", agent_id="a1234567890abcdef"))
    f.append(f.failure())
    f.rejected()
    assert f.runtime.advance()["phase"] == "waiting_safe_boundary"
    f.clear.assert_not_called()
    f.append(f.user([{"type": "tool_result", "tool_use_id": call, "content": "settled"}]), f.failure())
    f.rejected()
    assert f.runtime.advance()["phase"] == "clear_sent"
    assert f.runtime.receipt()["active_child_handles"] == ["a1234567890abcdef"]


def test_unknown_failure_clear_delivery_is_never_resent(managed_runtime):
    f = managed_runtime
    f.clear.side_effect = native_control.NativeControlError("fixture delivery unknown")
    f.append(f.failure())
    f.rejected()
    assert f.runtime.advance()["phase"] == "clear_sent"
    for _ in range(3):
        f.rejected()
        f.runtime.advance()
    f.clear.assert_called_once()
    assert f.runtime.receipt()["reconciliation"] is not None


def test_late_child_stop_settles_its_inherited_handle_across_clears(managed_runtime):
    f = managed_runtime
    child = "a1234567890abcdef"
    f.runtime.on_hook(f.hook("SubagentStart", agent_id=child))
    old_sid, old_source = f.clear_epoch()
    f.clear_epoch()
    for _ in range(2):
        f.runtime.on_hook(f.hook("SubagentStop", session_id=old_sid,
                                transcript_path=str(old_source), agent_id=child))
    assert f.runtime.receipt()["active_child_handles"] == []
    assert f.runtime.receipt()["reconciliation"] is None
    f.clear.assert_not_called()


def test_old_stop_does_not_settle_a_new_same_handle_execution(managed_runtime):
    f = managed_runtime
    child = "a1234567890abcdef"
    f.runtime.on_hook(f.hook("SubagentStart", agent_id=child))
    old_sid, old_source = f.clear_epoch()
    f.runtime.on_hook(f.hook("SubagentStart", agent_id=child))
    f.runtime.on_hook(f.hook("SubagentStop", session_id=old_sid,
                            transcript_path=str(old_source), agent_id=child))
    assert f.runtime.receipt()["active_child_handles"] == [child]
    f.runtime.on_hook(f.hook("SubagentStop", agent_id=child))
    assert f.runtime.receipt()["active_child_handles"] == []


def test_stopped_native_sidecar_is_not_reported_as_live_after_clear(managed_runtime):
    f = managed_runtime
    stopped, live = "a1234567890abcdef", "aabcdef1234567890"
    for child in (stopped, live):
        f.runtime.on_hook(f.hook("SubagentStart", agent_id=child))
        f.sidecar(child, stopped=child == stopped)
    old_stopped = f.sidecar(stopped, stopped=True)
    before = old_stopped.read_bytes()
    f.clear_epoch()
    assert f.runtime.receipt()["active_child_handles"] == [live]
    assert old_stopped.read_bytes() == before
    assert json.loads((f.root / f.sid / "subagents" / old_stopped.name).read_text())["stoppedByUser"]
    f.clear.assert_not_called()


def test_failure_without_a_successful_usage_sample_waits_without_reset_loop(managed_runtime):
    f = managed_runtime
    f.clear_epoch()
    f.append(f.user("An oversized first input"), f.failure())
    f.rejected()
    for _ in range(3):
        result = f.runtime.advance()
    assert result["reconciliation"]["reason"] == "context_rejection_without_native_usage"
    assert result["automatic_rotations"] == 0
    f.clear.assert_not_called()


@pytest.mark.parametrize("change", ["not_synthetic", "not_native_error", "wrong_text"])
def test_failure_boundary_requires_matching_native_error_evidence(managed_runtime, change):
    f = managed_runtime
    record = f.failure()
    if change == "not_synthetic":
        record["message"]["model"] = "fixture-native-model"
    elif change == "not_native_error":
        record.pop("isApiErrorMessage")
    else:
        record["message"]["content"][0]["text"] = "Some other failure"
    f.append(record)
    f.rejected()
    f.runtime.advance()
    f.clear.assert_not_called()


def test_child_failure_does_not_change_parent_budget_or_boundary(managed_runtime):
    f = managed_runtime
    before = f.runtime.receipt()
    f.runtime.on_hook(f.hook("StopFailure", agent_id="a1234567890abcdef", error="invalid_request",
                            last_assistant_message="Prompt is too long"))
    f.runtime.advance()
    assert f.runtime.receipt()["usage"] == before["usage"]
    assert f.runtime.receipt()["phase"] == before["phase"]
    f.clear.assert_not_called()


def test_unreadable_stop_metadata_never_implies_child_completion(managed_runtime):
    f = managed_runtime
    child = "a1234567890abcdef"
    f.runtime.on_hook(f.hook("SubagentStart", agent_id=child))
    path = f.sidecar(child, stopped=True)
    path.write_text("{incomplete", encoding="utf-8")
    f.runtime.on_hook(f.hook("Stop", last_assistant_message="fixture response"))
    f.runtime.advance()
    assert f.runtime.receipt()["active_child_handles"] == [child]
    f.clear.assert_not_called()


def test_stopped_sidecar_settles_at_next_boundary_without_resuming_any_agent(managed_runtime):
    f = managed_runtime
    child = "a1234567890abcdef"
    f.runtime.on_hook(f.hook("SubagentStart", agent_id=child))
    f.sidecar(child, stopped=True)
    f.runtime.on_hook(f.hook("Stop", last_assistant_message="fixture response"))
    f.runtime.advance()
    assert f.runtime.receipt()["active_child_handles"] == []
    f.clear.assert_not_called()
    f.continuation.assert_not_called()
