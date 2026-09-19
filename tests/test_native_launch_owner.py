"""Managed launch ownership checks use isolated state and never start a native CLI."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_tui_runtime as runtime_fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from claude_context_continuity import core, native_entry, tmux_transport, tui_runtime
from claude_context_continuity.tui_runtime import SessionOwnerError, TuiRuntime


class NativeLaunchOwnerTests(unittest.TestCase):
    usage = runtime_fixture.TuiRuntimeTests.usage
    hook = runtime_fixture.TuiRuntimeTests.hook

    def setUp(self):
        runtime_fixture.TuiRuntimeTests.setUp(self)
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["owned_pid"] = self.binding["pane_pid"]
            self.runtime._save(state)
        forms = {name: ("optional", False) for name in ("--resume", "-r")}
        forms.update({name: ("none", False) for name in ("--continue", "-c", "--fork-session")})
        forms.update({name: ("required", False) for name in ("--session-id", "--model", "--system-prompt")})
        forms["--tools"] = ("required", True)
        shape = patch.object(native_entry, "_native_cli_shape", return_value=(frozenset(), forms))
        shape.start()
        self.addCleanup(shape.stop)
        config = patch.object(core, "configuration", return_value=self.config)
        config.start()
        self.addCleanup(config.stop)

    def test_exact_resume_reuses_owner_without_state_input_or_process_changes(self):
        before = self.runtime.state_path.read_bytes()
        directories = set(self.runtime.directory.parent.iterdir())
        for args in (["--resume", self.sid], ["-r", self.sid],
                     [f"--resume={self.sid}"], [f"-r={self.sid}"]):
            with self.subTest(args=args), patch.object(tmux_transport, "create") as native, \
                    patch.object(tui_runtime.subprocess, "Popen") as controller:
                result = tui_runtime.run(self.cwd, detached=True, native_args=args)
                self.assertEqual(result["status"], "reused")
                self.assertEqual(result["context_id"], self.runtime.conversation_id)
                self.assertEqual(result["health"]["status"], "controller_unavailable")
                native.assert_not_called()
                controller.assert_not_called()
        self.assertEqual(self.runtime.state_path.read_bytes(), before)
        self.assertEqual(set(self.runtime.directory.parent.iterdir()), directories)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_interactive_resume_uses_existing_attach(self):
        with patch.object(sys.stdin, "isatty", return_value=True), \
                patch.object(os, "get_terminal_size", return_value=os.terminal_size((100, 30))), \
                patch.object(tui_runtime, "attach", return_value={"returncode": 0}) as attach, \
                patch.object(tmux_transport, "create") as native, \
                patch.object(tui_runtime.subprocess, "Popen") as controller:
            result = tui_runtime.run(self.cwd, native_args=["--resume", self.sid])
        self.assertEqual(result["status"], "reused")
        attach.assert_called_once_with(self.runtime.conversation_id)
        native.assert_not_called()
        controller.assert_not_called()

    def test_live_resume_does_not_ignore_options_or_deliver_a_prompt(self):
        cases = [(["--resume", self.sid, "--model", "other-model"], None),
                 (["--resume", self.sid, "new task"], None),
                 (["--resume", self.sid], "new task"),
                 (["--session-id", self.sid], None)]
        before = self.runtime.state_path.read_bytes()
        for args, prompt in cases:
            with self.subTest(args=args, prompt=prompt), patch.object(tui_runtime, "_create") as native, \
                    patch.object(tui_runtime.subprocess, "Popen") as controller:
                with self.assertRaises(SessionOwnerError):
                    tui_runtime.run(self.cwd, prompt=prompt, detached=True, native_args=args)
                native.assert_not_called()
                controller.assert_not_called()
        self.assertEqual(self.runtime.state_path.read_bytes(), before)
        self.send_mock.assert_not_called()

    def test_low_level_create_also_refuses_a_live_duplicate(self):
        with patch.object(tmux_transport, "create") as native:
            with self.assertRaises(SessionOwnerError):
                tui_runtime.create(self.cwd, native_args=["--resume", self.sid])
        native.assert_not_called()

    def test_ambiguous_resume_does_not_guess_a_live_session(self):
        for args in (["--resume"], ["--resume", "named-session"], ["--continue"], ["-c"]):
            with self.subTest(args=args), patch.object(tui_runtime, "_create") as native:
                with self.assertRaisesRegex(SessionOwnerError, "完整 session ID"):
                    tui_runtime.run(self.cwd, detached=True, native_args=args)
                native.assert_not_called()

    def test_multiple_live_owners_are_not_selected_or_stopped(self):
        other = TuiRuntime.create(cwd=self.cwd, session_id=self.sid, configuration=self.config,
                                  configuration_reader=lambda _: self.config)
        with core.lock(other.lock_path):
            state = other._state()
            state.update(tmux=self.binding, transport="tmux_tui", owned_pid=os.getpid(),
                         native_control=self.runtime._state()["native_control"])
            other._save(state)
        snapshots = [runtime.state_path.read_bytes() for runtime in (self.runtime, other)]
        with patch.object(tui_runtime, "_create") as native:
            with self.assertRaisesRegex(SessionOwnerError, "多个存活"):
                tui_runtime.run(self.cwd, detached=True, native_args=["--resume", self.sid])
        native.assert_not_called()
        self.assertEqual([runtime.state_path.read_bytes() for runtime in (self.runtime, other)], snapshots)

    def test_failed_tmux_identity_with_live_or_unknown_pid_fails_closed(self):
        for alive in (True, None):
            with self.subTest(alive=alive), patch.object(tmux_transport, "inspect", side_effect=OSError), \
                    patch.object(tui_runtime, "_pid_alive", return_value=alive), \
                    patch.object(tui_runtime, "_create") as native:
                with self.assertRaisesRegex(SessionOwnerError, "存活状态"):
                    tui_runtime.run(self.cwd, detached=True, native_args=["--resume", self.sid])
                native.assert_not_called()

    def test_missing_pid_is_not_evidence_of_a_dead_owner(self):
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["owned_pid"] = None
            self.runtime._save(state)
        with patch.object(tmux_transport, "inspect", side_effect=OSError), \
                patch.object(tui_runtime, "_create") as native:
            with self.assertRaises(SessionOwnerError):
                tui_runtime.run(self.cwd, detached=True, native_args=["--resume", self.sid])
        native.assert_not_called()

    def test_dead_native_owner_does_not_block_a_fresh_resume(self):
        before = self.runtime.state_path.read_bytes()
        args = ["--resume", self.sid]
        with patch.object(tmux_transport, "inspect", side_effect=OSError), \
                patch.object(tui_runtime, "_pid_alive", return_value=False), \
                patch.object(tui_runtime, "_create", return_value=object()) as native:
            created = tui_runtime.create(self.cwd, native_args=args)
        self.assertIs(created, native.return_value)
        self.assertEqual(native.call_args.kwargs["native_args"], args)
        self.assertEqual(native.call_args.kwargs["launch_session_ids"], [self.sid])
        self.assertEqual(self.runtime.state_path.read_bytes(), before)

    def test_explicit_resume_cannot_attach_across_working_directories(self):
        other_cwd = self.root / "other-workspace"
        other_cwd.mkdir()
        with patch.object(tui_runtime, "_create") as native:
            with self.assertRaisesRegex(SessionOwnerError, "其他工作目录"):
                tui_runtime.run(other_cwd, detached=True, native_args=["--resume", self.sid])
        native.assert_not_called()

    def test_fork_and_literal_arguments_do_not_reuse_the_source_owner(self):
        for args in (["--resume", self.sid, "--fork-session"],
                     ["--system-prompt", "--resume", self.sid],
                     ["--", "--resume", self.sid],
                     ["--tools", "Read", "--resume", str(uuid4())]):
            with self.subTest(args=args), patch.object(tui_runtime, "_create", return_value=object()) as native:
                created = tui_runtime.create(self.cwd, native_args=args)
                self.assertIs(created, native.return_value)
                self.assertEqual(native.call_args.kwargs["native_args"], args)

    def test_pending_resume_is_reserved_before_the_native_session_binds(self):
        sid = str(uuid4())
        args = ["--resume", sid]
        with patch.object(tmux_transport, "create", return_value=self.binding) as native:
            fresh = tui_runtime.create(self.cwd, native_args=args)
            self.assertEqual(fresh._state()["launch_session_ids"], [sid])
            self.assertFalse(fresh._state()["launch_pending"])
            with self.assertRaises(SessionOwnerError):
                tui_runtime.run(self.cwd, detached=True, native_args=args)
        self.assertEqual(native.call_count, 1)
        fresh.configuration_reader = lambda _: self.config
        fresh.on_hook({"hook_event_name": "SessionStart", "source": "startup",
                       "session_id": sid, "cwd": str(self.cwd)})
        self.assertNotIn("launch_session_ids", fresh._state())
        self.assertEqual(fresh._state()["session_id"], sid)

    def test_unknown_native_creation_result_is_not_replayed(self):
        args = ["--resume", str(uuid4())]
        with patch.object(tmux_transport, "create", side_effect=OSError("isolated spawn failure")) as native:
            with self.assertRaises(OSError):
                tui_runtime.create(self.cwd, native_args=args)
            with self.assertRaisesRegex(SessionOwnerError, "启动结果不明"):
                tui_runtime.create(self.cwd, native_args=args)
        self.assertEqual(native.call_count, 1)

    def test_unresolved_implicit_resume_blocks_followup_exact_resume(self):
        cwd = self.root / "picker-workspace"
        cwd.mkdir()
        sid = str(uuid4())
        with patch.object(tmux_transport, "create", return_value=self.binding) as native:
            fresh = tui_runtime.create(cwd, native_args=["--continue"])
            fresh.configuration_reader = lambda _: self.config
            fresh.on_hook({"hook_event_name": "SessionStart", "source": "startup",
                           "session_id": str(uuid4()), "cwd": str(cwd)})
            self.assertTrue(fresh._state()["native_session_uncertain"])
            with self.assertRaises(SessionOwnerError):
                tui_runtime.create(cwd, native_args=["--resume", sid])
        self.assertEqual(native.call_count, 1)
        sid, source = runtime_fixture.TuiRuntimeTests.resume_source(self, 125, cwd=cwd)
        fresh.on_hook({"hook_event_name": "SessionStart", "source": "resume",
                       "session_id": sid, "cwd": str(cwd), "transcript_path": str(source)})
        self.assertNotIn("native_session_uncertain", fresh._state())
        self.assertEqual(tui_runtime.run(cwd, detached=True, native_args=["--resume", sid])["status"], "reused")

    def test_failed_session_start_does_not_confirm_old_or_new_ownership(self):
        sid, source = runtime_fixture.TuiRuntimeTests.resume_source(self, 125)
        with core.lock(self.runtime.lock_path):
            state = self.runtime._state()
            state["pending_tool_ids"] = ["unsettled-call"]
            self.runtime._save(state)
        result = self.runtime.on_hook({"hook_event_name": "SessionStart", "source": "resume",
                                       "session_id": sid, "cwd": str(self.cwd), "transcript_path": str(source)})
        self.assertEqual(self.runtime._state()["session_id"], self.sid)
        self.assertNotIn("continue", result)
        self.assertNotIn("decision", result)
        for target in (self.sid, sid):
            with self.subTest(target=target), patch.object(tui_runtime, "_create", side_effect=AssertionError("new native launch")) as native:
                with self.assertRaises(SessionOwnerError):
                    tui_runtime.run(self.cwd, detached=True, native_args=["--resume", target])
                native.assert_not_called()

    def test_launch_lock_prevents_a_second_discovery_and_spawn(self):
        with core.lock(self.home / "runtime/native-launch.lock"), \
                patch.object(tui_runtime, "_live_launch_owner") as discover, \
                patch.object(tui_runtime, "_create") as native, \
                patch.object(core.time, "monotonic", side_effect=[0, 10]):
            with self.assertRaises(core.LockBusy):
                tui_runtime.create(self.cwd, native_args=["--resume", str(uuid4())])
        discover.assert_not_called()
        native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
