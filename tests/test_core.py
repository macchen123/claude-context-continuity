"""共享持久化、路径和窗口读取的定向检查。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import core  # noqa: E402


class CoreUtilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=TESTS)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def test_atomic_round_trip_replaces_only_after_an_exclusive_create(self) -> None:
        path = self.root / "runtime" / "state.json"
        initial = {"phase": "running", "generation": 0}
        core.atomic(path, initial, exclusive=True)
        self.assertEqual(core.read_json(path), initial)

        with self.assertRaises(FileExistsError):
            core.atomic(path, {"phase": "replacement"}, exclusive=True)
        self.assertEqual(core.read_json(path), initial)

        updated = {"phase": "paused", "generation": 1}
        core.atomic(path, updated)
        self.assertEqual(core.read_json(path), updated)

    def test_atomic_skip_unchanged_avoids_fsync_but_persists_changes(self) -> None:
        path = self.root / "state.json"
        initial = {"phase": "running"}
        core.atomic(path, initial, skip_unchanged=True)
        before = path.stat()
        with patch.object(core.os, "fsync", wraps=core.os.fsync) as sync:
            core.atomic(path, initial, skip_unchanged=True)
            sync.assert_not_called()
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
            core.atomic(path, {"phase": "paused"}, skip_unchanged=True)
            self.assertEqual(sync.call_count, 2)
        self.assertEqual(core.read_json(path), {"phase": "paused"})
        with self.assertRaises(FileExistsError):
            core.atomic(path, {"phase": "paused"}, exclusive=True, skip_unchanged=True)
        alias = self.root / "alias.json"
        alias.symlink_to(path)
        with self.assertRaises(core.ContinuityError):
            core.atomic(alias, {"phase": "paused"}, skip_unchanged=True)

    def test_busy_lock_is_distinct_and_does_not_transfer_ownership(self) -> None:
        path = self.root / ".lock"
        with core.lock(path):
            with self.assertRaises(core.LockBusy):
                with core.lock(path):
                    self.fail("a second owner acquired the held lock")
        with core.lock(path):
            pass
        self.assertTrue(issubclass(core.LockBusy, core.ContinuityError))

    def test_atomic_and_safe_path_reject_symlink_aliases(self) -> None:
        regular = self.workspace / "regular.txt"
        regular.write_text("bound bytes", encoding="utf-8")
        alias = self.workspace / "alias.txt"
        alias.symlink_to(regular)

        with self.assertRaises(core.ContinuityError):
            core.safe_path(self.workspace, "alias.txt")

        state = self.root / "state.json"
        state.write_text('{"state":"original"}\n', encoding="utf-8")
        state_alias = self.root / "state-alias.json"
        state_alias.symlink_to(state)
        with self.assertRaises(core.ContinuityError):
            core.atomic(state_alias, {"state": "replacement"})
        self.assertEqual(core.read_json(state), {"state": "original"})

    def test_safe_path_stays_within_the_declared_root(self) -> None:
        tracked = self.workspace / "tracked.txt"
        tracked.write_text("tracked", encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")

        self.assertEqual(core.safe_path(self.workspace, "tracked.txt"), tracked.resolve())
        self.assertEqual(core.safe_path(self.workspace, "future.txt", exists=False), self.workspace / "future.txt")
        with self.assertRaises(core.ContinuityError):
            core.safe_path(self.workspace, outside)

    def test_read_json_enforces_the_bounded_packet_size(self) -> None:
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b"x" * (core.MAX_PACKET + 1))
        with self.assertRaisesRegex(core.ContinuityError, "64 KiB"):
            core.read_json(oversized)

    def test_configured_window_accepts_only_positive_exact_values(self) -> None:
        self.assertEqual(core.configured_window({"env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}), 1000)
        self.assertEqual(core.configured_window({"env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": 2000}}), 2000)
        for value in (None, False, 0, -1, "0", "01x", " 1000", 1.5):
            with self.subTest(value=value):
                self.assertIsNone(core.configured_window({"env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": value}}))


if __name__ == "__main__":
    unittest.main()
