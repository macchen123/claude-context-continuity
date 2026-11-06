"""Regressions for the observed rollover and hook-output failures."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import agent_metadata, continuity, core, diagnostics, tui_runtime
from claude_context_continuity.context_runtime import ContextRuntime
from claude_context_continuity.result_budget import bound_result


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    workspace = root / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(core, "HOME", root / "state")
    config = {"hash": "fixture-config", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
    return tui_runtime.TuiRuntime.create(
        cwd=workspace,
        session_id=str(uuid4()),
        configuration=config,
        configuration_reader=lambda _: config,
    )


def start(runtime, sid, source, origin="startup"):
    return runtime.on_hook({
        "hook_event_name": "SessionStart", "session_id": sid,
        "cwd": runtime._state()["cwd"], "source": origin,
        "transcript_path": str(source),
    })


def test_session_start_does_not_restore_historical_agents(runtime, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(agent_metadata, "synchronize", lambda *args, **kwargs: (
        calls.append(kwargs) or {"status": "verified", "restored": 0, "blocked": []}
    ))
    old_sid = runtime._state()["session_id"]
    old_source = tmp_path.resolve() / f"{old_sid}.jsonl"
    old_source.write_text("")
    start(runtime, old_sid, old_source)
    new_sid = str(uuid4())
    new_source = tmp_path.resolve() / f"{new_sid}.jsonl"
    start(runtime, new_sid, new_source, "clear")
    assert runtime._state()["session_id"] == new_sid
    assert runtime._state()["phase"] == "running"
    assert calls == []


def test_startup_catalogue_waits_for_native_first_write(runtime, tmp_path):
    sid = runtime._state()["session_id"]
    source = tmp_path.resolve() / f"{sid}.jsonl"
    start(runtime, sid, source)
    assert list((runtime.directory / "history").glob("*.json")) == []
    source.write_text(json.dumps({
        "type": "user", "uuid": "user-fixture", "sessionId": sid,
        "message": {"role": "user", "content": "Continue the authorized task."},
    }) + "\n")
    runtime.on_hook({"hook_event_name": "PreToolUse", "session_id": sid,
                    "cwd": runtime._state()["cwd"], "transcript_path": str(source),
                    "tool_use_id": "first-history-probe"})
    entries = [core.read_json(p) for p in (runtime.directory / "history").glob("*.json")]
    assert entries == [{"generation": 0, "session_id": sid, "source_path": str(source)}]


def test_new_wait_reason_does_not_display_previous_diagnostic():
    state = {"phase": "running", "session_id": str(uuid4()),
             "reconciliation": {"stage": "running", "reason": "hook_observation_failed"},
             "diagnostic": "ContinuityError: previous credential rejection"}
    ContextRuntime._wait(state, "input_session_identity_pending")
    result = ContextRuntime._hook_output(state, "UserPromptSubmit")
    assert "input_session_identity_pending" in result["systemMessage"]
    assert "credential rejection" not in result["systemMessage"]
    assert state["last_reconciliation"]["diagnostic"].endswith("previous credential rejection")


@pytest.mark.parametrize("failure", [core.LockBusy("fixture lock contention"), OSError("fixture I/O failure")])
def test_send_message_dispatch_retains_actual_failure(runtime, monkeypatch, failure):
    monkeypatch.setenv("CLAUDE_CONTINUITY_ID", runtime.conversation_id)
    event = {"hook_event_name": "PreToolUse", "session_id": runtime._state()["session_id"],
             "tool_name": "SendMessage", "tool_input": {"to": "a1234567890", "message": "fixture"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    def fail_load(*args, **kwargs):
        raise failure
    monkeypatch.setattr(tui_runtime.TuiRuntime, "load", fail_load)
    args = continuity.parser().parse_args(["tui-hook", "--context-id", runtime.conversation_id])
    result = continuity.dispatch(args)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert type(failure).__name__ in reason
    events = [json.loads(line) for line in (runtime.directory / diagnostics.EVENT_FILE).read_text().splitlines()]
    assert any(row["event"] == "hook_failure" and row["error_type"] == type(failure).__name__ for row in events)


def test_agent_recovery_preparation_does_not_hold_parent_state_lock(runtime, tmp_path, monkeypatch):
    sid = runtime._state()["session_id"]
    source = tmp_path.resolve() / f"{sid}.jsonl"
    source.write_text("")
    start(runtime, sid, source)
    prepared = []
    def synchronize(*args, **kwargs):
        with core.lock(runtime.lock_path):
            prepared.append(True)
        return {"status": "verified", "restored": 0, "blocked": [], "agents": []}
    monkeypatch.setattr(agent_metadata, "synchronize", synchronize)
    result = runtime.on_hook({
        "hook_event_name": "PreToolUse", "session_id": sid, "cwd": runtime._state()["cwd"],
        "transcript_path": str(source), "tool_use_id": "target-message", "tool_name": "SendMessage",
        "tool_input": {"to": "a1234567890", "message": "Continue."},
    })
    assert prepared == [True]
    assert result.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_agent_preparation_cannot_commit_over_a_new_native_epoch(runtime, tmp_path, monkeypatch):
    sid = runtime._state()["session_id"]
    source = tmp_path.resolve() / f"{sid}.jsonl"
    source.write_text("")
    start(runtime, sid, source)
    new_sid = str(uuid4())
    new_source = tmp_path.resolve() / f"{new_sid}.jsonl"
    def synchronize(*args, **kwargs):
        start(runtime, new_sid, new_source, "clear")
        return {"status": "verified", "restored": 0, "blocked": [], "agents": []}
    monkeypatch.setattr(agent_metadata, "synchronize", synchronize)
    result = runtime.on_hook({
        "hook_event_name": "PreToolUse", "session_id": sid, "cwd": runtime._state()["cwd"],
        "transcript_path": str(source), "tool_use_id": "old-target-message", "tool_name": "SendMessage",
        "tool_input": {"to": "a1234567890", "message": "Continue."},
    })
    assert runtime._state()["session_id"] == new_sid
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "native_session_identity_mismatch" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_lifecycle_reads_start_at_the_recorded_launch_window(runtime, tmp_path, monkeypatch):
    sid = runtime._state()["session_id"]
    source = tmp_path.resolve() / f"{sid}.jsonl"
    source.write_text("")
    start(runtime, sid, source)
    state = runtime._state()
    agent = "a1234567890"
    state.update(window_generation=1, active_child_handles=[agent],
                 child_handle_sources={agent: [sid, str(source)]})
    unrelated_sid = str(uuid4())
    entries = [
        {"session_id": unrelated_sid, "source_path": str(tmp_path / f"{unrelated_sid}.jsonl"), "generation": 0},
        {"session_id": sid, "source_path": str(source), "generation": 1},
    ]
    monkeypatch.setattr(runtime, "_catalogue_entries", lambda _: entries)
    def unexpected_source(*args):
        raise AssertionError("unrelated pre-launch history was read")
    monkeypatch.setattr(tui_runtime, "_source", unexpected_source)
    runtime._reconcile_lifecycle(state, {
        "pending_tools": {}, "background_handles": {}, "settled_tool_ids": [],
        "terminal_background_handles": [], "background_lifecycle": {},
    })
    assert state["active_child_handles"] == [agent]


def test_bounded_stdout_preserves_empty_stderr(tmp_path):
    response = {"stdout": "large successful output\n" * 2000, "stderr": "",
                "interrupted": False, "isImage": False, "noOutputExpected": False}
    result = bound_result("Bash", response, directory=tmp_path.resolve(), byte_limit=16000)
    assert result["replaced"] is True
    assert result["response"]["stderr"] == ""
    assert result["response"]["interrupted"] is False
