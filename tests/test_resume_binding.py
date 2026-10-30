"""Exact native resume binding regressions.

Isolated JSONL fixtures and mocked native control only; no real Claude session,
terminal, network, or model is used.
"""
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

from claude_context_continuity import core, native_control, tmux_transport  # noqa: E402
from claude_context_continuity.context_runtime import ContextRuntimeError, _runtime_message  # noqa: E402
from claude_context_continuity.history import HistorySource  # noqa: E402
from claude_context_continuity.tui_runtime import TuiRuntime  # noqa: E402


class ResumeBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="resume-binding-", dir=Path(__file__).parent)
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.cwd = self.root / "workspace"
        self.evidence = self.cwd / "research" / "evidence"
        self.evidence.mkdir(parents=True)
        self.foreign = self.root / "foreign-workspace"
        self.foreign.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = {"hash": "resume-binding", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        self.binding = {"pane_pid": os.getpid(), "cursor_x": 0, "cursor_y": 0}
        for item in (patch.object(core, "HOME", self.home),
                     patch.object(native_control, "ready", return_value=True),
                     patch.object(tmux_transport, "inspect", return_value=self.binding)):
            item.start()
            self.addCleanup(item.stop)
        clear = patch.object(native_control, "send_clear")
        send = patch.object(native_control, "send_continuation")
        self.clear_mock, self.send_mock = clear.start(), send.start()
        self.addCleanup(clear.stop)
        self.addCleanup(send.stop)

    @staticmethod
    def _user(sid, text):
        return {"type": "user", "uuid": str(uuid4()), "sessionId": sid,
                "message": {"role": "user", "content": text}}

    @staticmethod
    def _assistant(sid, tokens, cwd, *, text="fixture native response", tool_id=None):
        content = [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {}}] if tool_id else text
        return {"type": "assistant", "uuid": str(uuid4()), "sessionId": sid, "cwd": str(cwd),
                "message": {"id": f"msg_{uuid4().hex}", "role": "assistant", "content": content,
                            "model": "fixture-native-model",
                            "usage": {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 0}}}

    @staticmethod
    def _tool_result(sid, tool_id):
        return {"type": "user", "uuid": str(uuid4()), "sessionId": sid,
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": "fixture result"}]}}

    @staticmethod
    def _append(path, *records):
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def _new_source(self):
        sid = str(uuid4())
        return sid, self.root / f"{sid}.jsonl"

    def _event(self, name, sid, path, **fields):
        return {"hook_event_name": name, "session_id": sid, "cwd": str(self.cwd),
                "transcript_path": str(path), **fields}

    @staticmethod
    def _bounds(sid, path):
        return HistorySource(path, sid).instruction_bounds()

    def _snapshot(self, root_sid, root_path, latest_sid, latest_path):
        """Authorization exactly as an owner observed it at that time."""
        return {"root_instruction_locator": self._bounds(root_sid, root_path)["first"],
                "latest_instruction_locator": self._bounds(latest_sid, latest_path)["last"]}

    @staticmethod
    def _catalogue_pairs(runtime):
        return {(entry["session_id"], entry["source_path"])
                for path in (runtime.directory / "history").glob("*.json")
                for entry in [core.read_json(path)]}

    def _manager(self):
        """A new managed native process that started its own session before /resume."""
        sid, path = self._new_source()
        self._append(path, self._user(sid, "Start an unrelated local session."), self._assistant(sid, 100, self.cwd))
        runtime = TuiRuntime.create(cwd=self.cwd, session_id=sid, configuration=self.config,
                                    configuration_reader=lambda _: self.config)
        runtime.on_hook(self._event("SessionStart", sid, path, source="startup"))
        runtime._control_auth = "isolated-resume-binding-value-123456"
        with core.lock(runtime.lock_path):
            state = runtime._state()
            state.update(transport="tmux_tui", tmux=self.binding, native_control={
                "protocol": native_control.PROTOCOL, "context_id": runtime.conversation_id,
                "socket_path": str(self.home / "runtime" / "control" / f"{runtime.conversation_id}.sock")})
            runtime._save(state)
        return runtime, sid, path

    def _owner(self, sources, authorization, *, launch_cwd=None, cwd=None):
        """A closed historical manager whose verified catalogue registered ``sources`` in order."""
        launch = launch_cwd or self.cwd
        owner = TuiRuntime.create(cwd=launch, session_id=sources[0][0], configuration=self.config,
                                  configuration_reader=lambda _: self.config)
        with core.lock(owner.lock_path):
            state = owner._state()
            for generation, (sid, path) in enumerate(sources):
                state.update(session_id=sid, source_path=str(path), window_generation=generation)
                owner._catalogue_source(state)
            state.update(phase="closed", cwd=str(cwd or launch), authorization=authorization,
                         rotation={"generation": len(sources) - 1, "request": None, "clear": None})
            owner._save(state)
        return owner

    def _resumable(self):
        """Genuine instructions first observed in the launch workspace."""
        sid, path = self._new_source()
        self._append(path, self._user(sid, "Run the authorized research evidence task."),
                     self._assistant(sid, 200, self.cwd))
        return sid, path

    def _complete_rotation(self, runtime):
        new_sid, new_path = self._new_source()
        new_path.write_text("")
        runtime.on_hook(self._event("SessionStart", new_sid, new_path, source="clear"))
        runtime.advance()
        self.send_mock.assert_called_once()
        return self.send_mock.call_args.args[2]

    def _assert_genuine_resume_child_history(self, *, recover=False, rotate=False):
        root_sid, root_path = self._resumable()
        sid, path = self._new_source()
        self._append(path, self._user(sid, "Continue the same authorized work."),
                     self._assistant(sid, 900 if rotate else 400, self.cwd))
        sources = [(root_sid, root_path), (sid, path)]
        if rotate:
            sources.append(self._resumable())
        owner = self._owner(sources, self._snapshot(*sources[0], *sources[-1]))
        agent = "a1234567890abcdef"
        metadata = {"agentType": "ui-design-worker", "toolUseId": "original-launch", "finished": True}
        original = self.root / root_sid / "subagents"
        current = self.root / sid / "subagents"
        for directory in (original, current):
            directory.mkdir(parents=True)
            (directory / f"agent-{agent}.meta.json").write_text(json.dumps(metadata))
        original_history = original / f"agent-{agent}.jsonl"
        original_history.write_text(json.dumps({
            "type": "assistant", "agentId": agent, "sessionId": root_sid,
            "isSidechain": True, "uuid": str(uuid4()),
            "message": {"role": "assistant", "content": "Completed original leaf."}
        }) + "\n")
        owner_before = owner.state_path.read_bytes()
        authorization = self._snapshot(sid, path, sid, path)
        runtime, _, _ = self._manager()

        if recover:
            with patch.object(runtime, "_inherit_registered_catalogue"):
                runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
            self.assertEqual(runtime._state()["agent_metadata_compat"]["status"], "blocked")
            if rotate:
                self.assertEqual(runtime.advance()["phase"], "clear_sent")
                continuation = self._complete_rotation(runtime)
                rotated = runtime._state()
                sid, path = rotated["session_id"], Path(rotated["source_path"])
                self._append(path, self._user(sid, continuation), self._assistant(sid, 400, self.cwd))
                self.assertEqual(runtime.advance()["phase"], "running")
                self.clear_mock.assert_called_once()
                self.clear_mock.reset_mock()
                self.send_mock.reset_mock()
                current = self.root / sid / "subagents"
            runtime.recover_observation(sid)
        else:
            runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))

        state = runtime._state()
        self.assertEqual(state["session_id"], sid)
        self.assertIn((root_sid, str(root_path)), self._catalogue_pairs(runtime))
        if rotate:
            self.assertNotIn((sources[-1][0], str(sources[-1][1])), self._catalogue_pairs(runtime))
        self.assertEqual(state["authorization"], authorization)
        self.assertEqual(state["agent_metadata_compat"]["status"], "verified")
        self.assertTrue(os.path.samefile(original_history, current / original_history.name))
        if recover:
            catalogue = runtime._catalogue_entries(runtime)
            history_before = original_history.read_bytes()
            runtime.recover_observation(sid)
            repeated = runtime._state()
            self.assertEqual(runtime._catalogue_entries(runtime), catalogue)
            for key in ("session_id", "window_generation", "rotation", "authorization", "phase"):
                self.assertEqual(repeated[key], state[key])
            self.assertTrue(os.path.samefile(original_history, current / original_history.name))
            self.assertEqual(original_history.read_bytes(), history_before)
        self.assertEqual(owner.state_path.read_bytes(), owner_before)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_resume_with_genuine_input_retains_catalogued_child_history(self):
        self._assert_genuine_resume_child_history()

    def test_recover_restores_previously_dropped_child_catalogue_without_resuming_work(self):
        self._assert_genuine_resume_child_history(recover=True)

    def test_recover_restores_dropped_catalogue_after_another_rotation(self):
        self._assert_genuine_resume_child_history(recover=True, rotate=True)

    def test_catalogue_inheritance_excludes_future_windows_and_foreign_workspaces(self):
        root_sid, root_path = self._resumable()
        sid, path = self._resumable()
        future_sid, future_path = self._resumable()
        self._owner([(root_sid, root_path), (sid, path), (future_sid, future_path)],
                    self._snapshot(root_sid, root_path, future_sid, future_path))
        foreign_sid, foreign_path = self._resumable()
        self._owner([(foreign_sid, foreign_path), (sid, path)],
                    self._snapshot(foreign_sid, foreign_path, sid, path), launch_cwd=self.foreign)
        runtime, _, _ = self._manager()

        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))

        self.assertEqual(runtime._state()["session_id"], sid)
        pairs = self._catalogue_pairs(runtime)
        self.assertIn((root_sid, str(root_path)), pairs)
        self.assertNotIn((future_sid, str(future_path)), pairs)
        self.assertNotIn((foreign_sid, str(foreign_path)), pairs)
        self.assertEqual(runtime._state()["authorization"]["root_instruction_locator"],
                         self._bounds(sid, path)["first"])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_repeated_native_source_keeps_intervening_history_but_not_later_windows(self):
        root = self._resumable()
        target = self._resumable()
        intervening = self._resumable()
        later = self._resumable()
        self._owner([root, target, intervening, target, later],
                    self._snapshot(*root, *later))
        runtime, _, _ = self._manager()

        runtime.on_hook(self._event("SessionStart", *target, source="resume"))

        pairs = self._catalogue_pairs(runtime)
        self.assertIn((root[0], str(root[1])), pairs)
        self.assertIn((intervening[0], str(intervening[1])), pairs)
        self.assertNotIn((later[0], str(later[1])), pairs)
        self.assertEqual(runtime._state()["authorization"]["root_instruction_locator"],
                         self._bounds(*target)["first"])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_api_request_metadata_does_not_leave_resume_history_prefix_pending(self):
        sid, path = self._resumable()
        self._append(path,
            {"type": "api-request-shape", "sessionId": sid, "shape": {}, "shapeHash": "a" * 64,
             "timestamp": "2026-10-20T00:00:00Z", "version": "2.1.304"},
            {"type": "api-request-blob", "sessionId": sid, "hash": "b" * 64,
             "message": {"role": "user", "content": "Do not promote diagnostic request payloads."}},
            {"type": "api-request", "sessionId": sid, "id": "fixture-request", "params": {}, "keep": 1,
             "tail": [], "cacheControl": [], "requestDigest": "c" * 64, "querySource": "fixture",
             "timestamp": "2026-10-20T00:00:00Z", "version": "2.1.304", "shapeHash": "a" * 64})
        runtime, _, _ = self._manager()
        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        state = runtime._state()
        self.assertNotIn("pending_session_start", state, state.get("diagnostic"))
        self.assertEqual(state["session_id"], sid)
        self.assertFalse(state.get("native_session_uncertain"))
        self.assertEqual(state["native_resume_confirmations"], 1)
        bounds = self._bounds(sid, path)
        self.assertEqual(state["authorization"], {"root_instruction_locator": bounds["first"],
                                                 "latest_instruction_locator": bounds["last"]})
        self.assertEqual(state["usage"]["total_input_and_cache_tokens"], 200)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_stale_owner_snapshot_does_not_block_a_registered_workspace_resume(self):
        sid, path = self._resumable()
        owner = self._owner([(sid, path)], self._snapshot(sid, path, sid, path), cwd=self.evidence)
        # Later genuine work after a cd: the owner's latest locator is still exact,
        # but no longer the source's current latest instruction.
        self._append(path, self._user(sid, "A later genuine correction for the same task."),
                     self._assistant(sid, 900, self.evidence))
        owner_state = owner.state_path.read_bytes()
        runtime, startup_sid, _ = self._manager()

        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        state = runtime._state()
        self.assertEqual(state["session_id"], sid, state.get("diagnostic"))
        self.assertEqual(state["source_path"], str(path))
        self.assertNotIn("pending_session_start", state)
        self.assertFalse(state.get("native_session_uncertain"))
        bounds = self._bounds(sid, path)
        self.assertEqual(state["authorization"], {"root_instruction_locator": bounds["first"],
                                                  "latest_instruction_locator": bounds["last"]})
        self.assertEqual(state["usage"]["total_input_and_cache_tokens"], 900)
        self.assertEqual(owner.state_path.read_bytes(), owner_state)

        outcome = runtime.advance()
        self.assertEqual(outcome["phase"], "clear_sent")
        self.assertEqual(outcome["automatic_rotations"], 1)
        self.clear_mock.assert_called_once()
        continuation = self._complete_rotation(runtime)
        self.assertIn(bounds["first"]["message_id"], continuation)
        self.assertIn(bounds["last"]["message_id"], continuation)
        self.assertNotEqual(startup_sid, sid)

    def test_multiple_stale_owners_with_different_roots_do_not_block_resume(self):
        root_sid, root_path = self._new_source()
        self._append(root_path, self._user(root_sid, "Original root task in an earlier window."),
                     self._assistant(root_sid, 200, self.cwd))
        sid, path = self._resumable()
        first = self._owner([(root_sid, root_path), (sid, path)],
                            self._snapshot(root_sid, root_path, sid, path))
        self._append(path, self._user(sid, "Second genuine instruction."),
                     self._assistant(sid, 300, self.evidence))
        second = self._owner([(sid, path)], self._snapshot(sid, path, sid, path), cwd=self.evidence)
        self._append(path, self._user(sid, "Third genuine instruction after both owners closed."),
                     self._assistant(sid, 900, self.evidence))
        owners = {owner.conversation_id: owner.state_path.read_bytes() for owner in (first, second)}
        roots = [owner._state()["authorization"]["root_instruction_locator"] for owner in (first, second)]
        self.assertNotEqual(roots[0], roots[1])
        runtime, _, _ = self._manager()

        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        state = runtime._state()
        self.assertEqual(state["session_id"], sid, state.get("diagnostic"))
        self.assertNotIn("pending_session_start", state)
        bounds = self._bounds(sid, path)
        self.assertEqual(state["authorization"], {"root_instruction_locator": bounds["first"],
                                                  "latest_instruction_locator": bounds["last"]})
        for owner in (first, second):
            self.assertEqual(owner.state_path.read_bytes(), owners[owner.conversation_id])
        self.assertEqual(runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()

    def test_rejected_valid_resume_is_reobserved_from_its_own_genuine_hooks(self):
        sid, path = self._resumable()
        # The latest pre-resume model call ran in a subdirectory and no manager
        # registered this source, so its workspace is initially unproved.
        self._append(path, self._assistant(sid, 300, self.evidence))
        runtime, startup_sid, _ = self._manager()

        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        rejected = runtime._state()
        self.assertEqual(rejected["session_id"], startup_sid)
        pending = rejected.get("pending_session_start")
        self.assertIsInstance(pending, dict, rejected.get("diagnostic"))
        self.assertIs(pending["bound"], False)
        self.assertEqual((pending["session_id"], pending["transcript_path"], pending["cwd"]),
                         (sid, str(path), str(self.cwd)))
        first_failure = pending["first_failure"]
        self.assertTrue(first_failure)
        self.assertEqual(rejected["diagnostic"], first_failure)
        self.assertTrue(rejected["native_session_uncertain"])
        self.assertEqual(runtime.advance()["session_id"], startup_sid)

        prompt = "Continue the resumed task from the workspace root."
        output = runtime.on_hook(self._event("UserPromptSubmit", sid, path, prompt=prompt))
        self.assertIsNot(output.get("continue"), False)
        runtime.on_hook(self._event("PreToolUse", sid, path, tool_use_id="live-read",
                                    tool_name="Read", tool_input={}))
        held = runtime._state()
        self.assertEqual(held["session_id"], startup_sid)
        self.assertTrue(held["pending_session_start"]["activity_observed"])
        self.assertEqual(held["pending_session_start"]["first_failure"], first_failure)
        self.assertEqual(held["diagnostic"], first_failure)

        # The resumed turn's own model call now proves the SessionStart workspace.
        self._append(path, self._user(sid, prompt), self._assistant(sid, 400, self.cwd, tool_id="live-read"))
        runtime.on_hook(self._event("PostToolUse", sid, path, tool_use_id="live-read",
                                    tool_name="Read", tool_input={}))
        bound = runtime._state()
        self.assertEqual(bound["session_id"], sid, bound.get("diagnostic"))
        self.assertNotIn("pending_session_start", bound)
        self.assertFalse(bound.get("native_session_uncertain"))
        self.assertNotIn("resume_safe_boundary", bound)
        self.assertEqual(bound["authorization"]["latest_instruction_locator"], self._bounds(sid, path)["last"])
        self.assertEqual(runtime.advance()["phase"], "running")
        self.clear_mock.assert_not_called()

        final = "The resumed turn finished."
        self._append(path, self._tool_result(sid, "live-read"), self._assistant(sid, 900, self.cwd, text=final))
        runtime.on_hook(self._event("Stop", sid, path, last_assistant_message=final))
        self.assertEqual(runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_recover_reobserves_the_exact_pending_event_when_registry_proof_returns(self):
        sid, path = self._resumable()
        self._append(path, self._assistant(sid, 900, self.evidence))
        owner = self._owner([(sid, path)], self._snapshot(sid, path, sid, path))
        saved = owner.state_path.read_bytes()
        owner.state_path.write_text("{")  # temporarily unreadable: neither proof nor conflict
        runtime, startup_sid, _ = self._manager()
        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        self.assertIs(runtime._state()["pending_session_start"]["bound"], False)
        owner.state_path.write_bytes(saved)

        # Unchanged native history gives the observer no new fact to re-read.
        self.assertEqual(runtime.advance()["session_id"], startup_sid)
        receipt = runtime.recover_observation(sid)
        self.assertEqual(receipt["session_id"], sid, receipt.get("diagnostic"))
        state = runtime._state()
        self.assertNotIn("pending_session_start", state)
        self.assertTrue(state["resume_safe_boundary"])
        self.assertEqual(state["authorization"]["root_instruction_locator"], self._bounds(sid, path)["first"])
        self.assertEqual(runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        self.send_mock.assert_not_called()

    def test_observer_reobservation_after_unhooked_history_change_waits_for_a_genuine_boundary(self):
        sid, path = self._resumable()
        self._append(path, self._assistant(sid, 300, self.evidence))
        runtime, startup_sid, _ = self._manager()
        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        self.assertIs(runtime._state()["pending_session_start"]["bound"], False)

        # Native history changed with no observed hook; the recorded idle
        # resume boundary can no longer be assumed.
        self._append(path, self._assistant(sid, 900, self.cwd))
        self.assertEqual(runtime.advance()["session_id"], sid)
        state = runtime._state()
        self.assertNotIn("pending_session_start", state)
        self.assertNotIn("resume_safe_boundary", state)
        self.assertEqual(runtime.advance()["phase"], "running")
        self.clear_mock.assert_not_called()
        runtime.on_hook(self._event("Stop", sid, path, last_assistant_message="fixture native response"))
        self.assertEqual(runtime.advance()["phase"], "clear_sent")
        self.clear_mock.assert_called_once()
        self.assertNotEqual(startup_sid, sid)

    def test_another_genuine_resume_supersedes_an_unbound_event_without_replay(self):
        unproved_sid, unproved_path = self._resumable()
        self._append(unproved_path, self._assistant(unproved_sid, 300, self.evidence))
        target_sid, target_path = self._resumable()
        runtime, startup_sid, startup_path = self._manager()
        runtime.on_hook(self._event("SessionStart", unproved_sid, unproved_path, source="resume"))
        self.assertIs(runtime._state()["pending_session_start"]["bound"], False)

        runtime.on_hook(self._event("SessionStart", target_sid, target_path, source="resume"))
        state = runtime._state()
        self.assertEqual(state["session_id"], target_sid, state.get("diagnostic"))
        self.assertNotIn("pending_session_start", state)
        self.assertFalse(state.get("native_session_uncertain"))
        self.assertNotIn("launch_session_ids", state)
        self.assertEqual(state["native_resume_confirmations"], 1)
        self.assertEqual((state["superseded_lifecycle"]["session_id"], state["superseded_lifecycle"]["source_path"]),
                         (startup_sid, str(startup_path)))
        self.assertEqual(state["authorization"]["root_instruction_locator"]["session_id"], target_sid)
        self.assertEqual(runtime.advance()["phase"], "running")
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_manual_clear_supersedes_bound_and_unbound_resume_events_without_replay(self):
        for bound in (False, True):
            with self.subTest(bound=bound):
                self.clear_mock.reset_mock()
                self.send_mock.reset_mock()
                sid, path = self._new_source()
                if bound:
                    path.write_text("")  # adopted epoch whose native history has not flushed yet
                else:
                    self._append(path, self._user(sid, "An unproved resume target."),
                                 self._assistant(sid, 300, self.evidence))
                runtime, _, _ = self._manager()
                runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
                pending = runtime._state().get("pending_session_start")
                self.assertIsInstance(pending, dict)
                self.assertIs(pending.get("bound", True), bound)
                runtime.on_hook(self._event("PreToolUse", sid, path, tool_use_id="before-clear",
                                            tool_name="Read", tool_input={}))

                new_sid, new_path = self._new_source()
                new_path.write_text("")
                runtime.on_hook(self._event("SessionStart", new_sid, new_path, source="clear"))
                cleared = runtime._state()
                self.assertEqual(cleared["session_id"], new_sid)
                self.assertNotIn("pending_session_start", cleared)
                self.assertFalse(cleared.get("native_session_uncertain"))
                self.assertNotIn("launch_session_ids", cleared)
                self.assertEqual(cleared["manual_clear_count"], 1)
                receipt = runtime.advance()
                self.assertIsNone(receipt["reconciliation"], receipt.get("diagnostic"))
                self.assertIn((new_sid, str(new_path)), self._catalogue_pairs(runtime))
                self.clear_mock.assert_not_called()
                self.send_mock.assert_not_called()

    def test_unknown_conflicting_or_provisional_workspace_evidence_fails_closed(self):
        for case in ("unknown", "conflicting", "provisional"):
            with self.subTest(case=case):
                sid, path = self._new_source()
                if case == "provisional":
                    path.write_text("")
                    provisional, _, _ = self._manager()
                    provisional.on_hook(self._event("SessionStart", sid, path, source="resume"))
                    self.assertEqual(provisional._state()["session_id"], sid)
                self._append(path, self._user(sid, "Task observed outside this proved workspace."),
                             self._assistant(sid, 900, self.foreign if case == "unknown" else self.evidence))
                if case == "conflicting":
                    snapshot = self._snapshot(sid, path, sid, path)
                    self._owner([(sid, path)], snapshot)
                    self._owner([(sid, path)], snapshot, launch_cwd=self.foreign)
                runtime, startup_sid, _ = self._manager()
                before = runtime._state()["authorization"]

                runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
                runtime.on_hook(self._event("UserPromptSubmit", sid, path, prompt="Keep working."))
                runtime.on_hook(self._event("PreToolUse", sid, path, tool_use_id="held", tool_name="Read",
                                            tool_input={}))
                receipt = runtime.recover_observation(sid)
                runtime.advance()
                state = runtime._state()
                self.assertEqual(receipt["session_id"], startup_sid)
                self.assertEqual(state["session_id"], startup_sid)
                self.assertEqual(state["authorization"], before)
                self.assertIs(state["pending_session_start"]["bound"], False)
                self.assertEqual(state["pending_session_start"]["session_id"], sid)
                self.assertTrue(state["native_session_uncertain"])
                self.assertFalse(receipt["rotation_pending"])

                # Only the exact recorded identity can be reobserved.
                other_sid, other_path = self._resumable()
                runtime.on_hook(self._event("PreToolUse", other_sid, other_path, tool_use_id="other",
                                            tool_name="Read", tool_input={}))
                self.assertEqual(runtime._state()["session_id"], startup_sid)
                self.assertEqual(runtime._state()["pending_session_start"]["session_id"], sid)
                with self.assertRaises(ContextRuntimeError):
                    runtime.recover_observation(other_sid)
                self.clear_mock.assert_not_called()
                self.send_mock.assert_not_called()

    def test_pending_resume_uses_native_agent_id_for_child_activity(self):
        sid, path = self._resumable()
        self._append(path, self._assistant(sid, 300, self.evidence))
        runtime, startup_sid, _ = self._manager()
        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        runtime.on_hook(self._event("PreToolUse", sid, path, agent_id="native-child", tool_use_id="child-read",
                                    tool_name="Read", tool_input={}))
        self.assertFalse(runtime._state()["pending_session_start"].get("activity_observed"))
        runtime.on_hook(self._event("PreToolUse", sid, path, agentId="ignored-extra", tool_use_id="root-read",
                                    tool_name="Read", tool_input={}))
        state = runtime._state()
        self.assertEqual(state["session_id"], startup_sid)
        self.assertIs(state["pending_session_start"]["bound"], False)
        self.assertTrue(state["pending_session_start"]["activity_observed"])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_unsupported_siblings_do_not_poison_current_registered_workspace_proof(self):
        sid, path = self._resumable()
        owner = self._owner([(sid, path)], self._snapshot(sid, path, sid, path))
        self._append(path, self._assistant(sid, 400, self.evidence))
        snapshots = {owner.state_path: owner.state_path.read_bytes()}
        for phase in ("paused", "future_phase", None):
            unrelated_sid, unrelated_path = self._resumable()
            stale = self._owner([(unrelated_sid, unrelated_path)],
                                self._snapshot(unrelated_sid, unrelated_path, unrelated_sid, unrelated_path),
                                launch_cwd=self.foreign)
            core.atomic(stale.state_path, {**stale._state(), "phase": phase})
            snapshots[stale.state_path] = stale.state_path.read_bytes()
        runtime, _, _ = self._manager()
        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        state = runtime._state()
        self.assertEqual(state["session_id"], sid, state.get("diagnostic"))
        self.assertNotIn("pending_session_start", state)
        self.assertEqual(state["authorization"]["root_instruction_locator"], self._bounds(sid, path)["first"])
        for state_path, snapshot in snapshots.items():
            self.assertEqual(state_path.read_bytes(), snapshot)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_unsupported_owner_cannot_supply_host_only_resume_authorization(self):
        root_sid, root_path = self._resumable()
        sid, path = self._new_source()
        self._append(path, self._user(sid, _runtime_message('{"kind":"native_context_continuation"}')),
                     self._assistant(sid, 900, self.cwd))
        owner = self._owner([(root_sid, root_path), (sid, path)],
                            self._snapshot(root_sid, root_path, root_sid, root_path))
        core.atomic(owner.state_path, {**owner._state(), "phase": "paused"})
        before = owner.state_path.read_bytes()
        runtime, _, _ = self._manager()
        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        state = runtime._state()
        self.assertEqual(state["session_id"], sid)
        self.assertIsNone(state["authorization"])
        self.assertIsNotNone(state["pending_session_start"])
        self.assertFalse(runtime.advance()["rotation_pending"])
        self.assertEqual(owner.state_path.read_bytes(), before)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_unbound_resume_preserves_subagent_identity_denial(self):
        sid, path = self._resumable()
        self._append(path, self._assistant(sid, 300, self.evidence))
        runtime, startup_sid, _ = self._manager()
        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        for to in ("original-worker", "original-worker [abc123]"):
            with self.subTest(to=to), patch("claude_context_continuity.native_addressing.named_peer", return_value=False):
                result = runtime.on_hook(self._event("PreToolUse", sid, path,
                    tool_use_id=f"resume-{to}", tool_name="SendMessage", tool_input={"to": to, "message": "Continue."}))
                guard = result.get("hookSpecificOutput", {})
                self.assertEqual(guard.get("permissionDecision"), "deny")
                self.assertIn("native_session_identity_mismatch", guard["permissionDecisionReason"])
                self.assertEqual(runtime._state()["session_id"], startup_sid)
                self.assertIs(runtime._state()["pending_session_start"]["bound"], False)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_unbound_resume_peer_messages_do_not_request_local_identity_restoration(self):
        sid, path = self._resumable()
        self._append(path, self._assistant(sid, 300, self.evidence))
        runtime, startup_sid, _ = self._manager()
        runtime.on_hook(self._event("SessionStart", sid, path, source="resume"))
        pending = runtime._state()["pending_session_start"]
        first_failure = pending["first_failure"]
        for to in ("openmetadata-dd", "openmetadata-dd [abc123]", "uds:/tmp/cc-socks/example.sock", "main"):
            with self.subTest(to=to), patch("claude_context_continuity.native_addressing.named_peer", return_value=True):
                result = runtime.on_hook(self._event("PreToolUse", sid, path,
                    tool_use_id=str(uuid4()), tool_name="SendMessage", tool_input={"to": to, "message": "Fixture."}))
                self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
                state = runtime._state()
                self.assertEqual(state["session_id"], startup_sid)
                self.assertIs(state["pending_session_start"]["bound"], False)
                self.assertEqual(state["pending_session_start"]["first_failure"], first_failure)
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()

    def test_host_only_resume_still_requires_current_inherited_lineage(self):
        root_sid, root_path = self._resumable()
        host_sid, host_path = self._new_source()
        self._append(host_path, self._user(host_sid, _runtime_message('{"kind":"native_context_continuation"}')),
                     self._assistant(host_sid, 900, self.cwd))
        self.assertIsNone(self._bounds(host_sid, host_path)["first"])
        self._owner([(root_sid, root_path), (host_sid, host_path)],
                    self._snapshot(root_sid, root_path, root_sid, root_path))
        # The inherited latest locator is no longer the root source's latest
        # instruction, so a host-only window cannot inherit authority from it.
        self._append(root_path, self._user(root_sid, "A later genuine instruction in the root source."))
        runtime, _, _ = self._manager()

        runtime.on_hook(self._event("SessionStart", host_sid, host_path, source="resume"))
        state = runtime._state()
        self.assertEqual(state["session_id"], host_sid)
        self.assertIsNone(state["authorization"])
        self.assertIsNotNone(state["pending_session_start"])
        self.assertIsNot(state["pending_session_start"].get("bound", True), False)
        receipt = runtime.advance()
        self.assertIsNone(receipt["authorization"])
        self.assertFalse(receipt["rotation_pending"])
        self.clear_mock.assert_not_called()
        self.send_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
