"""Regression coverage for per-hook history snapshot reuse."""
from __future__ import annotations

from contextlib import redirect_stdout
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

from claude_context_continuity import continuity, core  # noqa: E402
from claude_context_continuity.history import HistorySource  # noqa: E402
from claude_context_continuity.tui_runtime import TuiRuntime  # noqa: E402


class HookHistorySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="hook-history-snapshot-", dir=Path(__file__).parent)
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        home = patch.object(core, "HOME", self.home)
        home.start()
        self.addCleanup(home.stop)
        self.config = {"hash": "fixed-config", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        self.session_id = str(uuid4())
        self.runtime = TuiRuntime.create(
            cwd=self.cwd,
            session_id=self.session_id,
            configuration=self.config,
            configuration_reader=lambda _: self.config,
        )
        self.source = self.root / f"{self.session_id}.jsonl"
        self.instruction_id = str(uuid4())
        self.source.write_text("\n".join(json.dumps(record) for record in (
            {
                "type": "user",
                "uuid": self.instruction_id,
                "sessionId": self.session_id,
                "message": {"role": "user", "content": "Continue the already authorized task."},
            },
            {
                "type": "assistant",
                "uuid": str(uuid4()),
                "sessionId": self.session_id,
                "cwd": str(self.cwd),
                "message": {
                    "role": "assistant",
                    "content": "Native response.",
                    "model": "fixture-native-model",
                    "usage": {
                        "input_tokens": 125,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
        )) + "\n", encoding="utf-8")

    def hook(self, name: str, **fields: object) -> dict[str, object]:
        return {
            "hook_event_name": name,
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "transcript_path": str(self.source),
            **fields,
        }

    def test_hook_without_usage_keeps_observing_without_a_second_parse(self) -> None:
        first = self.source.read_text(encoding="utf-8").splitlines()[0]
        self.source.write_text(first + "\n", encoding="utf-8")
        self.runtime.on_hook(self.hook("SessionStart", source="startup"))
        original = HistorySource._records
        with patch.object(HistorySource, "_records", autospec=True, side_effect=original) as reads:
            self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="first-call"))
        state = self.runtime._state()
        self.assertEqual(state["phase"], "running")
        self.assertIsNone(state["usage"])
        self.assertEqual(state["pending_tool_ids"], ["first-call"])
        self.assertEqual(reads.call_count, 1)

    def test_first_sample_pending_clears_recovered_observation_fault(self) -> None:
        first = self.source.read_text(encoding="utf-8").splitlines()[0]
        self.source.write_text(first + "\n", encoding="utf-8")
        self.runtime.on_hook(self.hook("SessionStart", source="startup"))
        for name in ("UserPromptSubmit", "PreToolUse"):
            with self.subTest(event=name):
                self.runtime._wait_external("hook_observation_failed")
                result = self.runtime.on_hook(self.hook(name, prompt="Continue.", tool_use_id="first-call"))
                state = self.runtime._state()
                self.assertIsNone(state.get("reconciliation"))
                self.assertIsNone(state["usage"])
                self.assertEqual(state["budget"], {})
                self.assertIsNone(state["rotation"]["request"])
                self.assertNotIn("systemMessage", result)

    def test_recovered_hook_does_not_emit_stale_wait_context(self) -> None:
        self.runtime.on_hook(self.hook("SessionStart", source="startup"))
        self.runtime._wait_external("hook_observation_failed")
        result = self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="recovered-call"))
        self.assertIsNone(self.runtime._state().get("reconciliation"))
        self.assertNotIn("waiting for exact native confirmation", result["hookSpecificOutput"]["additionalContext"])

    def test_invalid_usage_is_a_real_error_not_a_missing_sample(self) -> None:
        records = [json.loads(line) for line in self.source.read_text().splitlines()]
        records[-1]["message"]["usage"]["input_tokens"] = "invalid"
        self.source.write_text("".join(json.dumps(row) + "\n" for row in records))
        result = self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="invalid-sample"))
        self.assertIn("systemMessage", result)
        self.assertIsNotNone(self.runtime._state().get("diagnostic"))
        self.assertIsNone(self.runtime._state()["usage"])

    def _cli_failure(self, error):
        with patch.object(sys, "argv", ["claude-context", "tui-hook", "--context-id", self.runtime.conversation_id]), \
                patch.dict(os.environ, {"CLAUDE_CONTINUITY_ID": self.runtime.conversation_id}), \
                patch.object(continuity, "dispatch", side_effect=error), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(continuity.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {})

    def test_cli_lock_contention_is_logged_without_poisoning_runtime_state(self) -> None:
        before = self.runtime.state_path.read_bytes()
        self._cli_failure(core.LockBusy("fixture lock contention"))
        self.assertEqual(self.runtime.state_path.read_bytes(), before)
        events = [json.loads(line) for line in (self.runtime.directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "hook_failure")
        self.assertEqual(events[-1]["error_type"], "LockBusy")

    def test_cli_failure_keeps_redacted_cause_after_recovery(self) -> None:
        self.runtime.on_hook(self.hook("SessionStart", source="startup"))
        secret = "fixture-private-credential-123456"
        with patch.dict(os.environ, {"HOOK_TEST_API_KEY": secret}):
            self._cli_failure(OSError(f"fixture history read failed: {secret}"))
        state = self.runtime._state()
        self.assertIn("OSError", state.get("diagnostic", ""))
        self.assertIn("fixture history read failed", state["diagnostic"])
        self.assertNotIn(secret, state["diagnostic"])
        events = [json.loads(line) for line in (self.runtime.directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["error_type"], "OSError")
        self.assertNotIn(secret, json.dumps(events))
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="recovery-call"))
        recovered = self.runtime._state()
        self.assertIsNone(recovered.get("reconciliation"))
        self.assertEqual(recovered["last_reconciliation"]["diagnostic"], state["diagnostic"])

    def test_tool_failure_hook_accepts_native_extension_metadata(self) -> None:
        self.runtime.on_hook(self.hook("SessionStart", source="startup"))
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="fixture-call", tool_name="Bash"))
        with self.source.open("a", encoding="utf-8") as stream:
            for kind in ("tools-used", "tools-used", "future-native-state"):
                stream.write(json.dumps({"type": kind, "sessionId": self.session_id, "tools": ["Bash"]}) + "\n")
        result = self.runtime.on_hook(self.hook(
            "PostToolUseFailure", tool_use_id="fixture-call", tool_name="Bash", error="fixture failure"))
        state = self.runtime._state()
        self.assertIsNone(state.get("reconciliation"))
        self.assertNotIn("systemMessage", result)
        self.assertEqual(state["usage"]["total_input_and_cache_tokens"], 125)
        self.assertEqual(state["pending_tool_ids"], [])
        self.assertEqual(state["authorization"]["root_instruction_locator"]["message_id"], self.instruction_id)

    def test_user_prompt_reuses_bound_records_for_usage(self) -> None:
        self.runtime.on_hook(self.hook("SessionStart", source="startup"))
        original = HistorySource._records
        with patch.object(HistorySource, "_records", autospec=True, side_effect=original) as reads:
            self.runtime.on_hook(self.hook("UserPromptSubmit", prompt="Continue the already authorized task."))
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(self.runtime._state()["usage"]["total_input_and_cache_tokens"], 125)

    def test_automatic_handoff_uses_one_history_snapshot(self) -> None:
        self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="fixture-call"))
        state = self.runtime._state()
        state["budget_handoff_signal"] = {"generation": 0}
        original = HistorySource._records
        with patch.object(HistorySource, "_records", autospec=True, side_effect=original) as reads:
            self.runtime._automatic_rotation(state)
        self.assertEqual(reads.call_count, 1)
        handoff = state["rotation"]["request"]["handoff"]
        self.assertIn('"source_kind":"assistant"', handoff)

    def test_deferred_input_locators_reuse_verified_records(self) -> None:
        state = self.runtime._state()
        state["deferred_inputs"] = [{
            "source_path": str(self.source), "session_id": self.session_id, "after_byte": 0,
            "prompt_hash": core.digest("Continue the already authorized task."),
        }]
        original = HistorySource._records
        with patch.object(HistorySource, "_records", autospec=True, side_effect=original) as reads:
            locators = self.runtime._deferred_input_locators(state)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(locators[0]["message_id"], self.instruction_id)
        self.assertEqual(locators[0]["source_kind"], "original_user")

    def test_parse_error_notice_does_not_promise_automatic_recovery(self) -> None:
        with self.source.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"user"}\n')
        result = self.runtime.on_hook(self.hook("PreToolUse", tool_use_id="fixture-call"))
        self.assertIn("record has no usable message content", result["systemMessage"])
        self.assertNotIn("确认后自动继续", result["systemMessage"])

    def test_active_tool_hooks_parse_once_and_share_binding_with_usage(self) -> None:
        events = (
            self.hook("PreToolUse", tool_use_id="fixture-call"),
            self.hook("PostToolUse", tool_use_id="fixture-call"),
            self.hook("PostToolBatch", tool_calls=[]),
        )
        original = HistorySource._records
        reads: list[Path] = []

        def counted(source: HistorySource, *args: object, **kwargs: object):
            reads.append(source.path)
            return original(source, *args, **kwargs)

        with patch.object(HistorySource, "_records", autospec=True, side_effect=counted):
            for event in events:
                before = len(reads)
                result = self.runtime.on_hook(event)
                self.assertIn("hookSpecificOutput", result)
                self.assertEqual(len(reads) - before, 1)

        state = self.runtime._state()
        self.assertEqual(len(reads), len(events))
        self.assertEqual(state["authorization"]["root_instruction_locator"]["message_id"], self.instruction_id)
        self.assertEqual(state["usage"]["total_input_and_cache_tokens"], 125)


if __name__ == "__main__":
    unittest.main()
