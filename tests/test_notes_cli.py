from io import BytesIO, TextIOWrapper
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import continuity, core


class NotesCliTests(unittest.TestCase):
    def test_cli_reuses_current_context_without_task_registration(self):
        sid = "123e4567-e89b-12d3-a456-426614174000"
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp, \
                patch.object(core, "HOME", Path(tmp)), \
                patch.dict("os.environ", {"CLAUDE_CONTINUITY_ID": sid}):
            def invoke(*argv, text=""):
                args = continuity.parser().parse_args(["notes", *argv])
                with patch.object(sys, "stdin", TextIOWrapper(BytesIO(text.encode("utf-8")))):
                    return continuity.dispatch(args)

            first = invoke("write", "work", text="统一上下文笔记")
            second = invoke("append", "work", "--expected-sha256", first["sha256"], text="\n继续原任务")
            self.assertEqual(invoke("read", "work")["text"], "统一上下文笔记\n继续原任务")
            self.assertEqual(invoke("read", "work", "--revision", first["revision"])["text"], "统一上下文笔记")
            self.assertEqual(invoke("list")["entries"][0]["sha256"], second["sha256"])
            self.assertEqual(invoke("search", "--query", "原任务")["entries"][0]["name"], "work")
            self.assertEqual(second["context_id"], sid)
            self.assertTrue(second["non_authoritative"])
            self.assertFalse((Path(tmp) / "runtime" / "interactive").exists())
            self.assertEqual(list(Path(tmp).rglob("watch.json")), [])
            with self.assertRaises(ValueError):
                invoke("write", "work", "--expected-sha256", first["sha256"], text="stale")


if __name__ == "__main__":
    unittest.main()
