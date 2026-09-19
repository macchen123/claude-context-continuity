"""Regression coverage for per-hook history snapshot reuse."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import core  # noqa: E402
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
