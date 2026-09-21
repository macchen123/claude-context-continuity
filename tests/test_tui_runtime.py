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

    def batch_records(self, tool_ids, value=820):
        request_id = str(uuid4())
        assistant = {"type": "assistant", "uuid": str(uuid4()), "sessionId": self.sid,
            "cwd": str(self.cwd), "message": {"id": request_id, "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {}}
                        for tool_id in tool_ids], "model": "native-model", "usage": {
            "input_tokens": value, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}
        results = [{"type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                        "content": "fixture result"}]}}
                   for tool_id in tool_ids]
        return assistant, results

    def append_records(self, *records):
        with self.source.open("a") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

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

    @staticmethod
    def peer_message(sender, body):
        envelope = f'<agent-message from="{sender}">\n{body}\n</agent-message>'
        return envelope, ("Another Claude session sent a message:\n" + envelope
                          + "\n<system-reminder>Native host safety footer.</system-reminder>")

    def threshold(self):
        self.usage(800)
        self.runtime.on_hook(self.hook("Stop"))

    def wait_for_unknown_continuation(self, reason="native continuation delivery is unknown"):
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["phase"] = "awaiting_continuation"
            state["continuation_hash"] = core.digest("unconfirmed fixture continuation")
            self.runtime._wait(state, reason)
            self.runtime._save(state)

    def assert_waiting(self, receipt, phase):
        self.assertEqual(receipt["phase"], phase)
        self.assertIsInstance(receipt["reconciliation"], dict)
        self.assertTrue(receipt["reconciliation"]["reason"])
        self.assertIsNone(receipt["pause_reason"])

    def test_idle_boundary_parses_once_and_does_not_rewrite_identical_state(self):
        self.runtime.on_hook(self.hook("Stop"))
        before = self.runtime.state_path.stat()
        with patch.object(HistorySource, "_records", autospec=True, side_effect=HistorySource._records) as reads, \
                patch.object(core.os, "fsync", wraps=os.fsync) as sync:
            for _ in range(6):
                self.assertEqual(self.runtime.advance()["phase"], "running")
        self.assertEqual(reads.call_count, 1)
        sync.assert_not_called()
        after = self.runtime.state_path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        stamp, state_hash = self.runtime._idle_observation
        self.assertEqual(len(stamp), 7)
        self.assertEqual(len(state_hash), 64)
        self.clear_mock.assert_not_called()

    def test_idle_snapshot_invalidates_when_live_budget_changes(self):
        self.runtime.on_hook(self.hook("Stop"))
        self.runtime.advance()
        self.config = {"hash": "smaller", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100"}}
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_idle_snapshot_detects_same_size_rewrite_even_with_restored_mtime(self):
        self.runtime.on_hook(self.hook("Stop"))
        self.runtime.advance()
        before = self.source.stat()
        content = self.source.read_text().replace('"input_tokens": 125', '"input_tokens": 800')
        self.source.write_text(content)
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.source.stat().st_size, before.st_size)
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_idle_snapshot_revalidates_replaced_source_identity(self):
        self.runtime.on_hook(self.hook("Stop"))
        self.runtime.advance()
        before = self.source.stat()
        replacement = self.root / "replacement.jsonl"
        replacement.write_text(self.source.read_text().replace(self.sid, str(uuid4())))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(self.source)
        self.assert_waiting(self.runtime.advance(), "running")
        self.clear_mock.assert_not_called()

    def test_idle_snapshot_detects_new_user_input_without_reusing_stop(self):
        self.runtime.on_hook(self.hook("Stop"))
        self.runtime.advance()
        with self.source.open("a") as handle:
            handle.write(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
                                    "message": {"role": "user", "content": "停止之前的任务。"}}) + "\n")
        self.assertEqual(self.runtime.advance()["phase"], "running")
        self.assertFalse(self.runtime._state()["at_turn_boundary"])
        self.clear_mock.assert_not_called()

    def test_incomplete_runtime_tail_waits_until_append_finishes(self):
        self.threshold()
        with self.source.open("a") as handle:
            handle.write(json.dumps({"type": "progress", "uuid": str(uuid4()), "sessionId": self.sid})[:-1])
        self.assertEqual(self.runtime.advance()["phase"], "running")
        self.clear_mock.assert_not_called()
        with self.source.open("a") as handle:
            handle.write("}\n")
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_complete_malformed_runtime_tail_is_not_deferred(self):
        self.threshold()
        with self.source.open("a") as handle:
            handle.write("not-json\n")
        self.assert_waiting(self.runtime.advance(), "running")
        self.clear_mock.assert_not_called()

    def test_serve_survives_real_state_lock_contention_and_records_recovery(self):
        lock = core.lock(self.runtime.lock_path)
        lock.__enter__()
        held = True
        real_lock = core.lock
        snapshots = []

        def bounded_lock(path, **kwargs):
            return real_lock(path, wait_seconds=0 if Path(path) == self.runtime.lock_path else 5)

        def release(_):
            nonlocal held
            snapshots.append(json.loads((self.runtime.directory / "controller-diagnostic.json").read_text()))
            self.assertTrue(tui_runtime._controller_locked(self.runtime))
            lock.__exit__(None, None, None)
            held = False

        try:
            with patch.object(tui_runtime, "TuiRuntime", return_value=self.runtime), \
                    patch.object(core, "lock", side_effect=bounded_lock), \
                    patch.object(tui_runtime.time, "sleep", side_effect=release), \
                    patch.object(self.runtime, "advance", return_value={"phase": "closed"}) as advance:
                self.assertEqual(tui_runtime.serve(self.runtime.conversation_id)["phase"], "closed")
        finally:
            if held:
                lock.__exit__(None, None, None)
        advance.assert_called_once()
        self.assertEqual(snapshots[0]["status"], "waiting_for_state_lock")
        diagnostic = json.loads((self.runtime.directory / "controller-diagnostic.json").read_text())
        self.assertEqual(diagnostic["status"], "running")
        self.assertEqual(diagnostic["last_error"], "state_lock_busy")
        self.assertFalse(tui_runtime._controller_locked(self.runtime))
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_serve_recovers_io_failure_without_replaying_or_logging_exception_text(self):
        with patch.object(tui_runtime, "TuiRuntime", return_value=self.runtime), \
                patch.object(self.runtime, "advance", side_effect=[
                    OSError("private-exception-value"), {"phase": "closed"},
                ]) as advance:
            outcome = tui_runtime.serve(self.runtime.conversation_id)
        self.assertEqual(outcome["phase"], "closed")
        self.assertEqual(advance.call_count, 2)
        raw = (self.runtime.directory / "controller-diagnostic.json").read_text()
        self.assertNotIn("private-exception-value", raw)
        self.assertEqual(json.loads(raw)["last_error"], "state_or_io_failure")
        self.assertIsInstance(self.runtime.receipt()["reconciliation"], dict)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_serve_keeps_retrying_when_state_cannot_be_persisted(self):
        with patch.object(tui_runtime, "TuiRuntime", return_value=self.runtime), \
                patch.object(self.runtime, "_save", side_effect=OSError("unwritable")), \
                patch.object(tui_runtime.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                tui_runtime.serve(self.runtime.conversation_id)
        self.assertTrue((self.runtime.directory / "controller-diagnostic.json").is_file())
        self.assertFalse(tui_runtime._controller_locked(self.runtime))

    def test_status_does_not_trust_running_or_live_pid_without_controller_lock(self):
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["controller_pid"] = state["owned_pid"] = os.getpid()
            self.runtime._save(state)
        before = self.runtime.state_path.read_bytes()
        args = continuity.parser().parse_args(["tui-status", "--context-id", self.runtime.conversation_id])
        outcome = continuity.dispatch(args)
        self.assertEqual(outcome["phase"], "running")
        self.assertEqual(outcome["health"]["status"], "controller_unavailable")
        self.assertTrue(outcome["health"]["controller_pid_alive"])
        self.assertFalse((self.runtime.directory / "controller.lock").exists())
        with core.lock(self.runtime.directory / "controller.lock"):
            self.assertEqual(continuity.dispatch(args)["health"]["status"], "healthy")
        self.assertEqual(self.runtime.state_path.read_bytes(), before)

    def test_attach_warns_without_spawning_or_timing_out_interactive_client(self):
        import io
        from contextlib import redirect_stderr
        with patch.object(tmux_transport, "attach_argv", return_value=["tmux", "attach"]), \
                patch.object(tui_runtime.subprocess, "run") as attach, \
                patch.object(tui_runtime.subprocess, "Popen") as spawn, redirect_stderr(io.StringIO()) as stderr:
            attach.return_value.returncode = 0
            result = tui_runtime.attach(self.runtime.conversation_id)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["health"]["status"], "controller_unavailable")
        self.assertIn("attach 只连接原生终端", stderr.getvalue())
        attach.assert_called_once_with(["tmux", "attach"], check=False)
        spawn.assert_not_called()
        self.clear_mock.assert_not_called()

    def test_unavailable_native_session_does_not_change_state_or_spawn_on_recovery(self):
        self.runtime._pause_external("observation failed")
        before = self.runtime.state_path.read_bytes()
        environment = native_control.environment(self.runtime._state()["native_control"], self.runtime._control_auth)
        environment["CLAUDE_CONTINUITY_ID"] = self.runtime.conversation_id
        with patch.dict(os.environ, environment), \
                patch.object(tmux_transport, "inspect", side_effect=tmux_transport.TmuxTransportError("unavailable")), \
                patch.object(tui_runtime.subprocess, "Popen") as spawn:
            with self.assertRaises(ValueError):
                tui_runtime.recover(self.runtime.conversation_id, self.sid)
        spawn.assert_not_called()
        self.assertEqual(self.runtime.state_path.read_bytes(), before)
        self.send_mock.assert_not_called()
        self.clear_mock.assert_not_called()

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
        self.assertEqual(self.runtime.receipt()["phase"], "running")
        self.assertIsNone(self.runtime.receipt()["reconciliation"])
        self.clear_mock.assert_not_called()

    def test_resume_with_pr_link_metadata_binds_target_session(self):
        runtime, startup_sid, _ = self.resume_runtime()
        target_sid, target_source = self.resume_source(125)
        with target_source.open("a") as handle:
            handle.write(json.dumps({"type": "pr-link", "sessionId": target_sid, "prNumber": 1,
                                     "prUrl": "https://github.com/example/project/pull/1",
                                     "prRepository": "example/project", "timestamp": "2026-09-18T00:00:00.000Z"}) + "\n")
        result = runtime.on_hook({"hook_event_name": "SessionStart", "source": "resume",
                                  "session_id": target_sid, "cwd": str(self.cwd),
                                  "transcript_path": str(target_source)})
        self.assertNotIn("continue", result)
        observed = runtime.receipt()
        self.assertEqual(observed["phase"], "running")
        self.assertNotEqual(observed["session_id"], startup_sid)
        self.assertEqual(observed["session_id"], target_sid)
        self.assertEqual(observed["source_path"], str(target_source))
        self.assertEqual(observed["native_resume_confirmations"], 1)
        self.assertEqual(observed["authorization"]["root_instruction_locator"]["session_id"], target_sid)
        self.assertEqual(observed["usage"]["total_input_and_cache_tokens"], 125)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

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
        self.assertEqual(runtime.advance()["phase"], "running")
        self.assertNotIn("resume_safe_boundary", runtime._state())
        self.assertEqual(len(runtime._state()["deferred_inputs"]), 1)
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
        self.assert_waiting(rejected, "running")
        self.assertEqual(rejected["session_id"], initial_sid)
        self.assertEqual(rejected["source_path"], str(initial_source))
        runtime.advance()
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_resume_invalidates_a_boundary_changed_before_first_clear(self):
        runtime, _, _ = self.resume_runtime()
        old_sid, old_source = self.resume_source(1200)
        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": old_sid,
                         "cwd": str(self.cwd), "source": "resume", "transcript_path": str(old_source)})
        with old_source.open("a") as f:
            f.write(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": old_sid,
                "message": {"role": "user", "content": "并发来源的新输入。"}}) + "\n")
        outcome = runtime.advance()
        self.assert_waiting(outcome, "running")
        self.assertNotIn("resume_safe_boundary", runtime._state())
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_resume_preserves_historical_unsettled_ids_without_replaying_them(self):
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
        resumed = runtime.receipt()
        self.assertEqual(resumed["phase"], "running")
        self.assertEqual(resumed["session_id"], old_sid)
        self.assertEqual(resumed["source_path"], str(old_source))
        self.assertNotEqual(resumed["session_id"], initial_sid)
        self.assertEqual(resumed["resume_historical_pending_tool_ids"], ["resume-pending-tool"])
        self.assertEqual(resumed["pending_tool_ids"], [])
        self.assertEqual(runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

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

    def test_missing_native_clear_ack_keeps_waiting_without_fallback(self):
        self.threshold()
        self.runtime.advance()
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        self.assert_waiting(self.runtime.advance(), "clear_sent")
        self.assert_waiting(self.runtime.recover_observation(self.sid), "clear_sent")
        self.runtime.advance()
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_clear_transport_failure_is_never_replayed(self):
        self.threshold()
        self.clear_mock.side_effect = RuntimeError("unknown transport result")
        self.assert_waiting(self.runtime.advance(), "clear_sent")
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
        self.runtime._wait_external("原生投递确认暂时缺失")
        self.assert_waiting(self.runtime.receipt(), "clear_sent")
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

    def _observe_window_usage(self, value, window, *, reset_budget=False):
        self.config = {"hash": f"budget-{window}", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(window)}}
        if reset_budget:
            with core.lock(self.runtime.lock_path):
                state = self.runtime._state()
                state["budget"] = {}
                self.runtime._save(state)
        self.usage(value)
        self.runtime.on_hook(self.hook("Stop"))

    def test_paused_budget_blocks_repeated_high_usage_input_and_preserves_stale_clear_receipts(self):
        self._observe_window_usage(515_908, 600_000, reset_budget=True)
        self._observe_window_usage(575_640, 600_000)
        self.assertEqual(self.runtime._state()["usage"]["total_input_and_cache_tokens"], 575_640)
        self.assertEqual(self.runtime._state()["budget"]["guard_tokens"], 141_268)
        old_sid = str(uuid4())
        old_source = self.root / f"{old_sid}.jsonl"
        old_source.write_text("")
        prior = {"session_id": old_sid, "source_path": str(old_source),
                 "prompt_hash": core.digest("unresolved deferred peer"), "after_byte": 0,
                 "turn_generation": 0}
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["window_generation"] = 7
            state["rotation"] = {
                "generation": 7,
                "request": {"generation": 7, "session_id": old_sid, "source_path": str(old_source),
                            "handoff": "existing handoff", "notes": ""},
                "clear": {"generation": 7, "old_session_id": old_sid, "command_id": str(uuid4()),
                          "reset_seen": True, "new_session_id": self.sid},
            }
            state["deferred_inputs"] = [prior]
            self.runtime._save(state)
        self.wait_for_unknown_continuation("previous rollover acknowledgement is unresolved")
        stale_rotation = json.loads(json.dumps(self.runtime._state()["rotation"]))
        first_prompt = "first blocked paused input"
        first_after_byte = self.source.stat().st_size
        first = self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=first_prompt))
        self.assertFalse(first["continue"])
        self.assertIn("正在等待原生确认", first["stopReason"])
        self.assertIn("下一次模型请求", first["stopReason"])
        self.assertIn("systemMessage", first)
        first_state = self.runtime._state()
        self.assert_waiting(first_state, "awaiting_continuation")
        self.assertEqual(first_state["rotation"], stale_rotation)
        self.assertEqual(first_state["budget"]["handoff_reported"], 1)
        self.assertEqual(first_state["deferred_inputs"][:-1], [prior])
        self.assertEqual(first_state["deferred_inputs"][-1], {
            "session_id": self.sid, "source_path": str(self.source),
            "prompt_hash": core.digest(first_prompt), "after_byte": first_after_byte,
            "turn_generation": first_state["turn_generation"],
        })
        second_prompt = "second blocked paused input"
        second = self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=second_prompt))
        self.assertFalse(second["continue"])
        self.assertIn("正在等待原生确认", second["stopReason"])
        self.assertNotIn("systemMessage", second)
        state = self.runtime._state()
        self.assertEqual(state["rotation"], stale_rotation)
        self.assertEqual([item["prompt_hash"] for item in state["deferred_inputs"]], [
            prior["prompt_hash"], core.digest(first_prompt), core.digest(second_prompt),
        ])
        clear_escape = self.runtime.on_hook(self.hook("UserPromptSubmit", prompt="/clear"))
        self.assertNotIn("continue", clear_escape)
        self.assertEqual(self.runtime._state()["rotation"], stale_rotation)
        self.assertEqual(len(self.runtime._state()["deferred_inputs"]), 3)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_paused_input_counts_pending_output_and_utf8_bytes(self):
        self._observe_window_usage(500, 1_000, reset_budget=True)
        self.wait_for_unknown_continuation()
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            accounting = self.runtime._output_accounting(state)
            accounting["items"]["pending-output"] = {"text_bytes": 240, "raw_bytes": 240, "defer": False}
            accounting["total_text_bytes"] = 240
            self.runtime._save(state)
        prompt = "界界界界"
        after_byte = self.source.stat().st_size
        blocked = self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=prompt))
        self.assertFalse(blocked["continue"])
        self.assertIn("正在等待原生确认", blocked["stopReason"])
        state = self.runtime._state()
        self.assert_waiting(state, "awaiting_continuation")
        self.assertEqual(state["output_budget"]["total_text_bytes"], 240)
        self.assertEqual(state["deferred_inputs"], [{
            "session_id": self.sid, "source_path": str(self.source),
            "prompt_hash": core.digest(prompt), "after_byte": after_byte,
            "turn_generation": state["turn_generation"],
        }])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_paused_batch_blocks_projected_pending_output_without_rotation(self):
        self._observe_window_usage(500, 1_000, reset_budget=True)
        self.wait_for_unknown_continuation()
        before_rotation = json.loads(json.dumps(self.runtime._state()["rotation"]))
        response = {"stdout": "x" * 300}
        batch = self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[{
            "tool_use_id": "pending-output", "tool_name": "Bash", "tool_input": {}, "tool_response": response,
        }]))
        self.assertFalse(batch["continue"])
        self.assertIn("正在等待原生确认", batch["stopReason"])
        self.assertIn("保留上下文", batch["stopReason"])
        state = self.runtime._state()
        self.assert_waiting(state, "awaiting_continuation")
        self.assertEqual(state["rotation"], before_rotation)
        item = state["output_budget"]["items"]["pending-output"]
        self.assertGreater(item["text_bytes"], 250)
        self.assertTrue(item["defer"])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_paused_batch_stops_for_a_defer_marked_result_even_when_projection_fits(self):
        self._observe_window_usage(500, 1_000, reset_budget=True)
        self.wait_for_unknown_continuation()
        before_rotation = json.loads(json.dumps(self.runtime._state()["rotation"]))
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            accounting = self.runtime._output_accounting(state)
            accounting["items"]["deferred-output"] = {"text_bytes": 0, "raw_bytes": 999, "defer": True}
            self.runtime._save(state)
        batch = self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[]))
        self.assertFalse(batch["continue"])
        self.assertIn("正在等待原生确认", batch["stopReason"])
        state = self.runtime._state()
        self.assert_waiting(state, "awaiting_continuation")
        self.assertEqual(state["rotation"], before_rotation)
        self.assertTrue(state["output_budget"]["items"]["deferred-output"]["defer"])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_paused_below_reserve_input_and_batch_remain_usable(self):
        self._observe_window_usage(100, 1_000, reset_budget=True)
        self.wait_for_unknown_continuation()
        batch = self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[]))
        prompt = self.runtime.on_hook(self.hook("UserPromptSubmit", prompt="short paused input"))
        self.assertNotIn("continue", batch)
        self.assertNotIn("continue", prompt)
        self.assert_waiting(self.runtime._state(), "awaiting_continuation")
        self.assertEqual(self.runtime._state().get("deferred_inputs", []), [])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_running_batch_rotation_behavior_is_unchanged(self):
        self.usage(800)
        batch = self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[]))
        self.assertFalse(batch["continue"])
        self.assertIn("正在自动切换上下文", batch["stopReason"])
        self.assertEqual(self.runtime.receipt()["phase"], "rotation_requested")
        self.assertEqual(self.runtime.receipt()["automatic_rotations"], 1)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_paused_automation_still_observes_usage_and_warns_once(self):
        self.wait_for_unknown_continuation()
        self.usage(450)
        first = self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="new-work"))
        state = self.runtime._state()
        self.assert_waiting(state, "awaiting_continuation")
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
        self.assert_waiting(self.runtime.recover_observation(self.sid), "clear_sent")
        self.runtime.advance()
        self.assertTrue(self.runtime.receipt()["native_session_uncertain"])
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

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
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_bytes(b"")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear", "session_id": sid,
                              "cwd": str(self.cwd), "transcript_path": str(source)})
        self.runtime.advance()
        packet = json.loads(self.send_mock.call_args.args[2].split("\n", 1)[1].split("\n", 1)[1])
        self.assertEqual(packet["active_background_agents"], ["still-running"])

    def test_batch_rotates_after_late_transcript_flush_without_another_prompt(self):
        self.usage(800)
        response = self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[{"tool_use_id": "late"}]))
        self.assertFalse(response["continue"])
        captured = self.runtime._state()["tool_batch_boundary"]["usage_locator"]
        assistant, results = self.batch_records(["late"])
        self.append_records(assistant, *results)
        self.assertNotEqual(HistorySource(self.source, self.sid).latest_usage()["locator"], captured)
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_text("")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear", "session_id": sid,
                              "cwd": str(self.cwd), "transcript_path": str(source)})
        self.runtime.advance()
        text = self.send_mock.call_args.args[2]
        with source.open("a") as handle:
            handle.write(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": sid,
                                     "isMeta": True, "message": {"role": "user", "content": text}}) + "\n")
        self.assertEqual(self.runtime.advance()["phase"], "running")
        self.assertTrue(self.runtime.receipt()["continuation_observed"])
        self.send_mock.assert_called_once()

    def test_batch_waits_for_every_call_and_result_to_reach_history(self):
        self.usage(800)
        self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[
            {"tool_use_id": "first"}, {"tool_use_id": "second"}]))
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        assistant, results = self.batch_records(["first", "second"], value=800)
        self.append_records(assistant)
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.append_records(results[0])
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.append_records(results[1])
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_batch_accepts_later_record_from_the_same_model_response(self):
        assistant, results = self.batch_records(["streamed"])
        self.append_records(assistant)
        self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[{"tool_use_id": "streamed"}]))
        later = json.loads(json.dumps(assistant))
        later["uuid"] = str(uuid4())
        later["message"]["content"] = [{"type": "text", "text": "same response"}]
        self.append_records(*results, later)
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_batch_does_not_clear_after_an_unrelated_model_response(self):
        self.usage(800)
        self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[{"tool_use_id": "old-batch"}]))
        assistant, results = self.batch_records(["old-batch"])
        self.append_records(assistant, *results)
        self.usage(850)
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.assertNotEqual(self.runtime.receipt()["phase"], "clear_sent")

    def test_stop_replaces_batch_after_queued_tool_free_reply(self):
        self.usage(800)
        self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[{"tool_use_id": "old-batch"}]))
        assistant, results = self.batch_records(["old-batch"])
        queued = {"type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
                  "message": {"role": "user", "content": "List the remaining tasks."}}
        self.append_records(assistant, *results, queued)
        self.usage(850)
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.assertNotIn("tool_batch_boundary", self.runtime._state())
        self.runtime.advance()
        self.clear_mock.assert_called_once()
        self.assertEqual(self.runtime._state()["authorization"]["latest_instruction_locator"]["message_id"],
                         queued["uuid"])

    def test_stop_after_batch_waits_for_queued_reply_to_flush(self):
        self.usage(800)
        self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[{"tool_use_id": "old-batch"}]))
        assistant, results = self.batch_records(["old-batch"])
        self.append_records(assistant, *results, {
            "type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
            "message": {"role": "user", "content": "List the remaining tasks."}})
        self.runtime.on_hook(self.hook("Stop"))
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.usage(850)
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_stop_after_batch_requires_matching_text_and_settled_tools(self):
        self.usage(800)
        self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[{"tool_use_id": "old-batch"}]))
        assistant, results = self.batch_records(["old-batch"])
        self.append_records(assistant, *results)
        self.usage(850)
        self.runtime.on_hook(self.hook("Stop", last_assistant_message="not the actual final reply"))
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="unfinished"))
        pending, results = self.batch_records(["unfinished"], value=850)
        self.append_records(pending)
        self.usage(850)
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.advance()["phase"], "waiting_safe_boundary")
        self.clear_mock.assert_not_called()
        self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="unfinished"))
        self.append_records(*results)
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.usage(850)
        self.runtime.on_hook(self.hook("Stop"))
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_batch_rejects_changed_usage_anchor_even_after_tools_settle(self):
        self.usage(800)
        self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[{"tool_use_id": "bound"}]))
        assistant, results = self.batch_records(["bound"])
        self.append_records(assistant, *results)
        self.source.write_text(self.source.read_text().replace('"input_tokens": 800', '"input_tokens": 801'))
        self.assert_waiting(self.runtime.advance(), "rotation_requested")
        self.clear_mock.assert_not_called()

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
        assistant, results = self.batch_records(["out-one", "out-two"], value=60000)
        for block in assistant["message"]["content"]:
            block["name"] = "Bash"
        self.append_records(assistant, *results)
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

    def test_wrapped_peer_deferred_input_crosses_clear_with_partial_trailing_append(self):
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        latest_human_locator = HistorySource(self.source, self.sid).latest_instruction()
        body = "peer handoff public marker"
        envelope, persisted = self.peer_message("worker-id", body)
        deferred = self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=envelope))
        self.assertFalse(deferred["continue"])
        peer = {"type": "user", "uuid": str(uuid4()), "sessionId": self.sid, "isMeta": True,
                "promptSource": "system", "origin": {"kind": "peer", "from": "worker-id",
                "body": body, "senderTaskId": "worker-id"},
                "message": {"role": "user", "content": persisted}}
        with self.source.open("ab") as handle:
            handle.write(json.dumps(peer, ensure_ascii=False).encode("utf-8") + b"\n")
            handle.write(b'{"type":"progress","sessionId":"' + self.sid.encode("ascii") + b'"')
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_bytes(b"")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear", "session_id": sid,
                              "cwd": str(self.cwd), "transcript_path": str(source)})
        self.assertEqual(self.runtime.advance()["phase"], "awaiting_continuation")
        text = self.send_mock.call_args.args[2]
        packet = json.loads(text.split("\n", 1)[1].split("\n", 1)[1])
        locator = packet["deferred_inputs"][0]
        old_source = HistorySource(self.source, self.sid)
        self.assertEqual(locator["source_kind"], "meta")
        self.assertIn(body, old_source.read(locator)["text"])
        self.assertEqual(packet["latest_instruction_locator"], latest_human_locator)
        self.assertNotIn(envelope, text)
        self.assertNotIn(body, text)
        self.clear_mock.assert_called_once()
        self.send_mock.assert_called_once()

    def test_identical_deferred_peer_envelopes_bind_distinct_native_records(self):
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        body = "identical peer handoff marker"
        envelope, persisted = self.peer_message("worker-id", body)
        for _ in range(2):
            deferred = self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=envelope))
            self.assertFalse(deferred["continue"])
        peers = [{"type": "user", "uuid": str(uuid4()), "sessionId": self.sid, "isMeta": True,
                  "promptSource": "system", "origin": {"kind": "peer", "from": "worker-id",
                  "body": body, "senderTaskId": "worker-id"},
                  "message": {"role": "user", "content": persisted}}
                 for _ in range(2)]
        self.append_records(*peers)
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_bytes(b"")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "clear", "session_id": sid,
                              "cwd": str(self.cwd), "transcript_path": str(source)})
        self.assertEqual(self.runtime.advance()["phase"], "awaiting_continuation")
        packet = json.loads(self.send_mock.call_args.args[2].split("\n", 1)[1].split("\n", 1)[1])
        locators = packet["deferred_inputs"]
        self.assertEqual(len(locators), 2)
        self.assertEqual({locator["message_id"] for locator in locators}, {peer["uuid"] for peer in peers})
        old_source = HistorySource(self.source, self.sid)
        self.assertTrue(all(body in old_source.read(locator)["text"] for locator in locators))
        self.assertEqual(packet["latest_instruction_locator"], old_source.latest_instruction())
        self.clear_mock.assert_called_once()
        self.send_mock.assert_called_once()

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
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_bytes(b"")
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

    def test_lifecycle_directory_change_settles_build_and_rotates(self):
        frontend = self.cwd / "frontend"
        frontend.mkdir()
        observed_cwds = []
        self.runtime.configuration_reader = lambda cwd: observed_cwds.append(cwd) or self.config
        request, results = self.batch_records(["cd-build"], value=820)
        self.append_records(request)
        self.runtime.on_hook(self.hook("PreToolUse", tool_name="Bash", tool_use_id="cd-build"))
        self.append_records(*results)
        output = self.runtime.on_hook(self.hook("PostToolUse", cwd=str(frontend),
            tool_name="Bash", tool_use_id="cd-build", tool_response={"stdout": "built", "stderr": ""}))
        self.assertNotIn("systemMessage", output)
        self.assertEqual(self.runtime.receipt()["cwd"], str(frontend))
        self.assertEqual(observed_cwds[-1], frontend)
        self.assertEqual(self.runtime.receipt()["pending_tool_ids"], [])
        batch = self.runtime.on_hook(self.hook("PostToolBatch", cwd=str(frontend),
            tool_calls=[{"tool_use_id": "cd-build"}]))
        self.assertFalse(batch["continue"])
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_lifecycle_changed_directory_cannot_rebind_foreign_source(self):
        frontend = self.cwd / "frontend"
        frontend.mkdir()
        foreign = frontend / self.source.name
        foreign.write_bytes(self.source.read_bytes())
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="live"))
        self.runtime.on_hook(self.hook("PostToolUse", cwd=str(frontend),
            transcript_path=str(foreign), tool_use_id="live"))
        state = self.runtime._state()
        self.assertEqual(state["source_path"], str(self.source))
        self.assertEqual(state["cwd"], str(self.cwd))
        self.assertEqual(state["pending_tool_ids"], ["live"])
        self.clear_mock.assert_not_called()

    def test_lifecycle_history_settles_missed_terminal_hook_at_next_boundary(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                tool_id = f"missed-post-{failed}"
                request, results = self.batch_records([tool_id], value=125)
                self.append_records(request)
                self.runtime.on_hook(self.hook("PreToolUse", tool_use_id=tool_id))
                results[0]["message"]["content"][0]["is_error"] = failed
                self.append_records(*results)
        request, results = self.batch_records(["next-batch"], value=820)
        self.append_records(request, *results)
        output = self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[{"tool_use_id": "next-batch"}]))
        self.assertFalse(output["continue"])
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.assertEqual(self.runtime.receipt()["pending_tool_ids"], [])
        self.clear_mock.assert_called_once()

    def test_lifecycle_settlement_needs_real_paired_terminal_evidence(self):
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="not-in-history"))
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="still-running"))
        request, results = self.batch_records(["still-running"], value=125)
        self.append_records(request)
        orphan = self.batch_records(["not-in-history"], value=125)[1][0]
        sidechain = dict(results[0], isSidechain=True)
        self.append_records(orphan, sidechain)
        request, results = self.batch_records(["finished-batch"], value=820)
        self.append_records(request, *results)
        self.runtime.on_hook(self.hook("PostToolBatch", tool_calls=[{"tool_use_id": "finished-batch"}]))
        self.assertEqual(self.runtime.advance()["phase"], "waiting_safe_boundary")
        self.assertEqual(set(self.runtime.receipt()["pending_tool_ids"]), {"not-in-history", "still-running"})
        self.clear_mock.assert_not_called()

    def test_lifecycle_clear_starts_fresh_foreground_epoch_and_keeps_background(self):
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="old-foreground"))
        self.runtime.on_hook(self.hook("SubagentStart", agent_id="live-background"))
        sid, source = self.resume_source(125)
        self.runtime.on_hook(self.hook("SessionStart", source="clear", session_id=sid,
                                      transcript_path=str(source)))
        state = self.runtime._state()
        self.assertEqual(state["pending_tool_ids"], [])
        self.assertEqual(state["active_child_handles"], ["live-background"])
        self.assertEqual(state["superseded_lifecycle"]["pending_tool_ids"], ["old-foreground"])
        self.runtime.on_hook(self.hook("PreToolUse", session_id=sid,
                                      transcript_path=str(source), tool_use_id="new-foreground"))
        frontend = self.cwd / "frontend"
        frontend.mkdir()
        self.runtime.on_hook(self.hook("PostToolUse", cwd=str(frontend), tool_use_id="old-foreground"))
        state = self.runtime._state()
        self.assertEqual(state["pending_tool_ids"], ["new-foreground"])
        self.assertEqual(state["superseded_lifecycle"]["pending_tool_ids"], [])
        self.assertEqual(state["cwd"], str(self.cwd))
        self.assertIsNone(state.get("reconciliation"))
        self.clear_mock.assert_not_called()

    def test_lifecycle_reconciles_completed_tool_carried_from_catalogued_window(self):
        old_id = "completed-before-clear"
        request, results = self.batch_records([old_id], value=125)
        self.append_records(request, *results)
        sid, source = self.resume_source(125)
        self.runtime.on_hook(self.hook("SessionStart", source="clear", session_id=sid,
                                      transcript_path=str(source)))
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["pending_tool_ids"] = [old_id]
            self.runtime._save(state)
        self.sid, self.source = sid, source
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        self.assertEqual(self.runtime.receipt()["pending_tool_ids"], [])
        self.clear_mock.assert_called_once()

    def test_lifecycle_native_failure_notice_settles_child_without_stop_hook(self):
        request, results = self.batch_records(["child-launch"], value=125)
        results[0]["toolUseResult"] = {"agentId": "failed-child", "isAsync": True}
        self.append_records(request, *results)
        self.runtime.on_hook(self.hook("SubagentStart", agent_id="failed-child"))
        text = "<task-notification><task-id>failed-child</task-id><status>failed</status></task-notification>"
        self.append_records({"type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
            "origin": {"kind": "task-notification"}, "promptSource": "system",
            "message": {"role": "user", "content": text}})
        self.usage(125)
        self.runtime.on_hook(self.hook("Stop"))
        self.runtime.advance()
        self.assertEqual(self.runtime.receipt()["active_child_handles"], [])
        self.clear_mock.assert_not_called()

    def test_lifecycle_resume_after_directory_change_uses_current_native_cwd(self):
        frontend = self.cwd / "frontend"
        frontend.mkdir()
        self.runtime.on_hook(self.hook("SessionStart", source="resume", cwd=str(frontend)))
        state = self.runtime._state()
        self.assertEqual(state["cwd"], str(frontend))
        self.assertNotIn("pending_session_start", state)
        self.assertIsNone(state.get("reconciliation"))
        self.assertEqual(state["usage"]["total_input_and_cache_tokens"], 125)
        self.clear_mock.assert_not_called()

    def test_lifecycle_delayed_foreign_resume_retains_workspace_check(self):
        foreign_cwd = self.root / "foreign-workspace"
        foreign_cwd.mkdir()
        for hook_before_reconcile in (False, True):
            with self.subTest(hook_before_reconcile=hook_before_reconcile):
                runtime, _, _ = self.resume_runtime()
                sid, source = self.resume_source(125, cwd=foreign_cwd)
                complete = source.read_bytes()
                source.write_bytes(b"")
                event = self.hook("SessionStart", source="resume", session_id=sid,
                                  transcript_path=str(source))
                runtime.on_hook(event)
                self.assertIsNotNone(runtime._state().get("pending_session_start"))
                source.write_bytes(complete)
                if hook_before_reconcile:
                    runtime.on_hook(self.hook("Stop", session_id=sid, transcript_path=str(source)))
                state = runtime._state()
                self.assertIsNone(state["authorization"])
                self.assertFalse(runtime._reconcile_pending_session_start(state))
                self.assertIsNone(state["authorization"])
                self.assertIsNotNone(state.get("pending_session_start"))
                # Repeating SessionStart must not turn its provisional catalogue
                # entry into an already-verified workspace binding.
                runtime.on_hook(event)
                self.assertIsNone(runtime._state()["authorization"])
                self.clear_mock.assert_not_called()
                self.send_mock.assert_not_called()

    def test_lifecycle_provisional_resume_catalogue_is_not_workspace_proof(self):
        runtime, _, _ = self.resume_runtime()
        foreign_cwd = self.root / "foreign-workspace"
        foreign_cwd.mkdir()
        sid, source = self.resume_source(125, cwd=foreign_cwd)
        complete = source.read_bytes()
        source.write_bytes(b"")
        event = self.hook("SessionStart", source="resume", session_id=sid, transcript_path=str(source))
        runtime.on_hook(event)
        source.write_bytes(complete)
        runtime.on_hook(self.hook("SessionStart", source="resume"))
        self.assertEqual(runtime._state()["session_id"], self.sid)
        runtime.on_hook(event)
        self.assertNotEqual(runtime._state()["authorization"]["root_instruction_locator"]["session_id"], sid)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_lifecycle_cold_resume_after_cd_accepts_verified_launch_workspace(self):
        frontend = self.cwd / "frontend"
        frontend.mkdir()
        self.runtime.on_hook(self.hook("PostToolUse", tool_use_id="completed-cd", cwd=str(frontend)))
        self.usage(125, cwd=frontend)
        self.assertTrue(tui_runtime._owner_workspace_matches(self.runtime._state(), self.cwd))
        for delayed in (False, True):
            with self.subTest(delayed=delayed):
                runtime, _, _ = self.resume_runtime()
                complete = self.source.read_bytes()
                if delayed:
                    self.source.write_bytes(b"")
                runtime.on_hook(self.hook("SessionStart", source="resume"))
                if delayed:
                    self.source.write_bytes(complete)
                    with core.lock(runtime.lock_path):
                        state = runtime._state()
                        self.assertTrue(runtime._reconcile_pending_session_start(state))
                        runtime._save(state)
                state = runtime._state()
                self.assertEqual(state["session_id"], self.sid)
                self.assertEqual(state["cwd"], str(self.cwd))
                self.assertNotIn("pending_session_start", state)
                self.assertFalse(state.get("native_session_uncertain"))
                self.assertEqual(state["authorization"]["root_instruction_locator"]["session_id"], self.sid)
                self.clear_mock.assert_not_called()
                self.send_mock.assert_not_called()

    def test_lifecycle_old_result_cannot_settle_reused_live_tool_id(self):
        tool_id = "call-reused-across-sessions"
        request, results = self.batch_records([tool_id], value=125)
        self.append_records(request, *results)
        sid, source = self.resume_source(125)
        self.runtime.on_hook(self.hook("SessionStart", source="clear", session_id=sid,
                                      transcript_path=str(source)))
        self.runtime.on_hook(self.hook("PreToolUse", session_id=sid, transcript_path=str(source),
                                      tool_use_id=tool_id, tool_name="Bash", tool_input={}))
        state = self.runtime._state()
        activity = HistorySource(source, sid).activity()
        self.assertNotIn(tool_id, activity["pending_tools"])
        self.runtime._reconcile_lifecycle(state, activity)
        self.assertEqual(state["pending_tool_ids"], [tool_id])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

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
        self.assert_waiting(self.runtime.advance(), "running")
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
        self.assertFalse(notice["continue"])
        self.assertIn("正在等待原生确认", notice["stopReason"])
        self.runtime.on_hook(self.hook("Stop"))
        self.assert_waiting(self.runtime.advance(), "clear_sent")
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

    def test_recovery_observes_active_work_but_refuses_wrong_terminal(self):
        self.runtime._wait_external("observation_failed")
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="active"))
        result = self.runtime.recover_observation(self.sid)
        self.assertEqual(result["pending_tool_ids"], ["active"])
        with self.assertRaises(ValueError):
            self.runtime.recover_observation(str(uuid4()))
        self.assertEqual(self.runtime.receipt()["phase"], "running")
        self.runtime.advance()
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_recovery_after_acknowledged_clear_timeout_restarts_one_controller_then_dispatches_once(self):
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_bytes(b"")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(self.cwd),
                              "source": "clear", "transcript_path": str(source),
                              "model": "new-native-model", "permission_mode": "bypassPermissions"})
        self.assertEqual(self.runtime.receipt()["phase"], "awaiting_tui_prompt")
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        with patch.object(native_control, "ready", return_value=False):
            self.assert_waiting(self.runtime.advance(), "awaiting_tui_prompt")
        environment = native_control.environment(self.runtime._state()["native_control"], self.runtime._control_auth)
        environment["CLAUDE_CONTINUITY_ID"] = self.runtime.conversation_id
        with patch.dict(os.environ, environment), \
                patch("claude_context_continuity.tui_runtime.subprocess.Popen") as spawn:
            spawn.return_value.pid = 123456
            recovered = tui_runtime.recover(self.runtime.conversation_id, sid)
        self.assertEqual(recovered["phase"], "awaiting_tui_prompt")
        self.assertEqual(self.runtime._state()["native_session_model"], "new-native-model")
        self.assertEqual(self.runtime._state()["native_permission_mode"], "bypassPermissions")
        spawn.assert_called_once()
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()
        self.assertEqual(self.runtime.advance()["phase"], "awaiting_continuation")
        self.send_mock.assert_called_once()
        self.runtime.advance()
        self.send_mock.assert_called_once()

    def test_recovery_does_not_inject_stale_handoff_after_new_meta_user_prompt(self):
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        meta_prompt = {"type": "user", "uuid": str(uuid4()), "sessionId": sid,
                       "message": {"role": "user", "content": [
                           {"type": "text", "text": "new-window multimodal prompt"},
                           {"type": "image", "source": {"data": "fixture-image-data"}},
                       ]}}
        source.write_text(json.dumps(meta_prompt) + "\n")
        self.assertEqual(HistorySource(source, sid).locator(meta_prompt["uuid"])["source_kind"], "meta")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(self.cwd),
                              "source": "clear", "transcript_path": str(source)})
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        self.assertEqual(self.runtime.advance()["phase"], "running")
        self.assertIsNone(self.runtime._state()["rotation"]["request"])
        self.assertNotEqual(self.runtime.receipt()["phase"], "awaiting_tui_prompt")
        self.assertEqual(self.runtime.receipt()["session_id"], sid)
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()
        self.runtime.advance()
        self.send_mock.assert_not_called()

    def test_recovery_requeues_current_window_deferred_human_with_current_latest_locator(self):
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        old_latest = HistorySource(self.source, self.sid).latest_instruction()
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_bytes(b"")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(self.cwd),
                              "source": "clear", "transcript_path": str(source)})
        current_human = "new-window input that was blocked before dispatch"
        blocked = self.runtime.on_hook({"hook_event_name": "UserPromptSubmit", "session_id": sid,
                                        "cwd": str(self.cwd), "transcript_path": str(source),
                                        "prompt": current_human})
        self.assertFalse(blocked["continue"])
        source.write_text(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": sid,
                                      "message": {"role": "user", "content": current_human}}) + "\n")
        current_locator = HistorySource(source, sid).latest_instruction()
        self.assertEqual(current_locator["source_kind"], "original_user")
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            self.assertEqual(len(state["deferred_inputs"]), 1)
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        self.assertEqual(self.runtime.advance()["phase"], "awaiting_continuation")
        self.send_mock.assert_called_once()
        packet = json.loads(self.send_mock.call_args.args[2].split("\n", 1)[1].split("\n", 1)[1])
        self.assertEqual(packet["deferred_inputs"], [current_locator])
        self.assertEqual(packet["latest_instruction_locator"], current_locator)
        self.assertNotEqual(packet["latest_instruction_locator"], old_latest)
        self.clear_mock.assert_called_once()
        self.send_mock.assert_called_once()

    def test_recovery_of_active_new_window_keeps_human_authority_and_deferred_reference(self):
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        body = "deferred peer recovery marker"
        envelope, persisted = self.peer_message("worker-id", body)
        self.assertFalse(self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=envelope))["continue"])
        peer = {"type": "user", "uuid": str(uuid4()), "sessionId": self.sid, "isMeta": True,
                "promptSource": "system", "origin": {"kind": "peer", "from": "worker-id",
                "body": body, "senderTaskId": "worker-id"},
                "message": {"role": "user", "content": persisted}}
        self.append_records(peer)
        old_peer_locator = HistorySource(self.source, self.sid).locator(peer["uuid"])
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        latest_human = "This later human instruction is now authoritative."
        source.write_text(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": sid,
                                      "message": {"role": "user", "content": latest_human}}) + "\n")
        self.usage(125, source, sid)
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(self.cwd),
                              "source": "clear", "transcript_path": str(source),
                              "model": "later-native-model", "permission_mode": "bypassPermissions"})
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            deferred_before = json.loads(json.dumps(state["deferred_inputs"]))
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        recovered = self.runtime.advance()
        state = self.runtime._state()
        self.assertEqual(recovered["phase"], "running")
        self.assertIsNone(state["rotation"]["clear"])
        self.assertIsNone(state["rotation"]["request"])
        self.assertEqual(state["deferred_inputs"], deferred_before)
        self.assertIn(body, HistorySource(self.source, self.sid).read(old_peer_locator)["text"])
        self.assertEqual(state["authorization"]["latest_instruction_locator"],
                         HistorySource(source, sid).latest_instruction())
        self.assertEqual(HistorySource(source, sid).read(state["authorization"]["latest_instruction_locator"])["text"],
                         latest_human)
        self.assertEqual(state["native_session_model"], "later-native-model")
        self.assertEqual(state["native_permission_mode"], "bypassPermissions")
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()
        self.runtime.advance()
        self.send_mock.assert_not_called()

    def test_recovery_refuses_unknown_continuation_dispatch(self):
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_bytes(b"")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(self.cwd),
                              "source": "clear", "transcript_path": str(source)})
        self.assertEqual(self.runtime.advance()["phase"], "awaiting_continuation")
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            self.assertIn("continuation_hash", state)
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        self.assert_waiting(self.runtime.advance(), "awaiting_continuation")
        self.assert_waiting(self.runtime.recover_observation(sid), "awaiting_continuation")
        self.runtime.advance()
        self.clear_mock.assert_called_once()
        self.send_mock.assert_called_once()

    def test_recovery_refuses_unresolved_deferred_record_after_acknowledged_clear(self):
        self.threshold()
        self.assertEqual(self.runtime.advance()["phase"], "clear_sent")
        envelope, _ = self.peer_message("worker-id", "unflushed deferred peer")
        self.assertFalse(self.runtime.on_hook(self.hook("UserPromptSubmit", prompt=envelope))["continue"])
        sid = str(uuid4())
        source = self.root / f"{sid}.jsonl"
        source.write_bytes(b"")
        self.runtime.on_hook({"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(self.cwd),
                              "source": "clear", "transcript_path": str(source)})
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["rotation_deadline"] = 0
            self.runtime._save(state)
        self.assert_waiting(self.runtime.advance(), "awaiting_tui_prompt")
        self.assert_waiting(self.runtime.recover_observation(sid), "awaiting_tui_prompt")
        self.runtime.advance()
        self.assertEqual(len(self.runtime._state()["deferred_inputs"]), 1)
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

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
        with core.lock(self.runtime.directory / "controller.lock"), patch.dict(os.environ, environment), \
                patch("claude_context_continuity.tui_runtime.subprocess.Popen") as spawn:
            self.assertEqual(continuity.dispatch(args)["controller_pid"], 123456)
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
        self.assert_waiting(self.runtime.receipt(), "running")

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
        self.assert_waiting(runtime.receipt(), "running")

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

    def test_no_owner_host_signal_waits_until_a_real_user_record_recovers(self):
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
        self.assert_waiting(paused, "running")
        self.assertEqual(paused["session_id"], host_sid)
        self.assertNotEqual(paused["session_id"], startup_sid)
        self.assertEqual(paused["source_path"], str(host_source))
        self.assertNotEqual(paused["source_path"], str(startup_source))
        self.assertIsNone(paused["authorization"])
        self.assertEqual(paused["pending_session_start"]["session_id"], host_sid)
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
        self.assert_waiting(rejected, "running")
        self.assertEqual(rejected["session_id"], host_sid)
        self.assertEqual(rejected["source_path"], str(host_source))
        self.assertIsNone(rejected["authorization"])
        self.assertIsNotNone(rejected["pending_session_start"])
        self.assertEqual(rejected["usage"]["total_input_and_cache_tokens"], 1200)
        self.assert_waiting(runtime.advance(), "running")
        self.assertIsNone(runtime.receipt()["authorization"])
        self.assertFalse(runtime.receipt()["rotation_pending"])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_cross_manager_resume_rejects_an_exact_lineage_with_wrong_cwd(self):
        host_sid, host_source = self._managed_host_only_window()
        other_cwd = self.root / "wrong-lineage-cwd"
        other_cwd.mkdir()
        with core.lock(self.runtime.lock_path):
            owner = self.runtime._state()
            owner["cwd"] = owner["launch_cwd"] = str(other_cwd)
            self.runtime._save(owner)
        runtime, startup_sid, startup_source = self.resume_runtime()
        self.clear_mock.reset_mock()
        self.send_mock.reset_mock()

        runtime.on_hook({"hook_event_name": "SessionStart", "session_id": host_sid,
                         "cwd": str(self.cwd), "source": "resume",
                         "transcript_path": str(host_source)})
        rejected = runtime._state()
        self.assert_waiting(rejected, "running")
        self.assertEqual(rejected["session_id"], host_sid)
        self.assertEqual(rejected["source_path"], str(host_source))
        self.assertIsNone(rejected["authorization"])
        self.assertIsNotNone(rejected["pending_session_start"])
        self.assertEqual(rejected["usage"]["total_input_and_cache_tokens"], 1200)
        self.assert_waiting(runtime.advance(), "running")
        self.assertIsNone(runtime.receipt()["authorization"])
        self.assertFalse(runtime.receipt()["rotation_pending"])
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
        self.assert_waiting(rejected, "running")
        self.assertEqual(rejected["session_id"], host_sid)
        self.assertEqual(rejected["source_path"], str(host_source))
        self.assertIsNone(rejected["authorization"])
        self.assertIsNotNone(rejected["pending_session_start"])
        self.assertEqual(rejected["usage"]["total_input_and_cache_tokens"], 1200)
        self.assert_waiting(runtime.advance(), "running")
        self.assertIsNone(runtime.receipt()["authorization"])
        self.assertFalse(runtime.receipt()["rotation_pending"])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
