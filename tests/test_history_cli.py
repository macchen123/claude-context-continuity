from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import continuity, core  # noqa: E402


class HistoryCliTests(unittest.TestCase):
    def test_windows_are_paged_from_runtime_source_bindings(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
            home = Path(tmp)
            context_id, first_session, second_session = str(uuid4()), str(uuid4()), str(uuid4())
            history = home / "runtime" / "contexts" / context_id / "history"
            first = {
                "session_id": first_session,
                "source_path": "/not-read/by-window-list.jsonl",
                "generation": 0,
            }
            second = {
                "session_id": second_session,
                "source_path": "/not-read/by-window-list-second.jsonl",
                "generation": 1,
            }
            core.atomic(history / f"00000000-{first_session}.json", first, exclusive=True)
            core.atomic(history / f"00000001-{second_session}.json", second, exclusive=True)

            with patch.object(core, "HOME", home):
                first_args = continuity.parser().parse_args([
                    "history-windows", "--context-id", context_id, "--offset", "0", "--limit", "1",
                ])
                first_page = continuity.dispatch(first_args)
                self.assertEqual(first_page["context_id"], context_id)
                self.assertEqual(first_page["entries"], [first])
                self.assertEqual(first_page["next_offset"], 1)
                self.assertFalse(first_page["write_authority"])

                second_args = continuity.parser().parse_args([
                    "history-windows", "--context-id", context_id, "--offset", "1", "--limit", "1",
                ])
                self.assertEqual(continuity.dispatch(second_args)["entries"], [second])
                self.assertIsNone(continuity.dispatch(second_args)["next_offset"])

    def test_search_is_dispatched_with_explicit_bounds(self) -> None:
        from claude_context_continuity.history import HistorySource

        sid = str(uuid4())
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
            path = Path(tmp) / f"{sid}.jsonl"
            path.write_text("", encoding="utf-8")
            args = continuity.parser().parse_args([
                "history", "--source", str(path), "--session-id", sid,
                "--search", "query", "--offset", "0", "--page-size", "2", "--scan-limit", "5",
            ])
            with patch.object(HistorySource, "search", return_value={"entries": []}) as search:
                self.assertEqual(continuity.dispatch(args), {"entries": []})
                self.assertEqual(search.call_args.args, ("query",))
                self.assertEqual(search.call_args.kwargs["scan_limit"], 5)
                self.assertEqual(search.call_args.kwargs["limit"], 2)


if __name__ == "__main__":
    unittest.main()
