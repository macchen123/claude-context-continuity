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
from claude_context_continuity import continuity, core, native_control, tmux_transport, tui_runtime
from claude_context_continuity.history import HistorySource
from claude_context_continuity.tui_runtime import TuiRuntime


class ContinuityCliExitCodeTests(unittest.TestCase):
    def _main_with_receipt(self, receipt):
        from contextlib import redirect_stdout
        import io

        with patch.object(sys, "argv", [
            "continuity.py", "context-request", "--context-id", str(uuid4()), "--handoff", "fixture",
        ]), patch.object(continuity, "dispatch", return_value=receipt), redirect_stdout(io.StringIO()) as output:
            code = continuity.main()
        self.assertEqual(json.loads(output.getvalue()), receipt)
        return code

    def test_paused_phase_receipt_returns_nonzero(self):
        self.assertEqual(self._main_with_receipt({"phase": "paused"}), 2)

    def test_queued_in_progress_receipt_returns_zero(self):
        self.assertEqual(self._main_with_receipt({"status": "queued", "phase": "in_progress"}), 0)

    def test_paused_status_receipt_returns_nonzero(self):
        self.assertEqual(self._main_with_receipt({"status": "paused"}), 2)


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
        self.control_binding = patch.object(native_control, "binding", side_effect=lambda cid: {
            "protocol": native_control.PROTOCOL, "context_id": cid,
            "socket_path": str(self.home / "runtime/control" / f"{cid}.sock")})
        self.control_binding.start()
        self.addCleanup(self.control_binding.stop)
        self.control_ready = patch.object(native_control, "ready", return_value=True)
        self.control_ready.start()
        self.addCleanup(self.control_ready.stop)
        version = patch.object(native_control, "require_supported_cli", return_value="2.1.266")
        version.start()
        self.addCleanup(version.stop)
        self.runtime._control_auth = "isolated-test-auth-not-a-real-secret"
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["tmux"] = self.binding
            state["native_control"] = native_control.binding(self.runtime.conversation_id)
            state["transport"] = "tmux_tui"
            self.runtime._save(state)
        self.inspector = patch.object(tmux_transport, "inspect", return_value=self.binding)
        self.capture = patch.object(tmux_transport, "capture", return_value="native TUI\n❯ \n")
        self.clear = patch.object(native_control, "send_clear")
        self.send = patch.object(native_control, "send_continuation")
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
        runtime._control_auth = "isolated-test-auth-not-a-real-secret"
        with core.lock(runtime.lock_path):
            state = runtime._state()
            state["tmux"] = self.binding
            state["native_control"] = native_control.binding(runtime.conversation_id)
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

    def test_durable_cron_startup_binding_survives_clear_and_resume(self):
        initial = self.runtime._state()["durable_cron_compat"]["scheduler_session_id"]
        sid, source = self.resume_source(150)
        self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear", "session_id": sid,
                              "cwd": str(self.cwd), "transcript_path": str(source)})
        self.assertEqual(self.runtime._state()["durable_cron_compat"]["scheduler_session_id"], initial)
        resumed_sid, resumed_source = self.resume_source(200)
        self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "resume", "session_id": resumed_sid,
                              "cwd": str(self.cwd), "transcript_path": str(resumed_source)})
        self.assertEqual(self.runtime._state()["durable_cron_compat"]["scheduler_session_id"], initial)
        self.assertEqual(self.runtime.receipt()["session_id"], resumed_sid)

    def test_durable_cron_post_hook_repairs_native_record_without_running_it(self):
        initial = self.sid
        self.sid, self.source = self.resume_source(150)
        self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear", "session_id": self.sid,
                              "cwd": str(self.cwd), "transcript_path": str(self.source)})
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["owned_pid"] = os.getpid()
            self.runtime._save(state)
        task = {"id": "a1234567", "cron": "* * * * *", "prompt": "report the time", "recurring": True,
                "createdAt": 123456, "createdBySessionId": self.sid, "createdByPid": os.getpid(),
                "createdByProcStart": "fixture-start"}
        path = self.cwd / ".claude/scheduled_tasks.json"
        core.atomic(path, {"tasks": [task]})
        core.atomic(path.with_name("scheduled_tasks.lock"), {
            "sessionId": initial, "pid": os.getpid(), "procStart": "fixture-start", "acquiredAt": 123})
        fields = {"tool_use_id": "cron-create", "tool_name": "CronCreate",
                  "tool_input": {"cron": task["cron"], "prompt": task["prompt"], "durable": True, "recurring": True},
                  "tool_response": {"id": task["id"], "durable": True, "recurring": True}}
        self.runtime.on_hook(self.hook("PreToolUse", **fields))
        response = self.runtime.on_hook(self.hook("PostToolUse", **fields))
        observed = json.loads(path.read_text())["tasks"][0]
        self.assertEqual(observed, {**task, "createdBySessionId": initial})
        self.assertEqual(self.runtime.receipt()["durable_cron_compat"]["last_result"]["status"], "applied")
        self.assertIn("不代表任务已自动触发", response["hookSpecificOutput"]["additionalContext"])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_durable_cron_compat_can_be_disabled_without_changing_native_arguments(self):
        with patch.dict(os.environ, {tui_runtime.DURABLE_CRON_COMPAT_ENV: "off"}):
            runtime, _, _ = self.resume_runtime()
        self.assertFalse(runtime.receipt()["durable_cron_compat"]["enabled"])
        with patch.dict(os.environ, {tui_runtime.DURABLE_CRON_COMPAT_ENV: "maybe"}):
            with self.assertRaisesRegex(ValueError, "on 或 off"):
                tui_runtime.create(self.cwd)

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
        self.assertIn(old_sid, self.send_mock.call_args.args[2])

    def test_resume_over_budget_defers_new_input_until_native_history_is_flushed(self):
        runtime, _, _ = self.resume_runtime()
        old_sid, old_source = self.resume_source(1200)
        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": old_sid,
                         "cwd": str(self.cwd), "source": "resume", "transcript_path": str(old_source)})
        blocked = runtime.on_hook({"hook_event_name": "UserPromptSubmit", "session_id": old_sid,
                                   "cwd": str(self.cwd), "prompt": "不要在检查前请求模型"})
        self.assertFalse(blocked["continue"])
        self.assertEqual(runtime.advance()["phase"], "rotation_requested")
        self.assertNotIn("resume_safe_boundary", runtime._state())
        self.clear_mock.assert_not_called()
        with old_source.open("a") as handle:
            handle.write(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": old_sid,
                "message": {"role": "user", "content": "不要在检查前请求模型"}}) + "\n")
        self.assertEqual(runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
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
        text = self.send_mock.call_args.args[2]
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
        self.assertIn(first["handoff"], self.send_mock.call_args.args[2])

    def test_manual_request_waits_only_for_direct_calls_not_the_input_draft(self):
        self.runtime.request_rotation("先结算当前直接调用，再按原任务继续。")
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="unfinished"))
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.advance()["phase"], "waiting_safe_boundary")
        self.clear_mock.assert_not_called()
        self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="unfinished"))
        self.runtime.on_hook(self.hook("Stop"))
        self.capture_mock.return_value = "native TUI\n❯ 正在输入\n"
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        self.capture_mock.assert_not_called()

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

    def test_queued_human_input_does_not_block_native_queued_clear(self):
        with self.source.open("a") as f:
            f.write(json.dumps({"type": "attachment", "uuid": str(uuid4()), "sessionId": self.sid,
                "isSidechain": False, "userType": "external", "attachment": {
                "type": "queued_command", "origin": {"kind": "human"}, "commandMode": "prompt",
                "source_uuid": self.sid, "prompt": "queued follow-up"}}) + "\n")
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        self.assertIn("queued follow-up", self.source.read_text())

    def test_accepted_continuation_does_not_time_out_during_long_thinking(self):
        self.threshold()
        self.runtime.advance()
        sid = str(uuid4())
        path = self.root / f"{sid}.jsonl"
        path.write_text("")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid,
            "cwd": str(self.cwd), "source": "clear", "transcript_path": str(path)})
        self.runtime.advance()
        text = self.send_mock.call_args.args[2]
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

    def test_user_draft_is_not_read_or_overwritten_by_native_clear(self):
        self.threshold()
        self.capture_mock.return_value = "native TUI\n❯ 正在编辑的输入\n"
        self.runtime.advance()
        self.clear_mock.assert_called_once()
        self.capture_mock.assert_not_called()
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

    def test_late_clear_confirmation_rebinds_after_concurrent_input_pause(self):
        self.threshold()
        self.runtime.advance()
        deferred = self.runtime.on_hook(self.hook("UserPromptSubmit", prompt="scheduled follow-up"))
        self.assertFalse(deferred["continue"])
        self.runtime._pause_external("原生投递确认暂时缺失")
        self.assertEqual(self.runtime.receipt()["phase"], "paused")
        sid, source = self.resume_source(125)
        result = self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear",
            "session_id": sid, "cwd": str(self.cwd), "transcript_path": str(source)})
        state = self.runtime._state()
        self.assertEqual(state["session_id"], sid)
        self.assertEqual(state["source_path"], str(source))
        self.assertTrue(state["rotation"]["clear"]["reset_seen"])
        self.assertEqual(state["rotation"]["clear"]["new_session_id"], sid)
        self.assertIn("history-search", result["hookSpecificOutput"]["additionalContext"])
        catalogue = list((self.runtime.directory / "history").glob(f"*-{sid}.json"))
        self.assertEqual(len(catalogue), 1)
        self.assertEqual(core.read_json(catalogue[0])["source_path"], str(source))
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_manual_clear_while_paused_restores_observation_without_old_handoff(self):
        self.runtime._pause_external("observation temporarily unavailable")
        sid, source = self.resume_source(125)
        result = self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear",
            "session_id": sid, "cwd": str(self.cwd), "transcript_path": str(source)})
        state = self.runtime._state()
        self.assertEqual(state["session_id"], sid)
        self.assertEqual(state["phase"], "running")
        self.assertIsNone(state["authorization"])
        self.assertIsNone(state["rotation"]["request"])
        self.assertIn("history-search", result["hookSpecificOutput"]["additionalContext"])
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_paused_automation_still_observes_usage_and_warns_once(self):
        self.runtime._pause_external("terminal input outcome is unknown")
        self.usage(450)
        first = self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="new-work"))
        state = self.runtime._state()
        self.assertEqual(state["phase"], "paused")
        self.assertEqual(state["usage"]["total_input_and_cache_tokens"], 450)
        self.assertIn("systemMessage", first)
        second = self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="new-work"))
        self.assertNotIn("systemMessage", second)
        self.assertNotIn("continue", first)
        self.assertFalse(self.runtime.receipt()["pending_tool_ids"])

    def test_invalid_clear_source_does_not_replace_bound_session(self):
        self.threshold()
        self.runtime.advance()
        sid = str(uuid4())
        self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear",
            "session_id": sid, "cwd": str(self.cwd), "transcript_path": str(self.source)})
        state = self.runtime._state()
        self.assertEqual(state["session_id"], self.sid)
        self.assertEqual(state["source_path"], str(self.source))
        self.assertFalse(state["rotation"]["clear"]["reset_seen"])

    def test_duplicate_clear_confirmation_does_not_create_another_generation(self):
        self.threshold()
        self.runtime.advance()
        sid, source = self.resume_source(125)
        event = {"hook_event_name": "SessionStart", "source": "clear", "session_id": sid,
                 "cwd": str(self.cwd), "transcript_path": str(source)}
        self.runtime.on_hook(event)
        generation = self.runtime._state()["window_generation"]
        self.runtime.on_hook(event)
        state = self.runtime._state()
        self.assertEqual(state["window_generation"], generation)
        self.assertEqual(state["native_clear_confirmations"], 1)
        self.assertNotEqual(state["phase"], "paused")
        self.assertEqual(len(list((self.runtime.directory / "history").glob(f"*-{sid}.json"))), 1)

    def test_tool_batch_stops_before_next_request_without_waiting_for_background_agent(self):
        self.runtime.on_hook(self.hook("SubagentStart", agent_id="still-running"))
        self.usage(800)
        result = self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[]))
        self.assertFalse(result["continue"])
        self.assertEqual(self.runtime.receipt()["active_child_handles"], ["still-running"])
        self.assertIsNone(self.runtime._state().get("stop_serial"))
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        sid, source = self.resume_source(125)
        self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear", "session_id": sid,
                              "cwd": str(self.cwd), "transcript_path": str(source)})
        self.runtime.advance()
        packet = json.loads(self.send_mock.call_args.args[2].split("\n", 1)[1].split("\n", 1)[1])
        self.assertEqual(packet["active_background_agents"], ["still-running"])

    def test_batch_accounts_multiple_outputs_and_bounds_native_response(self):
        self.config = {"hash": "output-budget", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000"}}
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["budget"] = {}
            self.runtime._save(state)
        self.usage(60000)
        response = {"stdout": "x" * 8000, "stderr": "", "interrupted": False}
        first = self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="out-one", tool_name="Bash",
                                               tool_response=response))
        self.assertNotIn("updatedToolOutput", first.get("hookSpecificOutput", {}))
        second = self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="out-two", tool_name="Bash",
                                                tool_response=response))
        replacement = second["hookSpecificOutput"]["updatedToolOutput"]
        self.assertFalse(replacement["interrupted"])
        self.assertLess(len(replacement["stdout"]), len(response["stdout"]))
        self.assertTrue(list((self.runtime.directory / "outputs").glob("*.json")))
        before = self.runtime._state()["output_budget"]["total_text_bytes"]
        result = self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[
            {"tool_use_id": "out-one", "tool_name": "Bash", "tool_input": {}, "tool_response": response},
            {"tool_use_id": "out-two", "tool_name": "Bash", "tool_input": {}, "tool_response": replacement},
        ]))
        self.assertFalse(result["continue"])
        self.assertEqual(self.runtime._state()["output_budget"]["total_text_bytes"], before)
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")

    def test_native_image_bytes_do_not_force_repeated_fresh_window_resets(self):
        response = {"type": "image", "file": {"base64": "A" * 200000, "type": "image/png"}}
        result = self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="figure", tool_name="Read",
                                                tool_response=response))
        self.assertNotIn("updatedToolOutput", result.get("hookSpecificOutput", {}))
        batch = self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[
            {"tool_use_id": "figure", "tool_name": "Read", "tool_input": {}, "tool_response": response}]))
        self.assertNotIn("continue", batch)
        self.assertTrue(self.runtime._state()["output_budget"]["items"]["figure"]["native_rich_or_unmeasured"])

    def test_deferred_input_is_read_from_native_history_and_not_replayed_as_a_command(self):
        self.threshold()
        self.runtime.advance()
        prompt = "只继续检查已有结果，不要重新执行实验。"
        deferred = self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=prompt))
        self.assertFalse(deferred["continue"])
        self.assertNotIn(prompt, self.runtime.state_path.read_text())
        with self.source.open("a") as handle:
            handle.write(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
                                     "message": {"role": "user", "content": prompt}}) + "\n")
        sid, source = self.resume_source(125)
        self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear", "session_id": sid,
                              "cwd": str(self.cwd), "transcript_path": str(source)})
        self.runtime.advance()
        text = self.send_mock.call_args.args[2]
        packet = json.loads(text.split("\n", 1)[1].split("\n", 1)[1])
        locator = packet["deferred_inputs"][0]
        self.assertEqual(HistorySource(self.source, self.sid).read(locator)["text"], prompt)
        self.assertEqual(packet["latest_instruction_locator"], locator)
        self.assertNotIn(prompt, text)
        self.clear_mock.assert_called_once()
        self.send_mock.assert_called_once()

    def test_current_native_plugin_keeps_history_on_the_existing_cli(self):
        manifest = tui_runtime._plugin_manifest(self.runtime.conversation_id)
        self.assertNotIn("mcpServers", manifest)
        self.assertIn("PostToolBatch", manifest["hooks"])
        prompt = tui_runtime.context_prompt()
        self.assertIn("history-search", prompt)
        self.assertIn("notes list/read/search/write/append", prompt)
        self.assertNotIn("ToolSearch", prompt)

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
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        for name in ("SubagentStop", "SubagentStop", "SubagentStart", "SubagentStop"):
            self.assertEqual(self.runtime.on_hook(self.hook(name, agent_id="same-agent")), {})
        self.assertEqual(self.runtime.receipt()["active_child_handles"], [])
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
        notice = self.runtime.on_hook(self.hook("UserPromptSubmit", prompt="继续正常任务"))
        self.assertIn("systemMessage", notice)
        self.assertNotIn("continue", notice)
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
        self.capture_mock.side_effect = AssertionError("原生队列投递不得捕获或改写输入框")
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_recovery_without_bound_native_credentials_does_not_start_or_change_state(self):
        self.runtime._pause_external("hook_state_or_source_drift")
        before = self.runtime.state_path.read_bytes()
        environment = {native_control.AUTH_ENV: "", "CLAUDE_CONTINUITY_ID": self.runtime.conversation_id,
                       native_control.SOCKET_ENV: self.runtime._state()["native_control"]["socket_path"]}
        with patch.dict(os.environ, environment), patch.object(tui_runtime.subprocess, "Popen") as spawn:
            spawn.return_value.pid = 123456
            with self.assertRaisesRegex(ValueError, "凭证|通道"):
                tui_runtime.recover(self.runtime.conversation_id, self.sid)
            spawn.assert_not_called()
        self.assertEqual(self.runtime.state_path.read_bytes(), before)

    def test_recovery_cli_starts_only_an_unowned_controller(self):
        from claude_context_continuity import continuity
        self.runtime._pause_external("hook_state_or_source_drift")
        args = continuity.parser().parse_args(["tui-recover", "--context-id", self.runtime.conversation_id,
                                               "--session-id", self.sid])
        environment = native_control.environment(self.runtime._state()["native_control"], self.runtime._control_auth)
        environment["CLAUDE_CONTINUITY_ID"] = self.runtime.conversation_id
        with patch.dict(os.environ, environment), patch("claude_context_continuity.tui_runtime.subprocess.Popen") as spawn:
            spawn.return_value.pid = 123456
            result = continuity.dispatch(args)
            self.assertEqual(result["controller_pid"], 123456)
            self.assertFalse(self.runtime._state()["at_turn_boundary"])
            self.assertNotIn("--model", spawn.call_args.args[0])
            self.assertEqual(spawn.call_args.kwargs["env"][native_control.AUTH_ENV], self.runtime._control_auth)
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
        child_env = create.call_args.args[3]
        self.assertEqual(child_env["CLAUDE_CONTINUITY_ID"], runtime.conversation_id)
        self.assertEqual(child_env["CLAUDE_BG_BACKEND"], "daemon")
        self.assertNotIn("CLAUDE_CODE_SESSION_KIND", child_env)
        self.assertEqual(child_env[native_control.SOCKET_ENV], runtime._state()["native_control"]["socket_path"])
        self.assertTrue(child_env[native_control.AUTH_ENV] == runtime._control_auth)
        self.assertNotIn(runtime._control_auth, runtime.state_path.read_text())
        self.assertNotIn("CLAUDECODE", child_env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", child_env)
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
        self.assertIn("systemMessage", tool)
        self.assertNotIn("permissionDecision", tool)
        self.assertNotIn("continue", tool)
        self.assertEqual(runtime.receipt()["phase"], "paused")

    def _managed_host_only_window(self, usage=1200):
        """Create an authentic managed continuation window with no local user root."""
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        host_sid = str(uuid4())
        host_source = self.root / f"{host_sid}.jsonl"
        host_source.write_text("")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": host_sid,
                              "cwd": str(self.cwd), "source": "clear",
                              "transcript_path": str(host_source)})
        self.assertEqual(self.runtime.advance()["phase"], "awaiting_continuation")
        continuation = self.send_mock.call_args.args[2]
        host_source.write_text(json.dumps({
            "type": "user", "uuid": str(uuid4()), "sessionId": host_sid,
            "message": {"role": "user", "content": continuation},
        }) + "\n")
        self.usage(usage, host_source, host_sid)
        self.runtime.on_hook({"hook_event_name": "Stop", "session_id": host_sid,
                              "cwd": str(self.cwd), "transcript_path": str(host_source),
                              "last_assistant_message": "真实形状的公开结果"})
        self.assertEqual(self.runtime.receipt()["phase"], "running")
        self.assertIsNone(HistorySource(host_source, host_sid).instruction_bounds()["first"])
        return host_sid, host_source

    @staticmethod
    def _catalogue_pairs(runtime):
        return {
            (entry["session_id"], entry["source_path"])
            for path in (runtime.directory / "history").glob("*.json")
            for entry in [core.read_json(path)]
        }

    def test_same_manager_resume_of_host_only_managed_window_uses_inherited_authentication(self):
        host_sid, host_source = self._managed_host_only_window()
        before = self.runtime._state()
        self.clear_mock.reset_mock()
        self.send_mock.reset_mock()

        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": host_sid,
                              "cwd": str(self.cwd), "source": "resume",
                              "transcript_path": str(host_source)})
        rebound = self.runtime._state()
        self.assertEqual(rebound["phase"], "running")
        self.assertEqual(rebound["session_id"], host_sid)
        self.assertEqual(rebound["authorization"]["root_instruction_locator"]["session_id"], self.sid)
        self.assertGreaterEqual(rebound["window_generation"], before["window_generation"])
        self.assertIsNone(HistorySource(host_source, host_sid).instruction_bounds()["last"])
        self.assertIsNone(rebound["resume_snapshot"]["instruction_head"])

        outcome = self.runtime.advance()
        self.assertEqual(outcome["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_new_manager_resume_of_host_only_managed_window_copies_lineage_and_rotates_safely(self):
        host_sid, host_source = self._managed_host_only_window()
        owner_state = self.runtime.state_path.read_bytes()
        owner_references = self._catalogue_pairs(self.runtime)
        runtime, startup_sid, startup_source = self.resume_runtime()
        self.clear_mock.reset_mock()
        self.send_mock.reset_mock()

        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": host_sid,
                         "cwd": str(self.cwd), "source": "resume",
                         "transcript_path": str(host_source)})
        rebound = runtime._state()
        self.assertEqual(rebound["phase"], "running")
        self.assertEqual(rebound["session_id"], host_sid)
        self.assertNotEqual(rebound["session_id"], startup_sid)
        self.assertNotEqual(rebound["source_path"], str(startup_source))
        self.assertEqual(rebound["usage"]["total_input_and_cache_tokens"], 1200)
        self.assertEqual(rebound["authorization"]["root_instruction_locator"]["session_id"], self.sid)
        self.assertIsNone(rebound["resume_snapshot"]["instruction_head"])
        self.assertTrue(owner_references.issubset(self._catalogue_pairs(runtime)))
        self.assertEqual(self.runtime.state_path.read_bytes(), owner_state)

        outcome = runtime.advance()
        self.assertEqual(outcome["phase"], "clear_sent")
        self.assertEqual(outcome["automatic_rotations"], 1)
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_no_owner_host_signal_pauses_actual_session_until_a_real_user_hook_recovers(self):
        runtime, startup_sid, startup_source = self.resume_runtime()
        host_sid = str(uuid4())
        host_source = self.root / f"{host_sid}.jsonl"
        fabricated = tui_runtime._runtime_message('{"kind":"fabricated"}')
        host_source.write_text(json.dumps({
            "type": "user", "uuid": str(uuid4()), "sessionId": host_sid,
            "message": {"role": "user", "content": fabricated},
        }) + "\n")
        self.usage(125, host_source, host_sid)

        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": host_sid,
                         "cwd": str(self.cwd), "source": "resume",
                         "transcript_path": str(host_source)})
        paused = runtime._state()
        self.assertEqual(paused["phase"], "paused")
        self.assertEqual(paused["session_id"], host_sid)
        self.assertNotEqual(paused["session_id"], startup_sid)
        self.assertEqual(paused["source_path"], str(host_source))
        self.assertNotEqual(paused["source_path"], str(startup_source))
        self.assertIsNone(paused["authorization"])
        self.assertTrue(paused["observation_only_pause"])
        self.assertEqual(paused["usage"]["total_input_and_cache_tokens"], 125)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

        prompt = "这是同一来源后来写入的真实用户指令。"
        with host_source.open("a") as handle:
            handle.write(json.dumps({
                "type": "user", "uuid": str(uuid4()), "sessionId": host_sid,
                "message": {"role": "user", "content": prompt},
            }) + "\n")
        before_hook = host_source.read_bytes()
        runtime.on_hook({"hook_event_name": "UserPromptSubmit", "session_id": host_sid,
                         "cwd": str(self.cwd), "transcript_path": str(host_source), "prompt": prompt})
        recovered = runtime._state()
        self.assertEqual(recovered["phase"], "running")
        self.assertEqual(recovered["session_id"], host_sid)
        self.assertEqual(recovered["authorization"]["root_instruction_locator"]["session_id"], host_sid)
        self.assertNotIn("observation_only_pause", recovered)
        self.assertNotIn("diagnostic", recovered)
        self.assertEqual(host_source.read_bytes(), before_hook)
        runtime.advance()
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_cross_manager_resume_rejects_a_tampered_inherited_root(self):
        host_sid, host_source = self._managed_host_only_window()
        records = [json.loads(line) for line in self.source.read_text().splitlines()]
        self.assertEqual(records[0]["message"]["content"], "只继续这项已经授权的任务。")
        records[0]["message"]["content"] = "伪造的根用户指令。"
        self.source.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        runtime, startup_sid, startup_source = self.resume_runtime()
        self.clear_mock.reset_mock()
        self.send_mock.reset_mock()

        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": host_sid,
                         "cwd": str(self.cwd), "source": "resume",
                         "transcript_path": str(host_source)})
        rejected = runtime._state()
        self.assertEqual(rejected["phase"], "paused")
        self.assertEqual(rejected["session_id"], startup_sid)
        self.assertEqual(rejected["source_path"], str(startup_source))
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_cross_manager_resume_rejects_an_exact_lineage_with_wrong_cwd(self):
        host_sid, host_source = self._managed_host_only_window()
        other_cwd = self.root / "wrong-lineage-cwd"
        other_cwd.mkdir()
        with core.lock(self.runtime.lock_path):
            owner = self.runtime._state()
            owner["cwd"] = str(other_cwd)
            self.runtime._save(owner)
        runtime, startup_sid, startup_source = self.resume_runtime()
        self.clear_mock.reset_mock()
        self.send_mock.reset_mock()

        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": host_sid,
                         "cwd": str(self.cwd), "source": "resume",
                         "transcript_path": str(host_source)})
        rejected = runtime._state()
        self.assertEqual(rejected["phase"], "paused")
        self.assertEqual(rejected["session_id"], startup_sid)
        self.assertEqual(rejected["source_path"], str(startup_source))
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_unrelated_or_missing_context_directories_do_not_block_an_ordinary_resume(self):
        runtime, _, _ = self.resume_runtime()
        (self.home / "runtime" / "contexts" / str(uuid4())).mkdir()
        unrelated = TuiRuntime.create(cwd=self.cwd, session_id=str(uuid4()), configuration=self.config,
                                      configuration_reader=lambda _: self.config)
        self.assertNotEqual(unrelated.conversation_id, runtime.conversation_id)
        resumed_sid, resumed_source = self.resume_source(800)

        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": resumed_sid,
                         "cwd": str(self.cwd), "source": "resume",
                         "transcript_path": str(resumed_source)})
        rebound = runtime.receipt()
        self.assertEqual(rebound["phase"], "running")
        self.assertEqual(rebound["session_id"], resumed_sid)
        self.assertEqual(rebound["source_path"], str(resumed_source))
        self.assertEqual(rebound["authorization"]["root_instruction_locator"]["session_id"], resumed_sid)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_cross_manager_resume_rejects_conflicting_verified_lineages(self):
        host_sid, host_source = self._managed_host_only_window()
        alternate_sid, alternate_source = self.resume_source(125)
        alternate_bounds = HistorySource(alternate_source, alternate_sid).instruction_bounds()
        conflicting = TuiRuntime.create(cwd=self.cwd, session_id=str(uuid4()), configuration=self.config,
                                        configuration_reader=lambda _: self.config)
        with core.lock(conflicting.lock_path):
            state = conflicting._state()
            state.update(session_id=alternate_sid, source_path=str(alternate_source), window_generation=0,
                         authorization={"root_instruction_locator": alternate_bounds["first"],
                                        "latest_instruction_locator": alternate_bounds["last"]},
                         rotation={"generation": 1, "request": None, "clear": None}, phase="running")
            conflicting._catalogue_source(state)
            state.update(session_id=host_sid, source_path=str(host_source), window_generation=1)
            conflicting._catalogue_source(state)
            conflicting._save(state)
        runtime, startup_sid, startup_source = self.resume_runtime()
        self.clear_mock.reset_mock()
        self.send_mock.reset_mock()

        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": host_sid,
                         "cwd": str(self.cwd), "source": "resume",
                         "transcript_path": str(host_source)})
        rejected = runtime.receipt()
        self.assertEqual(rejected["phase"], "paused")
        self.assertEqual(rejected["session_id"], startup_sid)
        self.assertEqual(rejected["source_path"], str(startup_source))
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
