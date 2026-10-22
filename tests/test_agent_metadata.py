"""原生子代理 sidecar 跨窗口恢复；不调用模型或启动后台任务。"""
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
from claude_context_continuity import core
from claude_context_continuity.tui_runtime import TuiRuntime


class AgentMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-metadata-", dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        home = patch.object(core, "HOME", self.home)
        home.start()
        self.addCleanup(home.stop)
        self.sid = str(uuid4())
        self.config = {"hash": "fixture", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        self.runtime = TuiRuntime.create(cwd=self.cwd, session_id=self.sid, configuration=self.config,
                                         configuration_reader=lambda _: self.config)
        self.source = self.root / f"{self.sid}.jsonl"
        self.source.write_text(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
            "message": {"role": "user", "content": "Continue only the authorized fixture."}}) + "\n")
        self.runtime.on_hook(self.event("SessionStart", source="startup"))
        self.agent = "a1234567890abcdef"
        self.metadata = {"agentType": "implementation-worker", "description": "Bounded implementation",
                         "toolUseId": "original-call", "model": "opus", "isBuiltIn": False,
                         "spawnDepth": 1, "requestShape": "background", "requestNonInteractive": True,
                         "permissionMode": "default", "futureNativeField": {"keep": True}}
        self.original = self.sidecar(self.sid)
        self.original.parent.mkdir(parents=True)
        self.original.write_text(json.dumps(self.metadata))

    def sidecar(self, sid, agent=None):
        return self.root / sid / "subagents" / f"agent-{agent or self.agent}.meta.json"

    def event(self, name, **values):
        return {"hook_event_name": name, "session_id": self.sid, "transcript_path": str(self.source),
                "cwd": str(self.cwd), **values}

    def clear(self):
        self.sid = str(uuid4())
        self.source = self.root / f"{self.sid}.jsonl"
        self.source.write_text("")
        return self.runtime.on_hook(self.event("SessionStart", source="clear"))

    def send(self, to=None):
        return self.runtime.on_hook(self.event("PreToolUse", tool_name="SendMessage", tool_use_id=str(uuid4()),
            tool_input={"to": to or self.agent, "message": "Continue the existing bounded task."}))

    def test_clear_preserves_active_custom_role_model_and_opaque_fields(self):
        self.runtime.on_hook(self.event("SubagentStart", agent_id=self.agent, agent_type="implementation-worker"))
        before = self.original.read_bytes()
        self.clear()
        target = self.sidecar(self.sid)
        actual = json.loads(target.read_text()) if target.exists() else {"agentType": "general-purpose"}
        self.assertEqual(actual, self.metadata)
        self.assertEqual(self.original.read_bytes(), before)
        self.assertIn(self.agent, self.runtime.receipt()["active_child_handles"])


    def test_completed_agent_survives_two_clears_and_idempotent_start(self):
        self.clear()
        first = self.sidecar(self.sid)
        self.clear()
        second = self.sidecar(self.sid)
        before = second.stat()
        self.runtime.on_hook(self.event("SessionStart", source="clear"))
        self.assertEqual(second.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(json.loads(first.read_text()), self.metadata)
        self.assertEqual(json.loads(second.read_text()), self.metadata)
        self.assertEqual(self.runtime.receipt()["active_child_handles"], [])

    def test_finished_after_clear_preserves_identity_and_current_completion_state(self):
        before = self.original.read_bytes()
        self.clear()
        target = self.sidecar(self.sid)
        completed = {**self.metadata, "finished": True}
        target.write_text(json.dumps(completed))
        target_before = target.read_bytes()
        modified = target.stat().st_mtime_ns
        result = self.send()
        self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertEqual(target.read_bytes(), target_before)
        self.assertEqual(target.stat().st_mtime_ns, modified)
        self.assertEqual(self.original.read_bytes(), before)
        self.clear()
        self.assertEqual(json.loads(self.sidecar(self.sid).read_text()), completed)
        self.assertNotEqual(self.send().get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_finished_removed_in_current_window_is_not_restored_from_old_window(self):
        self.original.write_text(json.dumps({**self.metadata, "finished": True}))
        before = self.original.read_bytes()
        self.clear()
        target = self.sidecar(self.sid)
        target.write_text(json.dumps(self.metadata))
        target_before = target.read_bytes()
        modified = target.stat().st_mtime_ns
        result = self.send()
        self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertEqual(target.read_bytes(), target_before)
        self.assertEqual(target.stat().st_mtime_ns, modified)
        self.assertEqual(self.original.read_bytes(), before)
        self.clear()
        self.assertEqual(json.loads(self.sidecar(self.sid).read_text()), self.metadata)

    def test_resume_lazily_restores_legacy_missing_sidecar_by_id_and_name(self):
        self.metadata["name"] = "bounded-worker"
        self.original.write_text(json.dumps(self.metadata))
        self.clear()
        target = self.sidecar(self.sid)
        for to in (self.agent, "bounded-worker", "bounded-worker [abc123]", "bounded-worker[abc123]", self.agent[:8],
                   f" {self.agent} ", " bounded-worker [ABC123] "):
            with self.subTest(to=to):
                target.unlink(missing_ok=True)
                result = self.send(to)
                self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
                self.assertEqual(json.loads(target.read_text()), self.metadata)

    def test_known_default_fallback_is_repaired_only_after_writer_stops(self):
        self.clear()
        target = self.sidecar(self.sid)
        broken = {key: self.metadata[key] for key in ("spawnDepth", "requestShape", "requestNonInteractive")}
        broken["agentType"] = "general-purpose"
        target.write_text(json.dumps(broken))
        self.runtime.on_hook(self.event("SubagentStart", agent_id=self.agent))
        self.assertEqual(self.send()["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(json.loads(target.read_text()), broken)
        self.runtime.on_hook(self.event("SubagentStop", agent_id=self.agent))
        result = self.send()
        self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertEqual(json.loads(target.read_text()), self.metadata)
        intents = list((self.runtime.directory / "agent_metadata").glob("*/intent.json"))
        self.assertTrue(any(json.loads(p.read_text())["previous_value"] == broken for p in intents))

    def test_general_purpose_explicit_model_is_not_replaced_with_parent_default(self):
        self.metadata["agentType"] = "general-purpose"
        self.original.write_text(json.dumps(self.metadata))
        self.clear()
        target = self.sidecar(self.sid)
        target.write_text(json.dumps({"agentType": "general-purpose", "spawnDepth": 1}))
        result = self.send()
        self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertEqual(json.loads(target.read_text())["model"], "opus")

    def test_unknown_agent_is_denied_but_peer_and_main_are_untouched(self):
        result = self.send("a0000000000000000")
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        for to in ("main", "peer-session [abc123]"):
            self.assertNotEqual(self.send(to).get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def record_named_launch(self, name, *, tool="Agent"):
        launch = {"type": "assistant", "uuid": str(uuid4()), "sessionId": self.sid,
                  "message": {"role": "assistant", "content": [{"type": "tool_use",
                      "id": self.metadata["toolUseId"], "name": tool, "input": {"name": name}}]}}
        with self.source.open("a") as stream:
            stream.write(json.dumps(launch) + "\n")

    def test_native_launch_name_with_missing_sidecar_cannot_bypass_as_same_named_peer(self):
        from types import SimpleNamespace
        self.record_named_launch("openmetadata-dd")
        self.original.unlink()
        self.pending_identity()
        listing = SimpleNamespace(returncode=0, stdout=json.dumps([{"name": "openmetadata-dd"}]))
        with patch("subprocess.run", return_value=listing):
            for to in ("openmetadata-dd", "openmetadata-dd [abc123]", "openmetadata"):
                with self.subTest(to=to):
                    guard = self.send(to)["hookSpecificOutput"]
                    self.assertEqual(guard["permissionDecision"], "deny")
                    self.assertIn("original_agent_metadata_missing", guard["permissionDecisionReason"])
        self.assertFalse(self.original.exists())

    def test_one_valid_same_named_local_does_not_hide_another_missing_identity(self):
        self.metadata["name"] = "bounded-worker"
        self.original.write_text(json.dumps(self.metadata))
        self.record_named_launch("bounded-worker")
        missing = {"type": "assistant", "uuid": str(uuid4()), "sessionId": self.sid,
                   "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "missing-launch",
                       "name": "Agent", "input": {"name": "bounded-worker"}}]}}
        with self.source.open("a") as stream:
            stream.write(json.dumps(missing) + "\n")
        before = self.original.read_bytes()
        result = self.send("bounded-worker [abc123]")
        self.assertEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertEqual(self.original.read_bytes(), before)

    def test_native_launch_name_restores_original_metadata_without_a_sidecar_name_field(self):
        self.record_named_launch("bounded-worker")
        self.clear()
        target = self.sidecar(self.sid)
        target.unlink()
        result = self.send(" BOUNDED WORKER [abc123] ")
        self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertTrue(target.exists())
        self.assertEqual(json.loads(target.read_text()), self.metadata)

    def pending_identity(self, **values):
        state = self.runtime._state()
        state.update(pending_session_start={"source": "resume", "session_id": self.sid,
            "transcript_path": str(self.source), "workspace_verified": True}, **values)
        self.runtime._save(state)

    def test_pending_sender_explicit_peer_addresses_and_main_do_not_restore_agents(self):
        self.pending_identity()
        before = self.original.read_bytes()
        for to in ("uds:/tmp/cc-socks/example.sock", "/tmp/cc-socks/example.sock",
                   "bridge:fixture-session", "did:example:fixture", "main", " MAIN ", "main [abc123]"):
            with self.subTest(to=to):
                result = self.send(to)
                self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
                self.assertTrue(self.runtime._state().get("pending_session_start"))
                self.assertEqual(self.original.read_bytes(), before)
                self.assertFalse((self.runtime.directory / "agent_metadata").exists())

    def test_pending_sender_named_native_peer_and_alias_do_not_restore_agents(self):
        self.pending_identity()
        # Native `claude agents --json` lists sessions, not in-process subagents.
        from types import SimpleNamespace
        listing = SimpleNamespace(returncode=0, stdout=json.dumps([
            {"kind": "interactive", "name": "openmetadata-dd", "sessionId": str(uuid4()),
             "cwd": str(self.cwd), "pid": 123, "status": "idle", "startedAt": 0}]))
        before = self.original.read_bytes()
        with patch("subprocess.run", return_value=listing):
            for to in ("openmetadata-dd", "openmetadata-dd [abc123]", "openmetadata-dd[abc123]", " OPENMETADATA DD "):
                with self.subTest(to=to):
                    result = self.send(to)
                    self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
                    self.assertTrue(self.runtime._state().get("pending_session_start"))
                    self.assertEqual(self.runtime._state()["agent_metadata_compat"]["status"], "not_local_agent")
        self.assertEqual(self.original.read_bytes(), before)
        self.assertFalse((self.runtime.directory / "agent_metadata").exists())

    def test_pending_sender_local_names_ids_and_prefixes_still_fail_closed(self):
        self.metadata["name"] = "bounded-worker"
        self.original.write_text(json.dumps(self.metadata))
        self.pending_identity()
        for to in (self.agent, self.agent[:8], "bounded-worker", "bounded-worker [abc123]",
                   " BOUNDED WORKER ", "bounded"):
            with self.subTest(to=to), patch("subprocess.run") as discovery:
                result = self.send(to)
                guard = result["hookSpecificOutput"]
                self.assertEqual(guard["permissionDecision"], "deny")
                self.assertIn("native_session_identity_pending", guard["permissionDecisionReason"])
                discovery.assert_not_called()
        self.assertFalse((self.runtime.directory / "agent_metadata").exists())

    def test_peer_name_collision_and_unreadable_local_registry_do_not_bypass_identity(self):
        from types import SimpleNamespace
        listing = SimpleNamespace(returncode=0, stdout=json.dumps([{"name": "openmetadata-dd"}]))
        self.pending_identity()
        self.metadata["name"] = "openmetadata-dd"
        self.original.write_text(json.dumps(self.metadata))
        with patch("subprocess.run", return_value=listing):
            for to in ("openmetadata-dd", "openmetadata-dd [abc123]"):
                self.assertEqual(self.send(to)["hookSpecificOutput"]["permissionDecision"], "deny")
            self.original.write_text("{incomplete")
            self.assertEqual(self.send("openmetadata-dd")["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertNotEqual(self.send("uds:/tmp/cc-socks/example.sock").get("hookSpecificOutput", {}).get(
                "permissionDecision"), "deny")

    def test_unreadable_local_metadata_directory_is_not_absence_of_a_local_name(self):
        from types import SimpleNamespace
        self.pending_identity()
        original_scandir = os.scandir
        def unreadable(path):
            if Path(path) == self.original.parent:
                raise PermissionError("fixture directory is unreadable")
            return original_scandir(path)
        with patch("os.scandir", side_effect=unreadable), patch("subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps([{"name": "openmetadata-dd"}]))):
            result = self.send("openmetadata-dd")
            self.assertEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_only_sendmessage_enters_the_current_resume_guard(self):
        from claude_context_continuity import agent_metadata
        current = self.event("PreToolUse", tool_name="SendMessage", tool_input={"to": self.agent})
        self.assertEqual(agent_metadata.recipient(current), self.agent)
        for tool in ("Agent", "Task"):
            with self.subTest(tool=tool):
                obsolete = self.event("PreToolUse", tool_name=tool,
                    tool_input={"resume": self.agent, "prompt": "Continue."})
                self.assertIsNone(agent_metadata.recipient(obsolete))
                self.assertEqual(agent_metadata.failure_hook(obsolete), {})

    def test_missing_original_local_identity_is_not_replaced_by_peer_discovery(self):
        self.original.write_text(json.dumps({"agentType": "general-purpose", "spawnDepth": 1}))
        with patch("subprocess.run") as discovery:
            guard = self.send()["hookSpecificOutput"]
            self.assertEqual(guard["permissionDecision"], "deny")
            self.assertIn("original_agent_identity_missing", guard["permissionDecisionReason"])
            discovery.assert_not_called()

    def test_pending_peer_discovery_must_supply_positive_evidence(self):
        from types import SimpleNamespace
        self.pending_identity()
        for output in ("[]", "{", '{"name":"openmetadata-dd"}'):
            with self.subTest(output=output), patch("subprocess.run", return_value=SimpleNamespace(
                    returncode=0, stdout=output)):
                guard = self.send("openmetadata-dd")["hookSpecificOutput"]
                self.assertEqual(guard["permissionDecision"], "deny")
                self.assertIn("native_session_identity_pending", guard["permissionDecisionReason"])

    def test_unrelated_tool_or_user_prose_does_not_create_a_local_agent_name(self):
        from types import SimpleNamespace
        self.record_named_launch("openmetadata-dd", tool="Bash")
        self.pending_identity()
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0,
                stdout=json.dumps([{"name": "openmetadata-dd"}]))):
            self.assertNotEqual(self.send("openmetadata-dd").get("hookSpecificOutput", {}).get(
                "permissionDecision"), "deny")

    def test_explicit_peer_address_is_independent_of_missing_uncertain_sender_identity(self):
        state = self.runtime._state()
        state.update(source_path=None, native_session_uncertain=True, pending_session_start=True)
        for to in ("uds:/tmp/cc-socks/example.sock", "main"):
            event = self.event("PreToolUse", session_id=str(uuid4()), tool_name="SendMessage", tool_input={"to": to})
            self.assertEqual(self.runtime._agent_metadata(state, event), {})
        event = self.event("PreToolUse", tool_name="SendMessage", tool_input={"to": self.agent})
        guard = self.runtime._agent_metadata(state, event)["hookSpecificOutput"]
        self.assertEqual(guard["permissionDecision"], "deny")

    def test_local_agent_cannot_restore_from_a_hook_with_missing_session_identity(self):
        event = self.event("PreToolUse", tool_name="SendMessage", tool_input={"to": self.agent})
        event.pop("session_id")
        guard = self.runtime._agent_metadata(self.runtime._state(), event)["hookSpecificOutput"]
        self.assertEqual(guard["permissionDecision"], "deny")
        self.assertIn("native_session_identity_mismatch", guard["permissionDecisionReason"])

    def test_unknown_metadata_and_model_conflicts_are_never_overwritten(self):
        self.clear()
        target = self.sidecar(self.sid)
        for change in ({"agentType": "research-scout"}, {"model": "haiku"}, {"toolUseId": "other-call"},
                       {"futureNativeField": {"keep": False}}, {"permissionMode": "bypassPermissions"}):
            with self.subTest(change=change):
                target.write_text(json.dumps({**self.metadata, "finished": True, **change}))
                before = target.read_bytes()
                self.assertEqual(self.send()["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertEqual(target.read_bytes(), before)

    def test_stop_marker_is_not_lost_after_migration(self):
        self.clear()
        self.sidecar(self.sid).write_text(json.dumps({**self.metadata, "finished": True}))
        self.original.write_text(json.dumps({**self.metadata, "stoppedByUser": True}))
        result = self.send()
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("agent_stopped_by_user", result["hookSpecificOutput"]["permissionDecisionReason"])
        self.sidecar(self.sid).unlink()
        self.send()
        self.assertTrue(json.loads(self.sidecar(self.sid).read_text())["stoppedByUser"])

    def test_malformed_symlink_and_secret_sidecars_fail_closed(self):
        self.clear()
        target = self.sidecar(self.sid)
        target.unlink()
        self.original.write_text("{incomplete")
        self.assertEqual(self.send()["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertFalse(target.exists())
        self.original.write_text(json.dumps(self.metadata))
        target.symlink_to(self.original)
        self.assertEqual(self.send()["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(target.is_symlink())
        target.unlink()
        secret = "fixture-secret-do-not-copy-8675309"
        with patch.dict(os.environ, {"CC_TEST_AUTH_TOKEN": secret}):
            self.original.write_text(json.dumps({**self.metadata, "description": secret}))
            result = self.send()
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertNotIn(secret, json.dumps(result))
            self.assertFalse(target.exists())

    def test_nested_native_agent_path_and_fork_identity_are_preserved(self):
        value = {**self.metadata, "isFork": True, "worktreePath": "/fixture/original-worktree"}
        nested = self.original.parent / "nested" / self.original.name
        nested.parent.mkdir()
        nested.write_text(json.dumps(value))
        self.original.unlink()
        self.clear()
        target = self.sidecar(self.sid).parent / "nested" / self.original.name
        self.assertEqual(json.loads(target.read_text()), value)
        self.assertFalse(self.sidecar(self.sid).exists())

    def test_source_change_and_destination_race_do_not_publish_wrong_identity(self):
        from claude_context_continuity import agent_metadata
        self.clear()
        target = self.sidecar(self.sid)
        target.unlink()
        with patch.object(agent_metadata, "_same_snapshot", side_effect=agent_metadata.MetadataError("changed")):
            self.assertEqual(self.send()["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertFalse(target.exists())
        original_atomic = core.atomic
        other = {**self.metadata, "model": "haiku"}
        def competing_writer(path, value, **kwargs):
            if Path(path) == target:
                target.write_text(json.dumps(other))
            return original_atomic(path, value, **kwargs)
        with patch.object(core, "atomic", side_effect=competing_writer):
            self.assertEqual(self.send()["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(json.loads(target.read_text()), other)

    def test_sendmessage_restore_and_child_calls_are_guarded(self):
        self.clear()
        self.sidecar(self.sid).unlink()
        result = self.send()
        self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertEqual(json.loads(self.sidecar(self.sid).read_text()), self.metadata)
        result = self.runtime.on_hook(self.event("PreToolUse", agent_id="a9876543210fedcba", tool_name="SendMessage",
            tool_input={"to": "a0000000000000000", "message": "continue"}))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_generated_hook_cli_separates_explicit_peer_from_local_resume(self):
        import subprocess
        from claude_context_continuity.tui_runtime import _write_plugin
        self.pending_identity()
        plugin = self.runtime.directory / "plugin"
        _write_plugin(plugin, self.runtime.conversation_id)
        command = core.read_json(plugin / ".claude-plugin/plugin.json")["hooks"]["PreToolUse"][0]["hooks"][0]
        config = self.root / "config"
        config.mkdir()
        environment = {**os.environ, "CLAUDE_CONFIG_DIR": str(config),
            "CLAUDE_CONTEXT_CONTINUITY_DIR": str(self.home), "CLAUDE_CONTINUITY_ID": self.runtime.conversation_id,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000", "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1"}
        for key in ("CLAUDE_BG_RV_AUTH", "CLAUDE_BG_RENDEZVOUS_SOCK", "CCLAUDE_CONTROL_AUTH_TOKEN", "CCLAUDE_CONTROL_SOCKET"):
            environment.pop(key, None)
        for to, expected in (("uds:/tmp/cc-socks/example.sock", None), (self.agent, "deny")):
            event = self.event("PreToolUse", tool_use_id=str(uuid4()), tool_name="SendMessage",
                               tool_input={"to": to, "message": "Fixture event only; no native message is sent."})
            completed = subprocess.run([command["command"], *command["args"]], input=json.dumps(event),
                cwd=self.cwd, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            self.assertEqual(output.get("hookSpecificOutput", {}).get("permissionDecision"), expected)
        self.assertTrue(self.runtime._state().get("pending_session_start"))
        self.assertFalse((self.runtime.directory / "agent_metadata").exists())

    def test_hook_dispatch_does_not_swallow_resume_guard_failure(self):
        import argparse
        import io
        from claude_context_continuity import continuity, tui_runtime
        args = argparse.Namespace(action="tui-hook", context_id=self.runtime.conversation_id)
        event = self.event("PreToolUse", tool_name="SendMessage", tool_input={"to": self.agent, "message": "continue"})
        with patch.dict(os.environ, {"CLAUDE_CONTINUITY_ID": self.runtime.conversation_id}), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
                patch.object(tui_runtime.TuiRuntime, "load", side_effect=RuntimeError("fixture observer failure")):
            result = continuity.dispatch(args)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
