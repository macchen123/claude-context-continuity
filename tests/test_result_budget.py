"""原生结果有界替换的定向检查。"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import core  # noqa: E402
from claude_context_continuity.history import redact  # noqa: E402
from claude_context_continuity.result_budget import bound_result  # noqa: E402


def compact_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def bash(stdout: str, stderr: str = "", **metadata: object) -> dict[str, object]:
    result: dict[str, object] = {"stdout": stdout, "stderr": stderr, "interrupted": False}
    result.update(metadata)
    return result


def completed_agent(text: str) -> dict[str, object]:
    return {
        "agentId": "agent-42",
        "agentType": "general-purpose",
        "content": [
            {"type": "text", "text": text, "citations": [{"source": "retained"}]},
            {"type": "text", "text": text, "citations": None},
        ],
        "resolvedModel": "claude-test",
        "modelsUsed": ["claude-test"],
        "totalToolUseCount": 0,
        "totalDurationMs": 0,
        "totalTokens": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "server_tool_use": None,
            "service_tier": None,
            "cache_creation": None,
            "inference_geo": None,
            "speed": None,
            "iterations": {"kept": True},
            "output_tokens_details": {"thinking_tokens": None},
        },
        "toolStats": {
            "readCount": 0,
            "searchCount": 0,
            "bashCount": 0,
            "editFileCount": 0,
            "linesAdded": 0,
            "linesRemoved": 0,
            "otherToolCount": 0,
            "frameCount": 0,
        },
        "status": "completed",
        "prompt": "Keep this original instruction exactly unchanged.",
        "worktreePath": "/worktree",
        "worktreeBranch": "feature/result-budget",
    }


class ResultBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="result-budget-", dir=TESTS)
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.context = self.work / "private-context"
        self.context.mkdir()

    def test_bash_archives_redacted_large_unicode_source_with_verified_hash(self) -> None:
        secret = "result-budget-private-secret"
        source = ("汉字🙂" * 14_000) + " api_key=abc " + secret
        response = bash(source, "stderr " + source)
        budget = 1_600

        with patch.dict(os.environ, {"RESULT_BUDGET_SECRET": secret}, clear=False):
            result = bound_result("Bash", response, directory=self.context, byte_limit=budget)

        self.assertTrue(result["replaced"])
        self.assertFalse(result["defer_required"])
        self.assertGreater(result["raw_bytes"], budget)
        self.assertLessEqual(result["model_bytes"], budget)
        self.assertEqual(result["raw_bytes"], compact_bytes(response))
        self.assertEqual(result["model_bytes"], compact_bytes(result["response"]))

        reference = result["reference"]
        self.assertIsNotNone(reference)
        archive = Path(reference["path"])
        self.assertEqual(archive.parent, self.context / "outputs")
        self.assertTrue(archive.is_file())
        source_bytes = archive.read_bytes()
        self.assertGreater(len(source_bytes), core.MAX_PACKET)
        self.assertEqual(reference["bytes"], len(source_bytes))
        self.assertEqual(reference["sha256"], hashlib.sha256(source_bytes).hexdigest())
        self.assertEqual(reference["sha256"], core.sha(archive))
        self.assertEqual(archive.name, f"{reference['sha256']}.json")

        archived = json.loads(source_bytes.decode("utf-8"))
        archive_text = source_bytes.decode("utf-8")
        self.assertEqual(archived["stdout"], redact(source, (secret,)))
        self.assertEqual(archived["stderr"], redact("stderr " + source, (secret,)))
        self.assertEqual(archived["interrupted"], False)
        self.assertNotIn(secret, archive_text)
        self.assertNotIn("api_key=abc", archive_text)
        self.assertIn("[REDACTED]", archive_text)
        self.assertTrue(reference["redacted"])

    def test_bash_preserves_false_flags_background_handles_and_native_paths(self) -> None:
        persisted = "/native/tool-results/persisted-123.txt"
        response = bash(
            "stdout-" * 4_000,
            "stderr-" * 4_000,
            backgroundTaskId="background-123",
            backgroundedByUser=False,
            timedOutAfterMs=500,
            backgroundEndsWithFinalResponse=True,
            dangerouslyDisableSandbox=False,
            noOutputExpected=False,
            persistedOutputPath=persisted,
            persistedOutputSize=99_999,
            rawOutputPath="/native/tool-results/raw-123.txt",
            returnCodeInterpretation="non-error special result",
        )
        result = bound_result("Bash", response, directory=self.context, byte_limit=1_500)

        self.assertTrue(result["replaced"])
        self.assertFalse(result["defer_required"])
        replacement = result["response"]
        for key in (
            "interrupted",
            "backgroundTaskId",
            "backgroundedByUser",
            "timedOutAfterMs",
            "backgroundEndsWithFinalResponse",
            "dangerouslyDisableSandbox",
            "noOutputExpected",
            "persistedOutputPath",
            "persistedOutputSize",
            "rawOutputPath",
            "returnCodeInterpretation",
        ):
            with self.subTest(key=key):
                self.assertEqual(replacement[key], response[key])
        self.assertIn(result["reference"]["path"], replacement["stdout"])
        self.assertIn(result["reference"]["sha256"], replacement["stdout"])
        self.assertIn(result["reference"]["path"], replacement["stderr"])
        self.assertIn(result["reference"]["sha256"], replacement["stderr"])
        self.assertIn("do not rerun", replacement["stdout"].lower())
        self.assertIn("do not rerun", replacement["stderr"].lower())
        self.assertNotIn(persisted, replacement["stdout"])

    def test_text_read_keeps_file_metadata_and_reports_actual_preview_lines(self) -> None:
        content = ("第一行 α\n第二行 β\n") * 1_000
        response = {
            "type": "text",
            "file": {
                "filePath": "/workspace/unicode.txt",
                "content": content,
                "numLines": 2_000,
                "startLine": 7,
                "totalLines": 4_000,
                "truncatedByTokenCap": False,
            },
            "artifactRead": {"slug": "source", "ver": "v3"},
            "schema": "native-read-schema-kept",
        }
        result = bound_result("Read", response, directory=self.context, byte_limit=900)

        self.assertTrue(result["replaced"])
        self.assertFalse(result["defer_required"])
        self.assertLessEqual(result["model_bytes"], 900)
        replacement = result["response"]
        file = replacement["file"]
        self.assertEqual(file["filePath"], response["file"]["filePath"])
        self.assertEqual(file["startLine"], response["file"]["startLine"])
        self.assertEqual(file["totalLines"], response["file"]["totalLines"])
        self.assertEqual(replacement["artifactRead"], response["artifactRead"])
        self.assertEqual(replacement["schema"], response["schema"])
        self.assertTrue(file["truncatedByTokenCap"])
        self.assertLess(len(file["content"]), len(content))
        self.assertTrue(content.startswith(file["content"]))
        self.assertEqual(file["numLines"], len(file["content"].splitlines()))
        self.assertIsNotNone(result["reference"])

    def test_completed_agent_keeps_status_usage_prompt_and_citations(self) -> None:
        response = completed_agent("agent result " * 5_000)
        result = bound_result("Agent", response, directory=self.context, byte_limit=1_500)

        self.assertTrue(result["replaced"])
        self.assertFalse(result["defer_required"])
        self.assertLessEqual(result["model_bytes"], 1_500)
        replacement = result["response"]
        for key in (
            "agentId",
            "agentType",
            "resolvedModel",
            "modelsUsed",
            "totalToolUseCount",
            "totalDurationMs",
            "totalTokens",
            "usage",
            "toolStats",
            "status",
            "prompt",
            "worktreePath",
            "worktreeBranch",
        ):
            with self.subTest(key=key):
                self.assertEqual(replacement[key], response[key])
        self.assertEqual(replacement["status"], "completed")
        self.assertEqual(len(replacement["content"]), len(response["content"]))
        self.assertEqual(replacement["content"][0]["citations"], response["content"][0]["citations"])
        self.assertIsNone(replacement["content"][1]["citations"])
        self.assertIn(result["reference"]["path"], replacement["content"][-1]["text"])
        self.assertIn("do not rerun", replacement["content"][-1]["text"].lower())

    def test_unsupported_rich_result_and_too_small_budget_have_no_archive_side_effect(self) -> None:
        small_unknown = {"kind": "native-rich", "payload": "small"}
        small_unknown_result = bound_result("UnknownNativeTool", small_unknown, directory=self.context, byte_limit=1_000)
        self.assertFalse(small_unknown_result["replaced"])
        self.assertFalse(small_unknown_result["defer_required"])
        self.assertIs(small_unknown_result["response"], small_unknown)
        self.assertFalse((self.context / "outputs").exists())

        small_rich = bash("visual bytes", isImage=True)
        small_rich_result = bound_result("Bash", small_rich, directory=self.context, byte_limit=1_000)
        self.assertFalse(small_rich_result["replaced"])
        self.assertFalse(small_rich_result["defer_required"])
        self.assertIs(small_rich_result["response"], small_rich)
        self.assertFalse((self.context / "outputs").exists())

        rich = bash("visual bytes" * 1_000, isImage=True)
        rich_result = bound_result("Bash", rich, directory=self.context, byte_limit=1_000)
        self.assertFalse(rich_result["replaced"])
        self.assertTrue(rich_result["defer_required"])
        self.assertIs(rich_result["response"], rich)
        self.assertFalse((self.context / "outputs").exists())

        tiny = bash("stdout" * 2_000, "stderr" * 2_000)
        tiny_result = bound_result("Bash", tiny, directory=self.context, byte_limit=1)
        self.assertFalse(tiny_result["replaced"])
        self.assertTrue(tiny_result["defer_required"])
        self.assertIsNone(tiny_result["reference"])
        self.assertFalse((self.context / "outputs").exists())

        asynchronous = {
            "status": "async_launched",
            "isAsync": True,
            "agentId": "agent-still-running",
            "description": "do not rewrite this task control result",
            "prompt": "original task instructions",
            "outputFile": "/native/agent-output.txt",
            "canReadOutputFile": False,
        }
        asynchronous_result = bound_result("Agent", asynchronous, directory=self.context, byte_limit=100)
        self.assertFalse(asynchronous_result["replaced"])
        self.assertTrue(asynchronous_result["defer_required"])
        self.assertIs(asynchronous_result["response"], asynchronous)
        self.assertFalse((self.context / "outputs").exists())

    def test_huge_preserved_metadata_defers_instead_of_mutating_its_semantics(self) -> None:
        response = bash(
            "stdout" * 2_000,
            "stderr" * 2_000,
            staleReadFileStateHint="metadata-must-not-be-truncated" * 500,
        )
        result = bound_result("Bash", response, directory=self.context, byte_limit=1_000)

        self.assertFalse(result["replaced"])
        self.assertTrue(result["defer_required"])
        self.assertIs(result["response"], response)
        self.assertEqual(result["response"]["staleReadFileStateHint"], response["staleReadFileStateHint"])
        self.assertFalse((self.context / "outputs").exists())

    def test_duplicate_identical_archive_is_reused_without_rewrite(self) -> None:
        response = bash("same-output-" * 8_000, "same-error-" * 8_000)
        first = bound_result("Bash", response, directory=self.context, byte_limit=1_400)
        archive = Path(first["reference"]["path"])
        original_bytes = archive.read_bytes()
        original_stat = archive.stat()

        second = bound_result("Bash", response, directory=self.context, byte_limit=1_400)

        self.assertTrue(first["replaced"])
        self.assertTrue(second["replaced"])
        self.assertEqual(first["reference"], second["reference"])
        self.assertEqual(archive.read_bytes(), original_bytes)
        self.assertEqual(archive.stat().st_ino, original_stat.st_ino)
        self.assertEqual(list((self.context / "outputs").glob("*.json")), [archive])

    def test_existing_archive_hash_drift_defers_without_overwrite(self) -> None:
        response = bash("binding-output-" * 4_000, "binding-error-" * 4_000)
        first = bound_result("Bash", response, directory=self.context, byte_limit=1_300)
        archive = Path(first["reference"]["path"])
        archive.write_text('{"unexpected":"replacement"}\n', encoding="utf-8")
        changed = archive.read_bytes()

        second = bound_result("Bash", response, directory=self.context, byte_limit=1_300)

        self.assertFalse(second["replaced"])
        self.assertTrue(second["defer_required"])
        self.assertIsNone(second["reference"])
        self.assertEqual(archive.read_bytes(), changed)

    def test_symlinked_output_boundary_is_denied_without_following_it(self) -> None:
        outside = self.work / "outside"
        outside.mkdir()
        (self.context / "outputs").symlink_to(outside, target_is_directory=True)
        response = bash("stdout" * 4_000, "stderr" * 4_000)

        result = bound_result("Bash", response, directory=self.context, byte_limit=1_300)

        self.assertFalse(result["replaced"])
        self.assertTrue(result["defer_required"])
        self.assertTrue((self.context / "outputs").is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_non_integer_or_nonfinite_budget_defers_without_writing(self) -> None:
        response = bash("stdout" * 2_000, "stderr" * 2_000)
        for budget in (True, False, 0, -1, 100.0, math.inf, math.nan):
            with self.subTest(budget=budget):
                result = bound_result("Bash", response, directory=self.context, byte_limit=budget)
                self.assertFalse(result["replaced"])
                self.assertTrue(result["defer_required"])
                self.assertIsNone(result["reference"])
                self.assertFalse((self.context / "outputs").exists())


if __name__ == "__main__":
    unittest.main()
