"""原生 hook 驱动的 TUI 轮换检查；不操作真实终端。"""
from __future__ import annotations

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
from claude_context_continuity import core, tmux_transport, tui_runtime
from claude_context_continuity.history import HistorySource
from claude_context_continuity.tui_runtime import TuiRuntime


class TuiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.patch = patch.object(core, "HOME", self.home)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.config = {"hash": "fixed-config", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        self.sid = str(uuid4())
        self.runtime = TuiRuntime.create(cwd=self.cwd, session_id=self.sid, configuration=self.config,
                                          configuration_reader=lambda _: self.config)
        self.source = self.root / f"{self.sid}.jsonl"
        self.source.write_text(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
            "message": {"role": "user", "content": "只继续这项已经授权的任务。"}}) + "\n")
        self.usage(125)
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": self.sid,
                              "cwd": str(self.cwd), "source": "startup"})
        self.binding = {"pane_pid": os.getpid(), "cursor_x": 2, "cursor_y": 1}
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["tmux"] = self.binding
            state["transport"] = "tmux_tui"
            self.runtime._save(state)
        self.inspector = patch.object(tmux_transport, "inspect", return_value=self.binding)
        self.capture = patch.object(tmux_transport, "capture", return_value="native TUI\n❯ \n")
        self.clear = patch.object(tmux_transport, "send_clear")
        self.send = patch.object(tmux_transport, "send_text")
        self.inspector.start()
        self.capture_mock = self.capture.start()
        self.clear_mock = self.clear.start()
        self.send_mock = self.send.start()
        for p in (self.inspector, self.capture, self.clear, self.send):
            self.addCleanup(p.stop)
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="bootstrap"))
        self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="bootstrap"))

    def usage(self, value, source=None, sid=None, cwd=None):
        with (source or self.source).open("a") as f:
            f.write(json.dumps({"type": "assistant", "uuid": str(uuid4()), "sessionId": sid or self.sid,
                "cwd": str(cwd or self.cwd), "message": {"role": "assistant", "content": "真实形状的公开结果",
                "model": "native-model", "usage": {"input_tokens": value,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")

    def resume_runtime(self):
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_text("")
        runtime = TuiRuntime.create(cwd=self.cwd, session_id=sid, configuration=self.config,
                                    configuration_reader=lambda _: self.config)
        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid,
                         "cwd": str(self.cwd), "source": "startup", "transcript_path": str(source)})
        with core.lock(runtime.lock_path):
            state = runtime._state()
            state["tmux"] = self.binding
            state["transport"] = "tmux_tui"
            runtime._save(state)
        return runtime, sid, source

    def resume_source(self, value, cwd=None):
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_text(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": sid,
            "message": {"role": "user", "content": "只继续已授权的恢复任务。"}}) + "\n")
        self.usage(value, source, sid, cwd)
        return sid, source

    def hook(self, event, **fields):
        return {"hook_event_name": event, "session_id": self.sid, "cwd": str(self.cwd),
                "transcript_path": str(self.source),
                **({"last_assistant_message": "真实形状的公开结果"} if event == "Stop" else {}), **fields}

    def threshold(self):
        self.usage(800)
        self.runtime.on_hook(self.hook("Stop"))

    def test_taskstop_settles_current_hook_and_history_before_rotation(self):
        def append(record_type, content, **extra):
            with self.source.open("a") as f:
                f.write(json.dumps({"type": record_type, "uuid": str(uuid4()), "sessionId": self.sid,
                                   "message": {"role": record_type, "content": content}, **extra}) + "\n")
        append("assistant", [{"type": "tool_use", "id": "launch-current", "name": "Agent", "input": {}}])
        append("user", [{"type": "tool_result", "tool_use_id": "launch-current", "content": "started"}],
               toolUseResult={"agentId": "current-child", "isAsync": True})
        self.runtime.on_hook(self.hook("SubagentStart", agent_id="current-child"))
        append("assistant", [{"type": "tool_use", "id": "stop-current", "name": "TaskStop", "input": {"task_id": "current-child"}}])
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="stop-current", tool_name="TaskStop", tool_input={"task_id": "current-child"}))
        response = {"task_id": "current-child", "task_type": "local_agent", "message": "Successfully stopped task"}
        append("user", [{"type": "tool_result", "tool_use_id": "stop-current", "content": "stopped"}], toolUseResult=response)
        self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="stop-current", tool_name="TaskStop",
                                       tool_input={"task_id": "current-child"}, tool_response=response))
        self.assertEqual(self.runtime.receipt()["active_child_handles"], [])
        self.assertEqual(HistorySource(self.source, self.sid).activity()["background_handles"], {})
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_taskstop_failure_and_duplicate_hook_do_not_clear_new_child(self):
        self.runtime.on_hook(self.hook("SubagentStart", agent_id="current-child"))
        self.runtime._pause_external("fixture observation pause")
        fields = {"tool_use_id": "stop-failed", "tool_name": "TaskStop", "tool_input": {"task_id": "current-child"},
                  "tool_response": {"task_id": "current-child", "task_type": "local_agent"}}
        self.runtime.on_hook(self.hook("PreToolUse", **fields))
        self.runtime.on_hook(self.hook("PostToolUseFailure", **fields))
        self.assertEqual(self.runtime.receipt()["active_child_handles"], ["current-child"])
        fields["tool_use_id"] = "stop-success"
        self.runtime.on_hook(self.hook("PreToolUse", **fields))
        self.runtime.on_hook(self.hook("PostToolUse", **fields))
        self.assertEqual(self.runtime.receipt()["active_child_handles"], [])
        self.runtime.on_hook(self.hook("SubagentStart", agent_id="current-child"))
        self.runtime.on_hook(self.hook("PostToolUse", **fields))
        self.assertEqual(self.runtime.receipt()["active_child_handles"], ["current-child"])
        self.assertEqual(self.runtime.receipt()["phase"], "paused")
        self.clear_mock.assert_not_called()

    def test_resume_rebinds_actual_usage_and_honors_live_windows(self):
        runtime, initial_sid, _ = self.resume_runtime()
        old_sid, old_source = self.resume_source(800)
        self.config = {"hash": "raised", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "2000"}}
        result = runtime.on_hook({"hook_event_name": "SessionStart", "session_id": old_sid,
                                  "cwd": str(self.cwd), "source": "resume",
                                  "transcript_path": str(old_source)})
        self.assertNotIn("continue", result)
        rebound = runtime.receipt()
        self.assertEqual(rebound["phase"], "running")
        self.assertEqual(rebound["session_id"], old_sid)
        self.assertEqual(rebound["source_path"], str(old_source))
        self.assertEqual(rebound["usage"]["total_input_and_cache_tokens"], 800)
        self.assertEqual(rebound["usage"]["remaining_context_tokens"], 1200)
        self.assertEqual(rebound["configuration"]["configured_window"], 2000)
        self.assertEqual(rebound["native_resume_confirmations"], 1)
        self.assertEqual(rebound["authorization"]["root_instruction_locator"]["session_id"], old_sid)
        self.assertNotEqual(rebound["session_id"], initial_sid)
        self.assertEqual(runtime.advance()["phase"], "running")
        self.assertNotIn("resume_safe_boundary", runtime._state())
        self.clear_mock.assert_not_called()
        self.config = {"hash": "lowered-after-resume", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "600"}}
        runtime.on_hook({"hook_event_name": "Stop", "session_id": old_sid, "cwd": str(self.cwd),
                         "transcript_path": str(old_source), "last_assistant_message": "真实形状的公开结果"})
        lowered = runtime.advance()
        self.assertEqual(lowered["phase"], "clear_sent")
        self.assertEqual(lowered["usage"]["remaining_context_tokens"], -200)
        self.clear_mock.assert_called_once()

    def test_resume_over_budget_rotates_before_next_model_request(self):
        runtime, _, _ = self.resume_runtime()
        old_sid, old_source = self.resume_source(1200)
        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": old_sid,
                         "cwd": str(self.cwd), "source": "resume", "transcript_path": str(old_source)})
        rebound = runtime.receipt()
        self.assertEqual(rebound["usage"]["total_input_and_cache_tokens"], 1200)
        self.assertTrue(runtime._state()["resume_safe_boundary"])
        outcome = runtime.advance()
        self.assertEqual(outcome["phase"], "clear_sent")
        self.assertEqual(outcome["automatic_rotations"], 1)
        self.clear_mock.assert_called_once()
        new_sid = str(uuid4())
        new_source = self.root / f"{new_sid}.jsonl"
        new_source.write_text("")
        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": new_sid,
                         "cwd": str(self.cwd), "source": "clear", "transcript_path": str(new_source)})
        runtime.advance()
        self.send_mock.assert_called_once()
        self.assertIn(old_sid, self.send_mock.call_args.args[1])

    def test_resume_new_prompt_cancels_the_old_safe_boundary_without_blocking(self):
        runtime, _, _ = self.resume_runtime()
        old_sid, old_source = self.resume_source(1200)
        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": old_sid,
                         "cwd": str(self.cwd), "source": "resume", "transcript_path": str(old_source)})
        blocked = runtime.on_hook({"hook_event_name": "UserPromptSubmit", "session_id": old_sid,
                                   "cwd": str(self.cwd), "prompt": "不要在检查前请求模型"})
        self.assertNotIn("continue", blocked)
        self.assertEqual(runtime.advance()["phase"], "running")
        self.assertNotIn("resume_safe_boundary", runtime._state())
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_resume_rejects_a_cross_directory_source_without_rebinding(self):
        runtime, initial_sid, initial_source = self.resume_runtime()
        other_cwd = self.root / "other-workspace"
        other_cwd.mkdir()
        old_sid, old_source = self.resume_source(800, other_cwd)
        result = runtime.on_hook({"hook_event_name": "SessionStart", "session_id": old_sid,
                                  "cwd": str(self.cwd), "source": "resume",
                                  "transcript_path": str(old_source)})
        self.assertNotIn("continue", result)
        rejected = runtime.receipt()
        self.assertEqual(rejected["phase"], "paused")
        self.assertEqual(rejected["session_id"], initial_sid)
        self.assertEqual(rejected["source_path"], str(initial_source))

    def test_resume_pauses_if_the_bound_source_changes_before_first_clear(self):
        runtime, _, _ = self.resume_runtime()
        old_sid, old_source = self.resume_source(1200)
        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": old_sid,
                         "cwd": str(self.cwd), "source": "resume", "transcript_path": str(old_source)})
        with old_source.open("a") as f:
            f.write(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": old_sid,
                "message": {"role": "user", "content": "并发来源的新输入。"}}) + "\n")
        outcome = runtime.advance()
        self.assertEqual(outcome["phase"], "paused")
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_resume_rejects_an_unsettled_source_without_rebinding(self):
        runtime, initial_sid, initial_source = self.resume_runtime()
        old_sid, old_source = self.resume_source(800)
        with old_source.open("a") as f:
            f.write(json.dumps({"type": "assistant", "uuid": str(uuid4()), "sessionId": old_sid,
                "cwd": str(self.cwd), "message": {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "resume-pending-tool", "name": "Read", "input": {}}],
                    "model": "native-model", "usage": {"input_tokens": 800,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
        result = runtime.on_hook({"hook_event_name": "SessionStart", "session_id": old_sid,
                                  "cwd": str(self.cwd), "source": "resume",
                                  "transcript_path": str(old_source)})
        self.assertNotIn("continue", result)
        rejected = runtime.receipt()
        self.assertEqual(rejected["phase"], "paused")
        self.assertEqual(rejected["session_id"], initial_sid)
        self.assertEqual(rejected["source_path"], str(initial_source))

    def test_budget_stop_clear_native_ack_and_real_source_consumption(self):
        self.threshold()
        outcome = self.runtime.advance()
        self.assertEqual(outcome["phase"], "clear_sent")
        self.assertEqual(outcome["automatic_rotations"], 1)
        self.clear_mock.assert_called_once()
        self.runtime.advance()
        self.clear_mock.assert_called_once()
        new_sid = str(uuid4())
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": new_sid,
                              "cwd": str(self.cwd), "source": "clear"})
        self.runtime.advance()
        self.send_mock.assert_called_once()
        text = self.send_mock.call_args.args[1]
        self.assertTrue(text.startswith("<continuity-host-event>"))
        self.assertIn(self.sid, text)
        self.assertEqual(self.runtime.receipt()["phase"], "awaiting_continuation")
        source = self.root / f"{new_sid}.jsonl"
        source.write_text(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": new_sid,
                                     "message": {"role": "user", "content": text}}) + "\n")
        self.usage(120, source, new_sid)
        self.runtime.on_hook({"hook_event_name": "Stop", "session_id": new_sid,
                              "cwd": str(self.cwd), "transcript_path": str(source)})
        outcome = self.runtime.receipt()
        self.assertEqual(outcome["phase"], "running")
        self.assertTrue(outcome["continuation_observed"])
        self.assertEqual(outcome["native_clear_confirmations"], 1)
        self.assertEqual(outcome["authorization"]["root_instruction_locator"]["session_id"], self.sid)

    def test_manual_request_uses_same_rotation_below_budget_and_is_idempotent(self):
        from claude_context_continuity.continuity import dispatch, parser
        args = parser().parse_args(["context-request", "--context-id", self.runtime.conversation_id,
                                    "--handoff", "保留完成结果，继续当前任务，不重复执行。"])
        outcome = dispatch(args)
        self.assertEqual(outcome["phase"], "rotation_requested")
        first = self.runtime._state()["rotation"]["request"]
        dispatch(args)
        self.assertEqual(self.runtime._state()["rotation"]["request"], first)
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.assertEqual(self.runtime._state().get("automatic_rotations", 0), 0)
        self.clear_mock.assert_called_once()
        sid = str(uuid4())
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid,
                              "cwd": str(self.cwd), "source": "clear"})
        self.runtime.advance()
        self.send_mock.assert_called_once()
        self.assertIn(first["handoff"], self.send_mock.call_args.args[1])

    def test_manual_request_waits_for_activity_and_preserves_typed_input(self):
        self.runtime.request_rotation("先结算当前活动，再按原任务继续。")
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="unfinished"))
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.advance()["phase"], "waiting_safe_boundary")
        self.clear_mock.assert_not_called()
        self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="unfinished"))
        self.runtime.on_hook(self.hook("Stop"))
        self.capture_mock.return_value = "native TUI\n❯ 正在输入\n"
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.capture_mock.return_value = "native TUI\n❯ \n"
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_new_tool_free_turn_cannot_reuse_previous_stop(self):
        self.runtime.on_hook(self.hook("Stop"))
        self.runtime.on_hook(self.hook("UserPromptSubmit", prompt="next user turn"))
        self.usage(800)
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.assertFalse(self.runtime._state()["at_turn_boundary"])
        self.runtime.on_hook(self.hook("Stop"))
        self.runtime.advance()
        self.clear_mock.assert_called_once()

    def test_stop_can_precede_final_transcript_flush(self):
        self.runtime.on_hook(self.hook("Stop", last_assistant_message="late final reply"))
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        with self.source.open("a") as f:
            f.write(json.dumps({"type": "assistant", "uuid": str(uuid4()), "sessionId": self.sid,
                "cwd": str(self.cwd), "message": {"role": "assistant", "content": "late final reply",
                "model": "native-model", "usage": {"input_tokens": 800,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_queued_human_input_is_not_skipped_at_stop_boundary(self):
        with self.source.open("a") as f:
            f.write(json.dumps({"type": "attachment", "uuid": str(uuid4()), "sessionId": self.sid,
                "isSidechain": False, "userType": "external", "attachment": {
                "type": "queued_command", "origin": {"kind": "human"}, "commandMode": "prompt",
                "source_uuid": self.sid, "prompt": "queued follow-up"}}) + "\n")
        self.threshold()
        self.runtime.advance()
        self.clear_mock.assert_not_called()

    def test_accepted_continuation_does_not_time_out_during_long_thinking(self):
        self.threshold()
        self.runtime.advance()
        sid = str(uuid4())
        path = self.root / f"{sid}.jsonl"
        path.write_text("")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid,
            "cwd": str(self.cwd), "source": "clear", "transcript_path": str(path)})
        self.runtime.advance()
        text = self.send_mock.call_args.args[1]
        path.write_text(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": sid,
                                   "message": {"role": "user", "content": text}}) + "\n")
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        result = self.runtime.advance()
        self.assertEqual(result["phase"], "running")
        self.assertTrue(result["continuation_observed"])
        self.assertFalse(self.runtime._state()["at_turn_boundary"])

    def test_user_input_is_not_cleared_or_overwritten(self):
        self.threshold()
        self.capture_mock.return_value = "native TUI\n❯ 正在编辑的输入\n"
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_no_clear_while_native_background_or_tool_is_unsettled(self):
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="pending"))
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "waiting_safe_boundary")
        self.clear_mock.assert_not_called()

    def test_missing_native_clear_ack_pauses_without_fallback(self):
        self.threshold()
        self.runtime.advance()
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        self.assertEqual(self.runtime.advance()["phase"], "paused")
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_clear_transport_failure_is_never_replayed(self):
        self.threshold()
        self.clear_mock.side_effect = RuntimeError("unknown transport result")
        self.assertEqual(self.runtime.advance()["phase"], "paused")
        self.runtime.advance()
        self.clear_mock.assert_called_once()

    def test_manual_clear_keeps_native_fresh_task_semantics(self):
        new_sid = str(uuid4())
        output = self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": new_sid,
                                       "cwd": str(self.cwd), "source": "clear"})
        self.assertNotIn("continue", output)
        outcome = self.runtime.receipt()
        self.assertEqual(outcome["session_id"], new_sid)
        self.assertIsNone(outcome["authorization"])
        self.runtime.advance()
        self.send_mock.assert_not_called()

    def test_resume_rebinds_after_manual_clear(self):
        current_sid = str(uuid4())
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": current_sid,
                              "cwd": str(self.cwd), "source": "clear"})
        old_sid, old_source = self.resume_source(800)
        result = self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": old_sid,
                                       "cwd": str(self.cwd), "source": "resume",
                                       "transcript_path": str(old_source)})
        self.assertNotIn("continue", result)
        rebound = self.runtime.receipt()
        self.assertEqual(rebound["phase"], "running")
        self.assertEqual(rebound["session_id"], old_sid)
        self.assertEqual(rebound["usage"]["total_input_and_cache_tokens"], 800)

    def test_live_window_updates_without_stopping_active_tui(self):
        self.config = {"hash": "new-defaults", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "2000"}}
        self.runtime.on_hook(self.hook("Stop"))
        result = self.runtime.advance()
        self.assertEqual(result["phase"], "running")
        self.assertEqual(result["configuration"]["configured_window"], 2000)
        self.assertTrue(self.runtime._state()["settings_changed_since_start"])

    def test_lowered_budget_rotates_at_boundary_instead_of_failing(self):
        self.threshold()
        self.config = {"hash": "lowered", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "600"}}
        result = self.runtime.advance()
        self.assertEqual(result["phase"], "clear_sent")
        self.assertEqual(result["usage"]["remaining_context_tokens"], -200)
        self.clear_mock.assert_called_once()

    def test_live_settings_window_is_not_masked_by_old_inherited_environment(self):
        config_home = self.root / "configuration"
        config_home.mkdir()
        (config_home / "settings.json").write_text(json.dumps({"autoCompactEnabled": False,
            "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "2000"}}))
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_home), "DISABLE_COMPACT": "1",
                                     "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}):
            self.assertEqual(core.configured_window(core.configuration(self.cwd)), 1000)
            self.assertEqual(core.configured_window(core.configuration(self.cwd, live_window=True)), 2000)

    def test_native_permission_change_is_observed_not_overridden_or_blocked(self):
        self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="bootstrap", permission_mode="default"))
        self.threshold()
        self.runtime.advance()
        result = self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": str(uuid4()),
            "cwd": str(self.cwd), "source": "clear", "permission_mode": "bypassPermissions"})
        self.assertNotIn("continue", result)
        self.assertEqual(self.runtime._state()["native_permission_mode"], "bypassPermissions")
        self.runtime.advance()
        self.send_mock.assert_called_once()

    def test_failed_agent_same_id_resume_and_duplicate_notifications_are_idempotent(self):
        for name in ("SubagentStart", "SubagentStart"):
            self.assertEqual(self.runtime.on_hook(self.hook(name, agent_id="same-agent")), {})
        self.assertEqual(self.runtime.receipt()["active_child_handles"], ["same-agent"])
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "waiting_safe_boundary")
        self.clear_mock.assert_not_called()
        for name in ("SubagentStop", "SubagentStop", "SubagentStart", "SubagentStop"):
            self.assertEqual(self.runtime.on_hook(self.hook(name, agent_id="same-agent")), {})
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_duplicate_tool_pre_and_failed_post_do_not_block(self):
        for name in ("PreToolUse", "PreToolUse", "PostToolUseFailure", "PostToolUseFailure"):
            output = self.runtime.on_hook(self.hook(name, tool_use_id="retry-tool"))
            self.assertNotIn("continue", output)
        self.assertEqual(self.runtime.receipt()["phase"], "running")
        self.assertEqual(self.runtime.receipt()["pending_tool_ids"], [])

    def test_observation_failure_leaves_native_tools_and_prompts_usable(self):
        self.config = {"hash": "invalid-window", "env": {}}
        for name in ("PreToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop", "UserPromptSubmit"):
            output = self.runtime.on_hook(self.hook(name, tool_use_id="still-usable",
                **({"agent_id": "child"} if name.startswith("Subagent") else {})))
            self.assertNotIn("continue", output)
            self.assertNotIn("decision", output)
        self.assertEqual(self.runtime.advance()["phase"], "paused")
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()
        self.config = {"hash": "repaired", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.receipt()["phase"], "running")
        self.assertEqual(self.runtime._state()["observation_recoveries"], 1)

    def test_unknown_clear_is_not_recovered_by_later_native_activity(self):
        self.threshold()
        self.clear_mock.side_effect = RuntimeError("unknown transport result")
        self.runtime.advance()
        self.assertEqual(self.runtime.on_hook(self.hook("UserPromptSubmit", prompt="继续正常任务")), {})
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.advance()["phase"], "paused")
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_resume_works_after_existing_work_and_with_same_session_id(self):
        for _ in range(2):
            output = self.runtime.on_hook(self.hook("SessionStart", source="resume"))
            self.assertNotIn("continue", output)
            self.assertEqual(self.runtime.receipt()["phase"], "running")
            self.assertEqual(self.runtime.receipt()["usage"]["total_input_and_cache_tokens"], 125)
        self.clear_mock.assert_not_called()

    def test_model_change_during_rotation_is_not_a_permission_gate(self):
        self.threshold()
        self.runtime.advance()
        sid = str(uuid4())
        output = self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid,
            "cwd": str(self.cwd), "source": "clear", "model": "new-user-model"})
        self.assertNotIn("continue", output)
        self.assertEqual(self.runtime._state()["native_session_model"], "new-user-model")
        self.runtime.advance()
        self.send_mock.assert_called_once()

    def test_recovery_reobserves_but_does_not_clear_or_replay(self):
        self.runtime._pause_external("hook_state_or_source_drift")
        before = self.source.read_bytes()
        result = self.runtime.recover_observation(self.sid)
        self.assertEqual(result["phase"], "running")
        self.assertEqual(result["session_id"], self.sid)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.runtime._state()["at_turn_boundary"])
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_recovery_refuses_active_work_and_wrong_terminal(self):
        self.runtime._pause_external("observation_failed")
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="active"))
        with self.assertRaises(ValueError):
            self.runtime.recover_observation(self.sid)
        with self.assertRaises(ValueError):
            self.runtime.recover_observation(str(uuid4()))
        self.assertEqual(self.runtime.receipt()["phase"], "paused")
        self.clear_mock.assert_not_called()

    def test_input_appearing_between_prepare_and_dispatch_is_preserved(self):
        self.threshold()
        self.capture_mock.side_effect = ["native TUI\n❯ \n", "native TUI\n❯ 用户新输入\n"]
        self.assertEqual(self.runtime.advance()["phase"], "paused")
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_recovery_cli_starts_only_an_unowned_controller(self):
        from claude_context_continuity import continuity
        self.runtime._pause_external("hook_state_or_source_drift")
        args = continuity.parser().parse_args(["tui-recover", "--context-id", self.runtime.conversation_id,
                                               "--session-id", self.sid])
        with patch("claude_context_continuity.tui_runtime.subprocess.Popen") as spawn:
            spawn.return_value.pid = 123456
            result = continuity.dispatch(args)
            self.assertEqual(result["controller_pid"], 123456)
            self.assertFalse(self.runtime._state()["at_turn_boundary"])
            self.assertNotIn("--model", spawn.call_args.args[0])
        with core.lock(self.runtime.directory / "controller.lock"), patch("claude_context_continuity.tui_runtime.subprocess.Popen") as spawn:
            with self.assertRaises(core.ContinuityError):
                continuity.dispatch(args)
            spawn.assert_not_called()
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_tui_hook_cli_failure_returns_no_native_block(self):
        from claude_context_continuity import continuity
        import io
        from contextlib import redirect_stdout
        argv = ["claude_context_continuity", "tui-hook", "--context-id", self.runtime.conversation_id]
        with patch.object(sys, "argv", argv), patch.object(sys, "stdin", io.StringIO("not-json")), \
                patch.dict(os.environ, {"CLAUDE_CONTINUITY_ID": self.runtime.conversation_id}), \
                redirect_stdout(io.StringIO()) as output:
            code = continuity.main()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {})
        self.assertEqual(self.runtime.receipt()["phase"], "paused")

    def test_cli_defaults_to_native_tui(self):
        from claude_context_continuity import continuity
        args = continuity.parser().parse_args(["context-run", "--cwd", str(self.cwd), "--detached"])
        self.assertFalse(hasattr(args, "headless"))
        with patch("claude_context_continuity.tui_runtime.run", return_value={"status": "started"}) as run:
            self.assertEqual(continuity.dispatch(args)["status"], "started")
            run.assert_called_once_with(str(self.cwd), prompt=None, detached=True, native_args=[])

    def test_create_injects_one_session_plugin_without_rewriting_caller_argv(self):
        caller_session, resumed_session = str(uuid4()), str(uuid4())
        caller_argv = [
            "--settings", '{"env":{"CALLER_VALUE":"unchanged"}}',
            "--system-prompt", "caller system prompt",
            "--append-system-prompt", "caller append prompt",
            "--plugin-dir", "/caller/plugin",
            "--model", "caller-model",
            "--tools", "Read,Edit",
            "--permission-mode", "manual",
            "--session-id", caller_session,
            "--resume", resumed_session,
        ]
        with patch.object(tmux_transport, "create", return_value=self.binding) as create:
            runtime = tui_runtime.create(self.cwd, native_args=caller_argv)
        plugin = runtime.directory / "plugin"
        self.assertEqual(create.call_args.args[2], ["claude", "--plugin-dir", str(plugin), *caller_argv])
        self.assertEqual(create.call_args.args[3], {
            **{key: value for key, value in os.environ.items() if key not in {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID"}},
            "CLAUDE_CONTINUITY_ID": runtime.conversation_id,
        })
        manifest = core.read_json(plugin / ".claude-plugin/plugin.json")
        self.assertEqual(manifest["name"], "cclaude")
        self.assertEqual(manifest["version"], tui_runtime.__version__)
        self.assertEqual(set(manifest["hooks"]), set(tui_runtime._PLUGIN_EVENTS))
        hook = manifest["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(hook["command"], sys.executable)
        self.assertEqual(hook["args"][-2:], ["--context-id", runtime.conversation_id])
        command = (plugin / "commands/renew.md").read_text()
        self.assertIn("name: renew\n", command)
        self.assertIn("disable-model-invocation: true", command)
        self.assertIn("context-request --context-id " + runtime.conversation_id, command)
        self.assertNotIn("allowed-tools:", command)
        self.assertNotIn("!`", command)
        self.assertIn("不要自行执行 /clear", command)
        self.assertFalse((self.home / "commands").exists())
        self.assertFalse((self.home / "settings.json").exists())

    def test_manual_command_binds_runtime_interpreter_without_fixed_environment(self):
        import shlex
        with patch.object(core.sys, "executable", "/custom python/bin/python"):
            command = tui_runtime._new_context_command(self.runtime.conversation_id)
        invocation = command.split("```sh\n", 1)[1].split("\n```", 1)[0]
        argv = shlex.split(invocation)
        self.assertEqual(argv[:4], ["/custom python/bin/python", "-B", "-m", "claude_context_continuity"])
        self.assertEqual(argv[4:7], ["context-request", "--context-id", self.runtime.conversation_id])
        self.assertNotIn("/opt/", command)

    def test_session_start_binds_actual_native_startup_and_resume_ids(self):
        startup_sid, resumed_sid = str(uuid4()), str(uuid4())
        startup_source = self.root / f"{startup_sid}.jsonl"
        startup_source.write_text(json.dumps({
            "type": "user", "uuid": str(uuid4()), "sessionId": startup_sid,
            "message": {"role": "user", "content": "启动任务。"},
        }) + "\n")
        self.usage(100, startup_source, startup_sid)
        runtime = TuiRuntime.create(cwd=self.cwd, session_id=str(uuid4()), configuration=self.config,
                                    configuration_reader=lambda _: self.config)
        runtime.on_hook({
            "hook_event_name": "SessionStart", "session_id": startup_sid, "cwd": str(self.cwd),
            "source": "startup", "transcript_path": str(startup_source),
        })
        self.assertEqual(runtime.receipt()["session_id"], startup_sid)

        resumed_source = self.root / f"{resumed_sid}.jsonl"
        resumed_source.write_text(json.dumps({
            "type": "user", "uuid": str(uuid4()), "sessionId": resumed_sid,
            "message": {"role": "user", "content": "恢复任务。"},
        }) + "\n")
        self.usage(125, resumed_source, resumed_sid)
        runtime.on_hook({
            "hook_event_name": "SessionStart", "session_id": resumed_sid, "cwd": str(self.cwd),
            "source": "resume", "transcript_path": str(resumed_source),
        })
        receipt = runtime.receipt()
        self.assertEqual(receipt["session_id"], resumed_sid)
        self.assertEqual(receipt["source_path"], str(resumed_source))
        self.assertEqual(receipt["native_resume_confirmations"], 1)

    def test_no_configured_budget_starts_native_session_without_a_tool_policy_block(self):
        config = {"hash": "no-budget", "env": {}}
        native_sid = str(uuid4())
        source = self.root / f"{native_sid}.jsonl"
        source.write_text(json.dumps({
            "type": "user", "uuid": str(uuid4()), "sessionId": native_sid,
            "message": {"role": "user", "content": "继续当前本地任务。"},
        }) + "\n")
        self.usage(100, source, native_sid)
        runtime = TuiRuntime.create(cwd=self.cwd, session_id=str(uuid4()), configuration=config,
                                    configuration_reader=lambda _: config)
        start = runtime.on_hook({
            "hook_event_name": "SessionStart", "session_id": native_sid, "cwd": str(self.cwd),
            "source": "startup", "transcript_path": str(source),
        })
        self.assertEqual(runtime.receipt()["phase"], "running")
        self.assertIn("additionalContext", start["hookSpecificOutput"])
        tool = runtime.on_hook({
            "hook_event_name": "PreToolUse", "session_id": native_sid, "cwd": str(self.cwd),
            "transcript_path": str(source), "tool_use_id": "no-budget-tool", "tool_name": "Read",
            "tool_input": {"file_path": str(self.cwd / "unblocked.txt")},
        })
        self.assertEqual(tool, {})
        self.assertNotIn("permissionDecision", tool)
        self.assertNotIn("continue", tool)
        self.assertEqual(runtime.receipt()["phase"], "paused")


if __name__ == "__main__":
    unittest.main()
