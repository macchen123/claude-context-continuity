"""Offline metadata consumers; no native process, message, or transcript read."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from claude_context_continuity import continuity, core, native_addressing, peer_lineage
from claude_context_continuity.tui_runtime import TuiRuntime


class PeerLineageTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / ".cache" / "peer-lineage-tests"
        cache.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        home = patch.object(core, "HOME", self.home)
        home.start()
        self.addCleanup(home.stop)
        self.rows = []
        self.real_discover = native_addressing.discover_peers
        discover = patch.object(native_addressing, "discover_peers", side_effect=lambda: self.rows)
        self.discovery = discover.start()
        self.addCleanup(discover.stop)
        self.config = {"hash": "fixture", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        self.peer, self.ids, self.row = self.context()

    def context(self, *, windows=3, name="same-peer", pid=None, ids=None):
        ids = ids or [str(uuid4()) for _ in range(windows)]
        runtime = TuiRuntime.create(cwd=self.cwd, session_id=ids[-1], configuration=self.config)
        state = runtime._state()
        state.update(transport="tmux_tui", controller_managed=True, phase="running",
                     owned_pid=pid or (12000 + len(self.rows)), initial_session_started=True)
        for generation, sid in enumerate(ids):
            state.update(session_id=sid, source_path=str(self.root / f"{sid}.jsonl"), window_generation=generation)
            runtime._catalogue_source(state)
        state["rotation"] = {"generation": len(ids) - 1, "request": None, "clear": None}
        runtime._save(state)
        row = {"kind": "interactive", "pid": state["owned_pid"], "sessionId": ids[-1],
               "name": name, "cwd": str(self.cwd), "status": "busy"}
        self.rows.append(row)
        return runtime, ids, row

    def change(self, runtime=None, **fields):
        runtime = runtime or self.peer
        state = runtime._state()
        state.update(fields)
        runtime._save(state)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def hook(self, *, tool="ListAgents", recipient="same-peer", response=None, **fields):
        event = {"hook_event_name": "PostToolUse", "session_id": self.ids[-1], "tool_name": tool,
                 "tool_input": {"to": recipient, "message": "A message-body UUID is not a recipient " + self.ids[0]},
                 "tool_response": {"success": True} if response is None else response}
        event.update(fields)
        return event

    def invoke_hook(self, event, original=None):
        original = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "existing context"},
                    "custom": {"tool_output": "native output stays unchanged"}} if original is None else original
        before = deepcopy((event, original))
        with patch.object(self.peer, "_on_hook", return_value=original), patch.object(self.peer, "_diagnose"):
            result = self.peer.on_hook(event)
        self.assertEqual((event, original), before)
        return result

    def test_oldest_of_two_renews_maps_to_existing_stable_context_read_only(self):
        before = self.snapshot()
        result = peer_lineage.peer_lineage(self.ids[0])
        self.assertEqual(result["status"], "verified")
        peer = result["peers"][0]
        self.assertEqual(peer["conversation_id"], self.peer.conversation_id)
        self.assertEqual(peer["current_session_id"], self.ids[-1])
        self.assertEqual(peer["confirmed_session_ids"], self.ids)
        self.assertFalse(result["write_authority"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(self.root.glob("*.jsonl")))

    def test_same_name_and_cwd_do_not_merge_different_contexts(self):
        other, ids, row = self.context()
        result = peer_lineage.peer_lineage()
        self.assertEqual(len(result["peers"]), 2)
        self.assertEqual(peer_lineage.peer_lineage(ids[0])["peers"][0]["conversation_id"], other.conversation_id)
        self.assertEqual(peer_lineage.peer_lineage(self.ids[0])["peers"][0]["pid"], self.row["pid"])
        self.assertEqual(peer_lineage.peer_lineage(recipient="same-peer")["peers"], [])

    def test_duplicate_or_competing_contexts_do_not_elect_successor(self):
        other, _, _ = self.context(ids=self.ids, pid=self.row["pid"])
        self.rows.pop()  # One native row, two contexts claiming exactly that row.
        result = peer_lineage.peer_lineage(self.ids[0])
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["peers"], [])
        self.assertEqual(peer_lineage.peer_lineage()["peers"], [])

    def test_competing_historical_epoch_is_ambiguous_even_with_distinct_live_rows(self):
        self.context(ids=[self.ids[0], str(uuid4()), str(uuid4())], name="other")
        self.assertEqual(peer_lineage.peer_lineage(self.ids[0])["status"], "ambiguous")
        self.assertEqual(peer_lineage.peer_lineage()["peers"], [])

    def test_pending_sibling_is_not_confirmed_history_or_a_successor(self):
        pending, ids, row = self.context(name="pending")
        self.change(pending, pending_session_start={"bound": False, "session_id": self.ids[0]},
                    launch_session_ids=[self.ids[0]])
        result = peer_lineage.peer_lineage(self.ids[0])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["peers"][0]["conversation_id"], self.peer.conversation_id)
        self.assertEqual(peer_lineage.peer_lineage(ids[0])["peers"], [])

    def test_pending_uncertain_stale_and_unmanaged_bindings_are_not_proof(self):
        original = self.peer._state()
        cases = [{"pending_session_start": {"bound": False}}, {"pending_session_start": {"bound": True}},
                 {"pending_session_start": {}}, {"native_session_uncertain": "invalid"},
                 {"reconciliation": {}}, {"rotation": {"generation": "invalid"}},
                 {"native_session_uncertain": True}, {"launch_pending": True},
                 {"controller_managed": False}, {"transport": "other"}, {"phase": "closed"},
                 {"owned_pid": self.row["pid"] + 1}, {"owned_pid": True},
                 {"reconciliation": {"stage": "running", "reason": "pending"}},
                 {"rotation": {"generation": 2, "clear": {"pending": True}, "request": None}}]
        for fields in cases:
            with self.subTest(fields=fields):
                self.peer._save({**original, **fields})
                self.assertEqual(peer_lineage.peer_lineage(self.ids[0])["peers"], [])
        self.peer._save(original)
        self.row["sessionId"] = str(uuid4())
        self.assertEqual(peer_lineage.peer_lineage()["peers"], [])

    def test_duplicate_native_sid_or_pid_is_not_unique(self):
        for fields in ({}, {"pid": self.row["pid"] + 1}, {"sessionId": str(uuid4())},
                       {"pid": "malformed"}, {"sessionId": "malformed"}):
            with self.subTest(fields=fields):
                self.rows.append({**self.row, **fields})
                self.assertEqual(peer_lineage.peer_lineage()["peers"], [])
                self.rows.pop()

    def test_no_catalogue_or_malformed_entries_or_state_yields_no_successor(self):
        path = next((self.peer.directory / "history").glob("*.json"))
        raw = path.read_bytes()
        for content in ("{", "null", json.dumps({"session_id": self.ids[0], "generation": 0, "source_path": "/bad"})):
            path.write_text(content)
            self.assertEqual(peer_lineage.peer_lineage()["peers"], [])
        path.write_bytes(raw)
        state = self.peer.state_path.read_bytes()
        for content in ("{", "null", json.dumps({"schema": "invalid", "conversation_id": self.peer.conversation_id})):
            self.peer.state_path.write_text(content)
            self.assertEqual(peer_lineage.peer_lineage()["peers"], [])
        self.peer.state_path.write_bytes(state)

    def test_state_changes_during_catalogue_read_are_not_joined(self):
        original = self.peer._state()
        with patch.object(peer_lineage.ContextRuntime, "_state", side_effect=[original, {**original, "native_session_uncertain": True}]):
            self.assertEqual(peer_lineage.peer_lineage()["peers"], [])

    def test_unknown_and_launch_only_session_ids_are_not_history(self):
        sid = str(uuid4())
        self.change(launch_session_ids=[sid], superseded_lifecycle={"session_id": sid})
        self.assertEqual(peer_lineage.peer_lineage(sid)["status"], "unverified")

    def test_full_catalogue_lookup_retains_requested_old_id_despite_display_limits(self):
        other, ids, row = self.context(windows=25, name="long-history")
        overview = peer_lineage.peer_lineage()
        self.assertTrue(overview["truncated"])
        result = peer_lineage.peer_lineage(ids[0])
        self.assertEqual(result["status"], "verified")
        self.assertIn(ids[0], result["peers"][0]["confirmed_session_ids"])
        self.assertEqual(result["peers"][0]["confirmed_session_count"], 25)
        self.assertIn("--session-id", result["lookup"])

    def test_cli_public_main_parses_old_sid_and_emits_consumer_json(self):
        with patch.object(sys, "argv", ["claude-context", "peer-lineage", "--session-id", self.ids[0]]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(continuity.main(), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["queried_session_id"], self.ids[0])
        self.assertEqual(result["peers"][0]["current_session_id"], self.ids[-1])
        with patch.object(sys, "argv", ["claude-context", "peer-lineage"]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(continuity.main(), 0)
        self.assertEqual(len(json.loads(out.getvalue())["peers"]), 1)
        with patch.object(sys, "argv", ["claude-context", "peer-lineage", "--session-id", "not-a-uuid"]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(continuity.main(), 2)

    def test_hook_listagents_adds_metadata_and_preserves_existing_context_and_outputs(self):
        event = self.hook(response={"agents": self.rows})
        result = self.invoke_hook(event)
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith("existing context\n"))
        self.assertIn(self.ids[0], text)
        self.assertIn(self.ids[-1], text)
        self.assertIn("not instructions", text)
        self.assertEqual(result["custom"]["tool_output"], "native output stays unchanged")

    def test_hook_successful_named_peer_and_ref_suffix_are_associated_not_body_uuids(self):
        for to in (" SAME PEER ", "same-peer [abc123]", "same"):
            result = self.invoke_hook(self.hook(tool="SendMessage", recipient=to))
            self.assertIn(self.peer.conversation_id, result["hookSpecificOutput"]["additionalContext"])
        for to in ("unknown", "a1234567890abcdef", self.ids[0], "main", "uds:/example.sock", "/example.sock"):
            result = self.invoke_hook(self.hook(tool="SendMessage", recipient=to))
            self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "existing context")

    def test_native_peer_name_collision_with_local_subagent_does_not_label_local_send(self):
        directory = self.root / self.ids[-1] / "subagents"
        directory.mkdir(parents=True)
        metadata = directory / "agent-a1234567890abcdef.meta.json"
        metadata.write_text(json.dumps({"agentType": "implementation-worker", "name": "same-peer"}))
        result = self.invoke_hook(self.hook(tool="SendMessage"))
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "existing context")
        metadata.write_text("{")
        result = self.invoke_hook(self.hook(tool="SendMessage"))
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "existing context")
        self.row["name"] = "a1234567890abcdef"
        result = self.invoke_hook(self.hook(tool="SendMessage", recipient=self.row["name"]))
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "existing context")

    def test_background_or_unmanaged_names_still_make_native_recipient_ambiguous(self):
        self.rows.append({"kind": "background", "name": "same-peer", "sessionId": str(uuid4())})
        result = self.invoke_hook(self.hook(tool="SendMessage"))
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "existing context")
        self.rows[-1]["name"] = "same-other"
        result = self.invoke_hook(self.hook(tool="SendMessage", recipient="same"))
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "existing context")

    def test_failed_unknown_local_subagent_and_inapplicable_hooks_stay_unchanged(self):
        cases = [self.hook(tool="SendMessage", response={"success": False}),
                 self.hook(tool="SendMessage", response={"error": "No transcript found"}),
                 self.hook(tool="SendMessage", response="No transcript found"),
                 self.hook(tool="SendMessage", response={}), self.hook(agent_id="a1234567890abcdef"),
                 self.hook(hook_event_name="PostToolUseFailure"), self.hook(hook_event_name="PreToolUse"),
                 self.hook(tool="Read"), self.hook(session_id=str(uuid4())), self.hook(is_error=True)]
        for event in cases:
            with self.subTest(event=event):
                self.assertEqual(self.invoke_hook(event)["hookSpecificOutput"]["additionalContext"], "existing context")
        for output in ({"continue": False}, {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "original"}}):
            self.assertEqual(self.invoke_hook(self.hook(), output), output)

    def test_missing_or_failed_discovery_never_blocks_hooks(self):
        with patch.object(native_addressing, "discover_peers", return_value=None):
            self.assertEqual(peer_lineage.peer_lineage()["status"], "unavailable")
            self.assertEqual(self.invoke_hook(self.hook())["hookSpecificOutput"]["additionalContext"], "existing context")
        with patch.object(peer_lineage, "peer_lineage", side_effect=OSError("optional metadata failure")):
            self.assertEqual(self.invoke_hook(self.hook())["hookSpecificOutput"]["additionalContext"], "existing context")

    def test_hook_metadata_is_bounded_and_points_to_exact_lookup(self):
        for _ in range(peer_lineage.MAX_PEERS + 1):
            self.context(windows=20, name="data\nnot an instruction " + "界" * 300)
        result = self.invoke_hook(self.hook())
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(text), peer_lineage.MAX_CONTEXT + len("existing context\n"))
        self.assertIn(peer_lineage.LOOKUP, text)

    def test_native_discovery_shared_helper_keeps_rows_and_rejects_bad_outputs(self):
        with patch.object(native_addressing.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(self.rows))) as run:
            self.assertEqual(self.real_discover(), self.rows)
        run.assert_called_once_with(["claude", "agents", "--json"], capture_output=True, text=True, timeout=5)
        for stdout in ("{", "null", "{}", '[{"name":"peer"},null]'):
            with self.subTest(stdout=stdout), patch.object(native_addressing.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=stdout)):
                self.assertIsNone(self.real_discover())
        with patch.object(native_addressing.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 5)):
            self.assertIsNone(self.real_discover())


if __name__ == "__main__":
    unittest.main()
