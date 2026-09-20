"""Focused regressions for automatic native-context reconciliation.

These tests use isolated JSONL fixtures and mocked native control only.  They do
not launch, attach to, or recover a real Claude session.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import continuity, core, native_control, tmux_transport, tui_runtime  # noqa: E402
from claude_context_continuity.tui_runtime import TuiRuntime  # noqa: E402


class AutomaticContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="automatic-continuity-", dir=Path(__file__).parent)
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        home = patch.object(core, "HOME", self.home)
        home.start()
        self.addCleanup(home.stop)
        self.config = {"hash": "automatic-continuity", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        self.sid = str(uuid4())
        self.runtime = TuiRuntime.create(
            cwd=self.cwd,
            session_id=self.sid,
            configuration=self.config,
            configuration_reader=lambda _: self.config,
        )
        self.source = self.root / f"{self.sid}.jsonl"
        self._write_records(self.source, self.sid, self._user("Continue the already-authorized task."),
                            self._assistant(125))
        self.runtime.on_hook(self.hook("SessionStart", source="startup"))
        self.binding = {"pane_pid": os.getpid(), "cursor_x": 0, "cursor_y": 0}
        self.control = {
            "protocol": native_control.PROTOCOL,
            "context_id": self.runtime.conversation_id,
            "socket_path": str(self.home / "runtime" / "control" / f"{self.runtime.conversation_id}.sock"),
        }
        self.runtime._control_auth = "isolated-test-authentication-value-123456"
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state.update(transport="tmux_tui", tmux=self.binding, native_control=self.control)
            self.runtime._save(state)
        ready = patch.object(native_control, "ready", return_value=True)
        inspect = patch.object(tmux_transport, "inspect", return_value=self.binding)
        clear = patch.object(native_control, "send_clear")
        send = patch.object(native_control, "send_continuation")
        self.ready_mock = ready.start()
        self.inspect_mock = inspect.start()
        self.clear_mock = clear.start()
        self.send_mock = send.start()
        for item in (ready, inspect, clear, send):
            self.addCleanup(item.stop)
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="bootstrap"))
        self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="bootstrap"))

    def _user(self, text: str) -> dict[str, object]:
        return {
            "type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
            "message": {"role": "user", "content": text},
        }

    def _assistant(self, tokens: int, *, sid: str | None = None, cwd: Path | None = None,
                   text: str = "fixture native response") -> dict[str, object]:
        return {
            "type": "assistant", "uuid": str(uuid4()), "sessionId": sid or self.sid,
            "cwd": str(cwd or self.cwd),
            "message": {
                "role": "assistant", "content": text, "model": "fixture-native-model",
                "usage": {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0},
            },
        }

    @staticmethod
    def _write_records(path: Path, sid: str, *records: dict[str, object]) -> None:
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def _append(self, path: Path, *records: dict[str, object]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def hook(self, name: str, **fields: object) -> dict[str, object]:
        return {
            "hook_event_name": name,
            "session_id": self.sid,
            "cwd": str(self.cwd),
            "transcript_path": str(self.source),
            **({"last_assistant_message": "fixture native response"} if name == "Stop" else {}),
            **fields,
        }

    def _request_rotation(self) -> None:
        self._append(self.source, self._assistant(800))
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_late_clear_and_continuation_confirmations_advance_once(self) -> None:
        self._request_rotation()
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        waiting_clear = self.runtime.advance()
        self.assertEqual(waiting_clear["phase"], "clear_sent")
        self.assertIsNotNone(waiting_clear["rotation_pending"])
        self.clear_mock.assert_called_once()

        new_sid = str(uuid4())
        new_source = self.root / f"{new_sid}.jsonl"
        new_source.write_bytes(b"")
        self.runtime.on_hook({
            "hook_event_name": "SessionStart", "source": "clear", "session_id": new_sid,
            "cwd": str(self.cwd), "transcript_path": str(new_source),
        })
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        dispatched = self.runtime.advance()
        self.assertEqual(dispatched["phase"], "awaiting_continuation")
        self.send_mock.assert_called_once()
        continuation = self.send_mock.call_args.args[2]
        self._write_records(new_source, new_sid, {
            "type": "user", "uuid": str(uuid4()), "sessionId": new_sid,
            "message": {"role": "user", "content": continuation},
        })
        settled = self.runtime.advance()
        self.assertEqual(settled["phase"], "running")
        self.assertFalse(settled["rotation_pending"])
        self.assertFalse(settled["clear_pending"])
        self.runtime.advance()
        self.clear_mock.assert_called_once()
        self.send_mock.assert_called_once()

    def test_resume_file_readiness_reconciles_without_another_human_hook(self) -> None:
        target_sid = str(uuid4())
        target_source = self.root / f"{target_sid}.jsonl"
        self.runtime.on_hook({
            "hook_event_name": "SessionStart", "source": "resume", "session_id": target_sid,
            "cwd": str(self.cwd), "transcript_path": str(target_source),
        })
        observed = self.runtime.receipt()
        self.assertEqual(observed["session_id"], target_sid)
        self.assertEqual(observed["source_path"], str(target_source))
        self.assertNotEqual(observed["phase"], "closed")

        self._write_records(target_source, target_sid, {
            "type": "user", "uuid": str(uuid4()), "sessionId": target_sid,
            "message": {"role": "user", "content": "Resume the exact existing task."},
        }, self._assistant(500, sid=target_sid))
        recovered = self.runtime.advance()
        self.assertEqual(recovered["session_id"], target_sid)
        self.assertEqual(recovered["authorization"]["root_instruction_locator"]["session_id"], target_sid)
        self.assertEqual(recovered["usage"]["total_input_and_cache_tokens"], 500)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_prewrite_retry_is_safe_but_unknown_clear_is_not_duplicated(self) -> None:
        self._append(self.source, self._assistant(800))
        self.runtime.on_hook(self.hook("Stop"))
        self.clear_mock.side_effect = [
            native_control.NativeControlNotSent("fixture prewrite failure"),
            native_control.NativeControlError("fixture write outcome unknown"),
        ]
        first = self.runtime.advance()
        self.assertEqual(first["phase"], "rotation_requested")
        self.assertTrue(first["rotation_pending"])
        self.assertFalse(first["clear_pending"])
        second = self.runtime.advance()
        self.assertEqual(second["phase"], "clear_sent")
        self.assertTrue(second["rotation_pending"])
        self.assertTrue(second["clear_pending"])
        third = self.runtime.advance()
        self.assertEqual(third["phase"], "clear_sent")
        # One mock call below represents the uncertain write; the prewrite attempt
        # is deliberately counted separately and must be the only retry.
        self.assertEqual(self.clear_mock.call_count, 2)
        self.send_mock.assert_not_called()

    def test_real_resume_supersedes_old_markers_without_adopting_old_source(self) -> None:
        old_source = self.source
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state.update(
                phase="awaiting_continuation",
                pending_tool_ids=["old-direct-tool"],
                continuation_hash="a" * 64,
                rotation={
                    "generation": 3,
                    "request": {"generation": 3, "session_id": self.sid, "source_path": str(old_source),
                                "handoff": "old handoff", "notes": ""},
                    "clear": {"generation": 3, "old_session_id": self.sid, "command_id": str(uuid4()),
                              "reset_seen": True, "new_session_id": self.sid},
                },
            )
            self.runtime._save(state)
        target_sid = str(uuid4())
        target_source = self.root / f"{target_sid}.jsonl"
        self._write_records(target_source, target_sid, {
            "type": "user", "uuid": str(uuid4()), "sessionId": target_sid,
            "message": {"role": "user", "content": "Continue only this real resumed task."},
        }, self._assistant(300, sid=target_sid))
        self.runtime.on_hook({
            "hook_event_name": "SessionStart", "source": "resume", "session_id": target_sid,
            "cwd": str(self.cwd), "transcript_path": str(target_source),
        })
        state = self.runtime._state()
        self.assertEqual(state["session_id"], target_sid)
        self.assertEqual(state["source_path"], str(target_source))
        self.assertEqual(state["authorization"]["root_instruction_locator"]["session_id"], target_sid)
        self.assertEqual(state["pending_tool_ids"], [])
        self.assertEqual(state["superseded_lifecycle"]["session_id"], self.sid)
        self.assertEqual(state["superseded_lifecycle"]["source_path"], str(old_source))
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_same_sid_resume_keeps_next_turn_hooks_in_the_current_epoch(self) -> None:
        for index in range(2):
            self.runtime.on_hook(self.hook("SessionStart", source="resume"))
            prompt = f"next real turn after same-session resume {index}"
            self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=prompt))
            tool_id = f"current-epoch-tool-{index}"
            self.runtime.on_hook(self.hook("PreToolUse", tool_use_id=tool_id))
            self.assertIn(tool_id, self.runtime.receipt()["pending_tool_ids"])
            self.runtime.on_hook(self.hook("PostToolUse", tool_use_id=tool_id))
            self.assertNotIn(tool_id, self.runtime.receipt()["pending_tool_ids"])
        self.assertEqual(self.runtime.receipt()["session_id"], self.sid)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_new_active_clear_window_cancels_stale_handoff_but_keeps_deferred_receipt(self) -> None:
        self._request_rotation()
        new_sid = str(uuid4())
        new_source = self.root / f"{new_sid}.jsonl"
        new_source.write_bytes(b"")
        self.runtime.on_hook({
            "hook_event_name": "SessionStart", "source": "clear", "session_id": new_sid,
            "cwd": str(self.cwd), "transcript_path": str(new_source),
        })
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            self.runtime._wait(state, "continuation_dispatch_deadline_elapsed")
            self.runtime._save(state)
        prompt = "Cancel the prior task; inspect only this new request."
        blocked = self.runtime.on_hook({
            "hook_event_name": "UserPromptSubmit", "session_id": new_sid, "cwd": str(self.cwd),
            "transcript_path": str(new_source), "prompt": prompt,
        })
        self.assertFalse(blocked["continue"])
        self._write_records(new_source, new_sid, {
            "type": "user", "uuid": str(uuid4()), "sessionId": new_sid,
            "message": {"role": "user", "content": prompt},
        }, self._assistant(200, sid=new_sid, text="new-window model activity"))
        self.runtime.on_hook({
            "hook_event_name": "PreToolUse", "session_id": new_sid, "cwd": str(self.cwd),
            "transcript_path": str(new_source), "tool_use_id": "new-active-tool",
        })
        settled = self.runtime.advance()
        state = self.runtime._state()
        self.assertEqual(settled["phase"], "running")
        self.assertFalse(settled["rotation_pending"])
        self.assertFalse(settled["clear_pending"])
        self.assertEqual(len(state["deferred_inputs"]), 1)
        self.assertEqual(state["authorization"]["latest_instruction_locator"]["session_id"], new_sid)
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_resume_historical_unmatched_tool_does_not_block_new_budget_rotation(self) -> None:
        target_sid = str(uuid4())
        target_source = self.root / f"{target_sid}.jsonl"
        interrupted = self._assistant(800, sid=target_sid)
        interrupted["message"]["content"] = [{
            "type": "tool_use", "id": "historical-interrupted-call", "name": "Read", "input": {},
        }]
        self._write_records(target_source, target_sid, {
            "type": "user", "uuid": str(uuid4()), "sessionId": target_sid,
            "message": {"role": "user", "content": "Resume the existing task without replaying it."},
        }, interrupted)
        self.runtime.on_hook({
            "hook_event_name": "SessionStart", "source": "resume", "session_id": target_sid,
            "cwd": str(self.cwd), "transcript_path": str(target_source),
        })
        rebound = self.runtime.receipt()
        self.assertEqual(rebound["session_id"], target_sid)
        self.assertEqual(rebound["phase"], "running")
        outcome = self.runtime.advance()
        self.assertEqual(outcome["phase"], "clear_sent")
        self.assertIn("historical-interrupted-call", self.runtime._state()["resume_historical_pending_tool_ids"])
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_missing_initial_human_still_observes_budget_without_handoff_authority(self) -> None:
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        self._write_records(source, sid, self._assistant(800, sid=sid))
        runtime = TuiRuntime.create(cwd=self.cwd, session_id=sid, configuration=self.config,
                                    configuration_reader=lambda _: self.config)
        runtime.on_hook({
            "hook_event_name": "SessionStart", "source": "startup", "session_id": sid,
            "cwd": str(self.cwd), "transcript_path": str(source),
        })
        runtime.on_hook({
            "hook_event_name": "PreToolUse", "session_id": sid, "cwd": str(self.cwd),
            "transcript_path": str(source), "tool_use_id": "budget-observation-only",
        })
        observed = runtime.receipt()
        self.assertNotEqual(observed["phase"], "paused")
        self.assertIsNone(observed["authorization"])
        self.assertEqual(observed["usage"]["total_input_and_cache_tokens"], 800)
        self.assertTrue(observed["budget_handoff_required"])
        self.assertFalse(observed["rotation_pending"])

    def test_fresh_resume_activity_invalidates_only_the_old_boundary(self) -> None:
        target_sid = str(uuid4())
        target_source = self.root / f"{target_sid}.jsonl"
        self._write_records(target_source, target_sid, {
            "type": "user", "uuid": str(uuid4()), "sessionId": target_sid,
            "message": {"role": "user", "content": "Resume this existing task."},
        }, self._assistant(800, sid=target_sid))
        self.runtime.on_hook({
            "hook_event_name": "SessionStart", "source": "resume", "session_id": target_sid,
            "cwd": str(self.cwd), "transcript_path": str(target_source),
        })
        self.assertTrue(self.runtime._state()["resume_safe_boundary"])
        self._append(target_source, {
            "type": "user", "uuid": str(uuid4()), "sessionId": target_sid,
            "message": {"role": "user", "content": "A fresh correction arrived after resume."},
        }, self._assistant(850, sid=target_sid, text="fresh model activity"))
        observed = self.runtime.advance()
        self.assertNotEqual(observed["phase"], "paused")
        self.assertNotIn("resume_safe_boundary", self.runtime._state())
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_cli_applies_resume_before_a_repaired_observer_can_advance(self) -> None:
        self._append(self.source, self._assistant(800))
        self.runtime.on_hook(self.hook("Stop"))
        target_sid = str(uuid4())
        target_source = self.root / f"{target_sid}.jsonl"
        self._write_records(target_source, target_sid, {
            "type": "user", "uuid": str(uuid4()), "sessionId": target_sid,
            "message": {"role": "user", "content": "Use only this resumed task."},
        }, self._assistant(200, sid=target_sid))
        event = self.hook("SessionStart", source="resume", session_id=target_sid,
                          transcript_path=str(target_source))
        args = continuity.parser().parse_args(["tui-hook", "--context-id", self.runtime.conversation_id])
        with patch.object(TuiRuntime, "load", return_value=self.runtime), \
                patch.object(tui_runtime, "repair_controller_from_hook",
                             side_effect=lambda *args: self.runtime.advance()) as repair, \
                patch.dict(os.environ, {"CLAUDE_CONTINUITY_ID": self.runtime.conversation_id}), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
            result = continuity.dispatch(args)
        repair.assert_called_once()
        self.assertIn("hookSpecificOutput", result)
        self.assertEqual(self.runtime.receipt()["session_id"], target_sid)
        self.assertEqual(self.runtime.receipt()["usage"]["total_input_and_cache_tokens"], 200)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_rejected_resume_cannot_spawn_or_act_on_the_old_boundary(self) -> None:
        self._append(self.source, self._assistant(800))
        self.runtime.on_hook(self.hook("Stop"))
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state.update(controller_managed=True, controller_expected=True)
            self.runtime._save(state)
        event = self.hook("SessionStart", source="resume", session_id=str(uuid4()))
        args = continuity.parser().parse_args(["tui-hook", "--context-id", self.runtime.conversation_id])
        environment = native_control.environment(self.control, self.runtime._control_auth) | {
            "CLAUDE_CONTINUITY_ID": self.runtime.conversation_id,
        }
        with patch.object(TuiRuntime, "load", return_value=self.runtime), \
                patch.object(tui_runtime.subprocess, "Popen") as spawn, \
                patch.dict(os.environ, environment), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
            continuity.dispatch(args)
        self.assertTrue(self.runtime.receipt()["native_session_uncertain"])
        self.runtime.advance()
        spawn.assert_not_called()
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_pending_resume_authority_does_not_erase_current_tool_hooks(self) -> None:
        target_sid = str(uuid4())
        target_source = self.root / f"{target_sid}.jsonl"
        self._write_records(target_source, target_sid, self._assistant(200, sid=target_sid))
        event = self.hook("SessionStart", source="resume", session_id=target_sid,
                          transcript_path=str(target_source))
        self.runtime.on_hook(event)
        self.runtime.on_hook({**event, "hook_event_name": "PreToolUse", "tool_use_id": "current-live-tool"})
        self.runtime.advance()
        self.assertEqual(self.runtime.receipt()["pending_tool_ids"], ["current-live-tool"])
        self._append(target_source, {
            "type": "user", "uuid": str(uuid4()), "sessionId": target_sid,
            "message": {"role": "user", "content": "The new genuine instruction is now durable."},
        }, self._assistant(300, sid=target_sid))
        self.runtime.advance()
        self.assertIsNone(self.runtime.receipt()["pending_session_start"])
        self.assertEqual(self.runtime.receipt()["pending_tool_ids"], ["current-live-tool"])
        self.assertNotIn("resume_safe_boundary", self.runtime._state())
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_native_resume_can_return_to_a_superseded_session(self) -> None:
        target_sid = str(uuid4())
        target_source = self.root / f"{target_sid}.jsonl"
        self._write_records(target_source, target_sid, {
            "type": "user", "uuid": str(uuid4()), "sessionId": target_sid,
            "message": {"role": "user", "content": "The other genuine task."},
        }, self._assistant(200, sid=target_sid))
        self.runtime.on_hook(self.hook("SessionStart", source="resume", session_id=target_sid,
                                       transcript_path=str(target_source)))
        self.assertEqual(self.runtime.receipt()["session_id"], target_sid)
        self.runtime.on_hook(self.hook("SessionStart", source="resume"))
        self.assertEqual(self.runtime.receipt()["session_id"], self.sid)
        self.assertEqual(self.runtime.receipt()["source_path"], str(self.source))
        self.assertEqual(self.runtime.receipt()["native_resume_confirmations"], 2)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_same_sid_restore_after_work_supersedes_an_unknown_clear(self) -> None:
        self.runtime.on_hook(self.hook("SessionStart", source="resume"))
        prompt = "Continue the actual next turn before restoring again."
        self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=prompt))
        self._append(self.source, self._user(prompt), self._assistant(800))
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="intervening-tool"))
        self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="intervening-tool"))
        self.runtime.on_hook(self.hook("Stop"))
        self.clear_mock.side_effect = native_control.NativeControlError("fixture delivery unknown")
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.runtime.on_hook(self.hook("SessionStart", source="resume"))
        state = self.runtime._state()
        self.assertEqual(state["phase"], "running")
        self.assertIsNone(state["rotation"]["clear"])
        self.assertIsNone(state["rotation"]["request"])
        self.assertEqual(state["native_resume_confirmations"], 2)
        self.assertTrue(state["superseded_lifecycle"]["clear_pending"])
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_handoff_confirmation_keeps_later_human_and_meta_inputs_actionable(self) -> None:
        self._request_rotation()
        early = "Input included in the first handoff."
        self.assertFalse(self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=early))["continue"])
        self._append(self.source, self._user(early))
        new_sid = str(uuid4())
        new_source = self.root / f"{new_sid}.jsonl"
        new_source.write_bytes(b"")
        self.runtime.on_hook(self.hook("SessionStart", source="clear", session_id=new_sid,
                                       transcript_path=str(new_source)))
        self.assertEqual(self.runtime.advance()["phase"], "awaiting_continuation")
        first_handoff = self.send_mock.call_args.args[2]
        late_human = "Cancel earlier work; inspect only the later human request."
        body = "An already-produced background result remains available."
        late_meta = f'<agent-message from="worker">\n{body}\n</agent-message>'
        for text, meta in ((late_human, False), (late_meta, True)):
            blocked = self.runtime.on_hook(self.hook("UserPromptSubmit", session_id=new_sid,
                transcript_path=str(new_source), prompt=text))
            self.assertFalse(blocked["continue"])
            row = {"type": "user", "uuid": str(uuid4()), "sessionId": new_sid,
                   "message": {"role": "user", "content": text}}
            if meta:
                row.update(isMeta=True, promptSource="system", origin={
                    "kind": "peer", "from": "worker", "body": body, "senderTaskId": "worker"})
            self._append(new_source, row)
        self._append(new_source, {"type": "user", "uuid": str(uuid4()), "sessionId": new_sid,
            "message": {"role": "user", "content": first_handoff}})
        self.runtime.advance()
        remaining = self.runtime._state()["deferred_inputs"]
        self.assertEqual([item["prompt_hash"] for item in remaining],
                         [core.digest(late_human), core.digest(late_meta)])
        self.assertFalse(self.runtime._state()["at_turn_boundary"])
        self.clear_mock.assert_called_once()
        self.send_mock.assert_called_once()
        self._append(new_source, self._assistant(200, sid=new_sid))
        self.runtime.on_hook(self.hook("Stop", session_id=new_sid, transcript_path=str(new_source)))
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        last_sid = str(uuid4())
        last_source = self.root / f"{last_sid}.jsonl"
        last_source.write_bytes(b"")
        self.runtime.on_hook(self.hook("SessionStart", source="clear", session_id=last_sid,
                                       transcript_path=str(last_source)))
        self.runtime.advance()
        second_handoff = self.send_mock.call_args.args[2]
        packet = json.loads(second_handoff.split("\n", 2)[2])
        self.assertNotEqual(first_handoff, second_handoff)
        self.assertEqual([item["source_kind"] for item in packet["deferred_inputs"]], ["original_user", "meta"])
        self.assertEqual(packet["latest_instruction_locator"], packet["deferred_inputs"][0])
        self.assertEqual(self.clear_mock.call_count, 2)
        self.assertEqual(self.send_mock.call_count, 2)
        self._append(last_source, {"type": "user", "uuid": str(uuid4()), "sessionId": last_sid,
            "message": {"role": "user", "content": second_handoff}})
        self.runtime.advance()
        self.assertEqual(self.runtime._state()["deferred_inputs"], [])
        self.assertEqual(self.send_mock.call_count, 2)

    def test_history_read_fault_cannot_erase_a_live_direct_tool_boundary(self) -> None:
        self._append(self.source, self._assistant(800))
        self.runtime.on_hook(self.hook("Stop"))
        with patch.object(self.runtime, "_bind", side_effect=OSError("fixture history read unavailable")):
            self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="live-direct-tool"))
        self.runtime.advance()
        self.assertEqual(self.runtime.receipt()["pending_tool_ids"], ["live-direct-tool"])
        self.assertFalse(self.runtime._state()["at_turn_boundary"])
        self.clear_mock.assert_not_called()
        self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="live-direct-tool"))
        self._append(self.source, self._assistant(850))
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_legacy_pause_migrates_without_replaying_a_pending_clear(self) -> None:
        self._request_rotation()
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["phase"], state["pause_reason"] = "paused", "old native acknowledgement timeout"
            state.pop("reconciliation", None)
            self.runtime._save(state)
        result = self.runtime.advance()
        self.assertEqual(result["phase"], "clear_sent")
        self.assertTrue(result["clear_pending"])
        self.assertIsInstance(result["reconciliation"], dict)
        self.assertTrue(self.runtime._state()["legacy_pause_migrated"])
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_managed_hook_repairs_from_owned_aliases_when_native_variables_are_consumed(self) -> None:
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state.update(controller_managed=True, controller_expected=True, controller_pid=None,
                         controller_starting=False)
            self.runtime._save(state)
        auth = "owned-alias-memory-only-fixture-authentication"
        environment = {
            "CLAUDE_CONTINUITY_ID": self.runtime.conversation_id,
            native_control.HOOK_SOCKET_ENV: self.control["socket_path"],
            native_control.HOOK_AUTH_ENV: auth,
        }
        event = self.hook("PreToolUse", tool_use_id="owned-alias-repair")
        args = continuity.parser().parse_args(["tui-hook", "--context-id", self.runtime.conversation_id])
        with patch.object(TuiRuntime, "load", return_value=self.runtime), \
                patch.object(tui_runtime.subprocess, "Popen") as spawn, \
                patch.dict(os.environ, environment, clear=True):
            spawn.return_value.pid = os.getpid()
            for _ in range(2):
                with patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
                    continuity.dispatch(args)
        spawn.assert_called_once()
        launched = spawn.call_args.kwargs["env"]
        self.assertTrue(launched[native_control.AUTH_ENV] == auth)
        self.assertEqual(launched[native_control.SOCKET_ENV], self.control["socket_path"])
        self.assertNotIn(auth, " ".join(spawn.call_args.args[0]))
        self.assertNotIn(auth.encode(), self.runtime.state_path.read_bytes())
        self.assertEqual(self.runtime.receipt()["controller_pid"], os.getpid())
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_controller_capability_pairs_require_exact_complete_nonconflicting_binding(self) -> None:
        state = self.runtime._state()
        state["controller_managed"] = True
        auth = "controller-pair-check-fixture-authentication"
        context = {"CLAUDE_CONTINUITY_ID": self.runtime.conversation_id}
        native = {native_control.SOCKET_ENV: self.control["socket_path"], native_control.AUTH_ENV: auth}
        owned = {native_control.HOOK_SOCKET_ENV: self.control["socket_path"], native_control.HOOK_AUTH_ENV: auth}
        before = self.runtime.state_path.read_bytes()
        for pair in (native, owned, native | owned):
            with self.subTest(valid_pair_names=sorted(pair)):
                restored = tui_runtime._controller_environment(
                    self.runtime, state, context | pair, automatic=True)
                self.assertTrue(restored[native_control.AUTH_ENV] == auth)
                self.assertEqual(restored[native_control.SOCKET_ENV], self.control["socket_path"])
        invalid = {
            "absent": context,
            "owned_auth_missing": context | {native_control.HOOK_SOCKET_ENV: self.control["socket_path"]},
            "owned_socket_missing": context | {native_control.HOOK_AUTH_ENV: auth},
            "owned_socket_mismatch": context | owned | {native_control.HOOK_SOCKET_ENV: "/unrelated/control.sock"},
            "context_mismatch": context | owned | {"CLAUDE_CONTINUITY_ID": str(uuid4())},
            "conflicting_auth": context | native | owned | {native_control.AUTH_ENV: "other-fixture-authentication-value-123456"},
            "conflicting_socket": context | native | owned | {native_control.SOCKET_ENV: "/unrelated/control.sock"},
            "incomplete_native_with_aliases": context | owned | {native_control.AUTH_ENV: auth},
        }
        for label, environment in invalid.items():
            with self.subTest(invalid_pair=label), self.assertRaises(tui_runtime.ContextRuntimeError):
                tui_runtime._controller_environment(self.runtime, state, environment, automatic=True)
        wrong_control = {**state, "native_control": {**self.control, "context_id": str(uuid4())}}
        with self.assertRaises(tui_runtime.ContextRuntimeError):
            tui_runtime._controller_environment(self.runtime, wrong_control, context | owned, automatic=True)
        manual = {**state, "controller_managed": False}
        with self.assertRaises(tui_runtime.ContextRuntimeError):
            tui_runtime._controller_environment(self.runtime, manual, context | owned, automatic=True)
        self.assertEqual(self.runtime.state_path.read_bytes(), before)
        self.assertNotIn(auth.encode(), before)

    def test_managed_hook_repairs_dead_controller_once_without_persisting_credential(self) -> None:
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state.update(controller_managed=True, controller_expected=True, controller_pid=None,
                         controller_starting=False)
            self.runtime._save(state)
        auth = "managed-hook-memory-only-authentication-123456"
        environment = native_control.environment(self.control, auth) | {
            "CLAUDE_CONTINUITY_ID": self.runtime.conversation_id,
        }
        args = continuity.parser().parse_args(["tui-hook", "--context-id", self.runtime.conversation_id])
        event = self.hook("PreToolUse", tool_use_id="repair-boundary")
        with patch.object(TuiRuntime, "load", return_value=self.runtime), \
                patch.object(tui_runtime.subprocess, "Popen") as spawn, \
                patch.dict(os.environ, environment):
            spawn.return_value.pid = os.getpid()
            for _ in range(2):
                with patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
                    continuity.dispatch(args)
        spawn.assert_called_once()
        self.assertNotIn(auth.encode("utf-8"), self.runtime.state_path.read_bytes())
        self.assertEqual(self.runtime._state()["controller_pid"], os.getpid())
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
