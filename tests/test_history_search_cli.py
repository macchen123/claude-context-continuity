"""跨窗口命令、原文读回和后续阶段回查的本地集成检查。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import continuity, core
from claude_context_continuity.context_runtime import context_prompt


class HistorySearchCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="history-cli-", dir=ROOT / "tests")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "state"
        self.context_id = str(uuid4())
        self.sources = []
        self.patch_home = patch.object(core, "HOME", self.home)
        self.patch_home.start()
        self.addCleanup(self.patch_home.stop)

    def source(self, generation, text, *, role="user", tool=None):
        sid = str(uuid4())
        path = self.root / f"{sid}.jsonl"
        records = []
        if tool:
            records.append({"uuid": f"ask-{generation}", "type": "assistant", "sessionId": sid,
                            "message": {"role": "assistant", "content": [
                                {"type": "tool_use", "name": tool, "id": f"call-{generation}",
                                 "input": {"command": "not-indexed-command"}}]}})
            content = [{"type": "tool_result", "tool_use_id": f"call-{generation}", "content": text}]
            role = "user"
        else:
            content = text
        records.append({"uuid": f"message-{generation}", "type": role, "sessionId": sid,
                        "timestamp": "2026-09-07T12:00:00Z", "message": {"role": role, "content": content}})
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        binding = {"source_path": str(path), "session_id": sid, "generation": generation}
        core.atomic(self.home / "runtime" / "contexts" / self.context_id / "history" /
                    f"{generation:08d}-{sid}.json", binding, exclusive=True)
        self.sources.append(binding)
        return binding

    @staticmethod
    def dispatch(*arguments):
        return continuity.dispatch(continuity.parser().parse_args(arguments))

    def test_context_search_filter_order_paging_and_bound_read(self):
        self.source(0, "接力 decision 原始约束")
        self.source(1, "接力 decision 后续回复", role="assistant")
        self.source(2, "接力 decision 工具证据", tool="Bash")
        args = ("history-search", "--context-id", self.context_id, "--query", "接力",
                "--recent-first", "--page-size", "1")
        first = self.dispatch(*args)
        self.assertEqual(first["sqlite_version"], "3.53.4")
        self.assertEqual(first["entries"][0]["generation"], 2)
        self.assertFalse(first["write_authority"])
        second = self.dispatch(*args, "--cursor", first["next_cursor"])
        self.assertEqual(second["entries"][0]["generation"], 1)
        users = self.dispatch("history-search", "--context-id", self.context_id,
                              "--query", "decision", "--role", "user")
        self.assertEqual([e["generation"] for e in users["entries"]], [0])
        tools = self.dispatch("history-search", "--context-id", self.context_id, "--tool", "Bash")
        self.assertEqual([e["generation"] for e in tools["entries"]], [2])
        locator = users["entries"][0]["locator"]
        read = self.dispatch("history", "--source", locator["source_path"], "--session-id", locator["session_id"],
                             "--message-id", locator["message_id"], "--expected-sha256", locator["sha256"])
        self.assertIn("原始约束", read["text"])
        self.assertEqual(read["locator"], locator)
        Path(locator["source_path"]).write_text(Path(locator["source_path"]).read_text().replace("原始约束", "已变内容"))
        with self.assertRaisesRegex(core.ContinuityError, "SHA256"):
            self.dispatch("history", "--source", locator["source_path"], "--session-id", locator["session_id"],
                          "--message-id", locator["message_id"], "--expected-sha256", locator["sha256"])

    def test_all_catalogue_pages_and_scope_filters(self):
        for generation in range(23):
            self.source(generation, f"跨窗口 marker {generation}")
        result = self.dispatch("history-search", "--context-id", self.context_id,
                               "--query", "marker 22", "--window", "22")
        self.assertEqual([e["generation"] for e in result["entries"]], [22])
        first = self.sources[0]
        result = self.dispatch("history-search", "--context-id", self.context_id,
                               "--session", first["session_id"])
        self.assertEqual([e["generation"] for e in result["entries"]], [0])
        with patch.dict(os.environ, {"CLAUDE_CONTINUITY_ID": self.context_id}):
            result = self.dispatch("history-search", "--query", "marker 21")
            self.assertEqual([e["generation"] for e in result["entries"]], [21])

    def test_explicit_source_does_not_use_inherited_context(self):
        first = self.source(0, "接力 single source")
        self.source(1, "接力 must not appear")
        with patch.dict(os.environ, {"CLAUDE_CONTINUITY_ID": "invalid-inherited-context"}):
            result = self.dispatch("history-search", "--source", first["source_path"],
                                   "--session-id", first["session_id"], "--query", "接力")
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["coverage"], "explicit_history_source")
        self.assertEqual(result["entries"][0]["locator"]["session_id"], first["session_id"])

    def test_missing_scope_and_invalid_bounds_fail_explicitly(self):
        first = self.source(0, "original")
        invalid = [
            ("--source", first["source_path"]),
            ("--session-id", first["session_id"]),
            ("--context-id", self.context_id, "--window", "-1"),
            ("--context-id", self.context_id, "--page-size", "21"),
            ("--context-id", self.context_id, "--query", ""),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.dispatch("history-search", *args)
        with patch.dict(os.environ, {"CLAUDE_CONTINUITY_ID": ""}):
            with self.assertRaisesRegex(core.ContinuityError, "context-id"):
                self.dispatch("history-search")

    def test_context_prompt_exposes_later_turn_search_and_exact_read(self):
        prompt = context_prompt()
        for text in ("整个任务期间", "history-search", "--role user", "--cursor", "--expected-sha256"):
            self.assertIn(text, prompt)

    def test_later_process_finds_old_constraint_without_replaying_producer(self):
        self.source(0, "后续导出文件名必须是 delivery.json，不要重新运行 producer。")
        self.source(1, "先完成准备步骤，再按旧约束导出。", role="assistant")
        producer = self.root / "producer-result.json"
        producer.write_text(json.dumps({"value": 42}))
        before = (hashlib.sha256(producer.read_bytes()).hexdigest(), producer.stat().st_mtime_ns)
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
                       "CLAUDE_CONTEXT_CONTINUITY_DIR": str(self.home)}
        environment.pop("CLAUDE_CONTINUITY_ID", None)

        def invoke(*arguments):
            run = subprocess.run([sys.executable, "-B", "-m", "claude_context_continuity", *arguments],
                                 cwd=self.root, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            return json.loads(run.stdout)

        invoke("history-windows", "--context-id", self.context_id)
        prepared = self.root / "prepared.json"
        prepared.write_text(json.dumps({"ready": True}))
        found = invoke("history-search", "--context-id", self.context_id, "--query", "导出文件名", "--role", "user")
        locator = found["entries"][0]["locator"]
        read = invoke("history", "--source", locator["source_path"], "--session-id", locator["session_id"],
                      "--message-id", locator["message_id"], "--expected-sha256", locator["sha256"])
        self.assertIn("delivery.json", read["text"])
        self.assertTrue(json.loads(prepared.read_text())["ready"])
        (self.root / "delivery.json").write_text(producer.read_text())
        self.assertEqual(json.loads((self.root / "delivery.json").read_text()), {"value": 42})
        self.assertEqual(before, (hashlib.sha256(producer.read_bytes()).hexdigest(), producer.stat().st_mtime_ns))


if __name__ == "__main__":
    unittest.main()
