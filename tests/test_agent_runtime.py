"""Child budgets stay separate from parent authorization, usage and rotation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claude_context_continuity import agent_runtime, core
from claude_context_continuity.tui_runtime import TuiRuntime


class AgentBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        home = patch.object(core, "HOME", self.root / "runtime-home")
        home.start()
        self.addCleanup(home.stop)
        self.sid, self.agent = str(uuid4()), "a12345678"
        self.config = {"hash": "fixture", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        self.runtime = TuiRuntime.create(cwd=self.root, session_id=self.sid, configuration=self.config,
                                         configuration_reader=lambda _: self.config)
        self.parent = self.root / f"{self.sid}.jsonl"
        self.parent.write_text(json.dumps({"type": "user", "uuid": str(uuid4()), "sessionId": self.sid,
            "message": {"role": "user", "content": "Perform the isolated fixture."}}) + "\n")
        self.path = self.root / self.sid / "subagents" / f"agent-{self.agent}.jsonl"
        self.path.parent.mkdir(parents=True)
        self.path.write_text("")
        state = self.runtime._state()
        state.update(source_path=str(self.parent), phase="running")
        self.runtime._save(state)
        self.runtime.on_hook(self.event("SubagentStart", agent_type="implementation-worker"))

    def event(self, name, **extra):
        return {"hook_event_name": name, "session_id": self.sid, "agent_id": self.agent,
                "cwd": str(self.root), "transcript_path": str(self.parent), **extra}

    def append(self, value, *, cache=0, output=0, model="child-model", **extra):
        row = {"type": "assistant", "uuid": str(uuid4()), "sessionId": self.sid,
               "agentId": self.agent, "isSidechain": True,
               "message": {"id": "msg_" + uuid4().hex, "role": "assistant", "model": model,
                           "content": [{"type": "text", "text": "child-public-text"}],
                           "usage": {"input_tokens": value, "cache_creation_input_tokens": 0,
                                     "cache_read_input_tokens": cache, "output_tokens": output}}, **extra}
        with self.path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        return row

    def child(self):
        return agent_runtime._load(self.runtime.directory, self.agent)

    def test_child_usage_never_mutates_parent_budget_or_rotation(self):
        before = self.runtime._state()
        self.append(50, cache=600, output=120)
        result = self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        after = self.runtime._state()
        self.assertFalse(result["continue"])
        for key in ("usage", "budget", "native_context_window", "rotation", "authorization", "pending_tool_ids"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.child()["remaining_tokens"], 230)
        self.assertTrue(self.child()["replacement_required"])

    def test_native_window_is_child_only_and_parent_aggregate_is_ignored(self):
        self.append(420)
        self.runtime.on_hook(self.event("PreToolUse", contextWindow=500, tool_use_id="one"))
        self.assertEqual(self.child()["window_tokens"], 500)
        self.assertTrue(self.child()["replacement_required"])
        self.assertIsNone(self.runtime._state()["native_context_window"])
        self.runtime.on_hook(self.event("PostToolUse", modelUsage={"parent": {"contextWindow": 100}}))
        self.assertEqual(self.child()["window_tokens"], 500)

    def test_incomplete_cache_and_unknown_window_are_not_zero(self):
        row = self.append(20)
        del row["message"]["usage"]["cache_read_input_tokens"]
        self.path.write_text(json.dumps(row) + "\n")
        result = self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        self.assertIsNot(result.get("continue"), False)
        self.assertIsNone(self.child()["remaining_tokens"])
        child = self.child()
        agent_runtime._observe(child, self.path, self.sid, None)
        self.assertIsNone(child["window_tokens"])
        self.assertEqual(child["window_source"], "unknown")

    def test_synthetic_zero_does_not_overwrite_actual_usage(self):
        self.append(800)
        self.append(0, model="<synthetic>")
        self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        self.assertEqual(self.child()["usage"]["total_input_tokens"], 800)
        self.assertTrue(self.child()["replacement_required"])

    def test_cross_parent_rotation_retains_child_observations(self):
        self.append(810)
        state = self.runtime._state()
        new_sid = str(uuid4())
        state.update(session_id=new_sid, source_path=str(self.root / f"{new_sid}.jsonl"))
        self.runtime._save(state)
        result = self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        self.assertFalse(result["continue"])
        self.runtime.on_hook(self.event("SubagentStop", agent_transcript_path=str(self.path)))
        state = self.runtime._state()
        self.assertNotIn(self.agent, state["active_child_handles"])
        self.assertTrue(self.child()["terminal_observed"])
        notice = agent_runtime.parent_notice(state, {"hook_event_name": "PostToolBatch"}, self.runtime.directory)
        self.assertIn(self.agent, notice["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(agent_runtime.parent_notice(state, {"hook_event_name": "PostToolBatch"}, self.runtime.directory), {})

    def test_cross_parent_rotation_prefers_current_child_transcript(self):
        self.append(100)
        self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        old_sid, old_parent, old_path = self.sid, self.parent, self.path
        self.sid = str(uuid4())
        self.parent = self.root / f"{self.sid}.jsonl"
        self.path = self.root / self.sid / "subagents" / f"agent-{self.agent}.jsonl"
        self.path.parent.mkdir(parents=True)
        state = self.runtime._state()
        state.update(session_id=self.sid, source_path=str(self.parent))
        state["child_handle_sources"] = {self.agent: [old_sid, str(old_parent)]}
        self.runtime._save(state)
        current = self.append(800)

        result = self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))

        assert old_path.is_file()
        assert self.child()["usage"]["request_id"] == current["message"]["id"]
        assert self.child()["usage"]["total_input_tokens"] == 800
        assert self.child()["source_path"] == str(self.path)
        assert self.child()["session_id"] == self.sid
        assert result["continue"] is False
        assert self.child()["replacement_required"] is True
        assert self.agent in self.runtime._state()["agent_handoffs"]

    def test_new_parent_hook_falls_back_to_existing_child_transcript(self):
        self.append(100)
        self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        state = self.runtime._state()
        new_sid = str(uuid4())
        new_parent = self.root / f"{new_sid}.jsonl"
        state.update(session_id=new_sid, source_path=str(new_parent))
        state["child_handle_sources"] = {self.agent: [self.sid, str(self.parent)]}
        self.runtime._save(state)
        current = self.append(800)

        result = self.runtime.on_hook(self.event("PostToolBatch", session_id=new_sid,
            transcript_path=str(new_parent), tool_calls=[]))

        assert self.child()["usage"]["request_id"] == current["message"]["id"]
        assert self.child()["usage"]["total_input_tokens"] == 800
        assert self.child()["source_path"] == str(self.path)
        assert self.child()["session_id"] == self.sid
        assert result["continue"] is False
        assert self.agent in self.runtime._state()["agent_handoffs"]

    def test_unrelated_child_event_cannot_read_or_mutate_parent(self):
        self.append(900)
        before = self.runtime._state()
        self.runtime.on_hook(self.event("PostToolBatch", session_id=str(uuid4()), tool_calls=[]))
        self.assertEqual(self.runtime._state(), before)
        self.assertFalse(self.child()["replacement_required"])

    def test_wrong_agent_partial_tail_and_symlink(self):
        self.append(100)
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"assistant"')
        sample = agent_runtime.transcript_usage(self.path, self.sid, self.agent)
        self.assertEqual(sample["usage"]["total_input_tokens"], 100)
        row = self.append(700)
        row["agentId"] = "another"
        self.path.write_text(json.dumps(row) + "\n")
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            agent_runtime.transcript_usage(self.path, self.sid, self.agent)
        target = self.path.with_suffix(".backup")
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            agent_runtime.transcript_usage(self.path, self.sid, self.agent)

    def metadata(self, **extra):
        value = {"agentType": "implementation-worker", "model": "opus", "spawnDepth": 1,
                 "toolUseId": "launch-fixture", "name": "fixture-worker", **extra}
        self.path.with_suffix(".meta.json").write_text(json.dumps(value))
        return value

    def resume(self, to=None):
        return self.runtime.on_hook({"hook_event_name": "PreToolUse", "session_id": self.sid,
            "cwd": str(self.root), "transcript_path": str(self.parent), "tool_name": "SendMessage",
            "tool_use_id": "resume-fixture", "tool_input": {"to": to or self.agent, "message": "continue"}})

    def test_old_oversized_agent_resume_by_id_or_name_is_denied(self):
        metadata = self.metadata()
        self.append(800)
        for address in (self.agent, "fixture-worker"):
            result = self.resume(address)
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("超限子代理", result["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(json.loads(self.path.with_suffix(".meta.json").read_text()), metadata)
        self.assertEqual(self.child()["agent_type"], "implementation-worker")
        self.assertEqual(self.child()["original_tool_use_id"], "launch-fixture")

    def test_small_agent_resume_and_cancelled_identity_keep_native_contract(self):
        self.metadata()
        self.append(40)
        result = self.resume()
        self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.metadata(stoppedByUser=True)
        denied = self.resume()
        self.assertIn("agent_stopped_by_user", denied["hookSpecificOutput"]["permissionDecisionReason"])

    def test_context_failure_does_not_claim_request_was_sent(self):
        self.metadata()
        self.append(20)
        row = self.append(0, model="<synthetic>", isApiErrorMessage=True, error="invalid_request")
        row["message"]["content"] = [{"type": "text", "text": "Prompt is too long"}]
        with self.path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        result = self.resume()
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(self.child()["context_rejected"])
        serialized = json.dumps(self.child())
        self.assertNotIn("child-public-text", serialized)
        self.assertNotIn('"request_sent": true', serialized)

    def test_native_finished_without_stop_hook_delivers_one_handoff(self):
        self.metadata()
        self.append(810)
        stopped = self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        self.assertFalse(self.child()["terminal_observed"])
        self.metadata(finished=True)
        with self.path.open("a") as stream:
            stream.write(json.dumps({"type": "attachment", "agentId": self.agent, "sessionId": self.sid,
                "attachment": {"type": "hook_stopped_continuation", "hookEvent": "PostToolBatch",
                               "message": stopped["stopReason"]}}) + "\n")
        state = self.runtime._state()
        result = agent_runtime.parent_notice(state, {"hook_event_name": "PostToolBatch"}, self.runtime.directory)
        self.assertIn(self.agent, result["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.child()["terminal_observed"])
        self.assertIn(self.agent, state["active_child_handles"])

    def test_known_stop_survives_a_later_transcript_read_fault(self):
        self.append(810)
        self.runtime.on_hook(self.event("PreToolUse", tool_use_id="read-fault"))
        with patch.object(agent_runtime, "transcript_usage", side_effect=OSError("fixture")):
            result = self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        self.assertFalse(result["continue"])
        self.assertTrue(self.child()["replacement_required"])
        self.assertIn(self.agent, self.runtime._state()["agent_handoffs"])

    def test_oversized_record_does_not_hide_later_real_usage(self):
        with self.path.open("a") as stream:
            stream.write(json.dumps({"type": "user", "payload": "x" * agent_runtime._MAX_LINE}) + "\n")
        self.append(810)
        result = self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        self.assertFalse(result["continue"])
        self.assertTrue(self.child()["history_incomplete"])
        self.assertEqual(self.child()["usage"]["total_input_tokens"], 810)

    def test_stale_finished_sidecar_cannot_confirm_current_stop(self):
        self.metadata(finished=True)
        self.append(810)
        self.runtime.on_hook(self.event("PreToolUse", tool_use_id="still-running"))
        state = self.runtime._state()
        result = agent_runtime.parent_notice(state, {"hook_event_name": "PostToolBatch"}, self.runtime.directory)
        self.assertEqual(result, {})
        self.assertFalse(self.child()["terminal_observed"])
        self.assertIn(self.agent, state["active_child_handles"])

    def test_child_source_survives_unbound_parent_with_new_hook_session(self):
        self.append(810)
        self.runtime.on_hook(self.event("PreToolUse", tool_use_id="before-clear"))
        state = self.runtime._state()
        new_sid = str(uuid4())
        state.update(session_id=new_sid, source_path=None)
        self.runtime._save(state)
        result = self.runtime.on_hook(self.event("PostToolBatch", session_id=new_sid,
            agent_transcript_path=str(self.path), tool_calls=[]))
        self.assertFalse(result["continue"])
        self.assertEqual(self.child()["source_path"], str(self.path))
        self.assertEqual(self.child()["session_id"], self.sid)
        self.assertIsNone(self.runtime._state()["source_path"])

    def test_parent_halt_does_not_consume_notice_and_new_window_can_receive_it(self):
        self.append(810)
        self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        self.runtime.on_hook(self.event("SubagentStop", agent_transcript_path=str(self.path)))
        state = self.runtime._state()
        event = {"hook_event_name": "PostToolBatch"}
        self.assertEqual(agent_runtime.parent_notice(state, event, self.runtime.directory, delivery_allowed=False), {})
        self.assertIsNone(self.child().get("notified_session_id"))
        self.assertIn("hookSpecificOutput", agent_runtime.parent_notice(state, event, self.runtime.directory))
        self.assertEqual(agent_runtime.parent_notice(state, event, self.runtime.directory), {})
        state["session_id"] = str(uuid4())
        self.assertIn("hookSpecificOutput", agent_runtime.parent_notice(state, event, self.runtime.directory))

    def test_parent_prompt_delivers_child_handoff(self):
        self.append(810)
        self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        self.runtime.on_hook(self.event("SubagentStop", agent_transcript_path=str(self.path)))
        result = self.runtime.on_hook({"hook_event_name": "UserPromptSubmit", "session_id": self.sid,
            "cwd": str(self.root), "transcript_path": str(self.parent), "prompt": "Continue the fixture."})
        self.assertIn(self.agent, result["hookSpecificOutput"]["additionalContext"])

    def test_parent_rotation_packet_carries_child_handoff(self):
        self.append(810)
        self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        self.runtime.on_hook({"hook_event_name": "PreToolUse", "session_id": self.sid,
            "cwd": str(self.root), "transcript_path": str(self.parent), "tool_use_id": "bootstrap"})
        self.runtime.request_rotation("Continue only the fixture.")
        packet = json.loads(self.runtime._continuation(self.runtime._state()))
        self.assertEqual(packet["agent_context_handoffs"][self.agent],
                         str(agent_runtime._path(self.runtime.directory, self.agent)))

    def test_new_request_invalidates_prior_terminal_observation(self):
        self.append(100)
        self.runtime.on_hook(self.event("SubagentStop", agent_transcript_path=str(self.path)))
        self.assertTrue(self.child()["terminal_observed"])
        self.append(810)
        self.runtime.on_hook(self.event("PreToolUse", tool_use_id="new-execution"))
        self.assertFalse(self.child()["terminal_observed"])

    def test_corrupt_child_notice_cannot_break_parent_hook(self):
        self.append(810)
        self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        agent_runtime._path(self.runtime.directory, self.agent).write_text("invalid-json")
        result = self.runtime.on_hook({"hook_event_name": "PostToolBatch", "session_id": self.sid,
            "cwd": str(self.root), "transcript_path": str(self.parent), "tool_calls": []})
        self.assertIsInstance(result, dict)

    def test_handoff_waits_for_terminal_and_does_not_restart_tools(self):
        self.append(800)
        self.runtime.on_hook(self.event("PostToolBatch", tool_calls=[]))
        state = self.runtime._state()
        self.assertEqual(agent_runtime.parent_notice(state, {"hook_event_name": "PreToolUse"}, self.runtime.directory), {})
        self.assertIn(self.agent, state["active_child_handles"])
        self.assertIsNone(state["rotation"]["request"])
        self.runtime.on_hook(self.event("SubagentStop", agent_transcript_path=str(self.path)))
        self.assertTrue(self.child()["terminal_observed"])


if __name__ == "__main__":
    unittest.main()
