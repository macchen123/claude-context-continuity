from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import apsw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity.history import HistoryError, HistorySource  # noqa: E402
from claude_context_continuity.history_index import (  # noqa: E402
    HistoryIndex,
    HistoryIndexCacheError,
    HistoryIndexError,
    HistoryIndexParserError,
    HistoryIndexStaleError,
)


class HistoryIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="history-index-", dir=ROOT / "tests")
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    @staticmethod
    def line(value: dict[str, object]) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

    def record(
        self,
        session_id: str,
        message_id: str,
        record_type: str,
        content: object = "",
        **extra: object,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "uuid": message_id,
            "type": record_type,
            "sessionId": session_id,
            "timestamp": "2026-09-07T12:00:00.000Z",
        }
        if record_type in {"user", "assistant"}:
            value["message"] = {
                "role": record_type,
                "content": content,
            }
        value.update(extra)
        return value

    def source(self, generation: int, *records: dict[str, object]) -> tuple[dict[str, object], Path, str]:
        session_id = str(uuid4())
        path = self.work / f"{session_id}.jsonl"
        corrected: list[dict[str, object]] = []
        for record in records:
            copied = dict(record)
            copied["sessionId"] = session_id
            corrected.append(copied)
        path.write_bytes(b"".join(self.line(record) for record in corrected))
        return ({"source_path": str(path), "session_id": session_id, "generation": generation}, path, session_id)

    def append(self, path: Path, value: dict[str, object]) -> None:
        with path.open("ab") as handle:
            handle.write(self.line(value))

    @staticmethod
    def ids(result: dict[str, object]) -> list[str]:
        return [entry["locator"]["message_id"] for entry in result["entries"]]  # type: ignore[index]

    def test_cross_window_browse_filters_order_pagination_and_stale_cursor(self) -> None:
        first, first_path, first_id = self.source(0, self.record("", "first", "user", "needle window zero"))
        second, _, second_id = self.source(1, self.record("", "second", "assistant", "needle window one"))
        third, _, _ = self.source(2, self.record("", "third", "user", "needle window two"))
        sources = [third, first, second]

        with HistoryIndex(self.work / "cache" / "history.sqlite") as index:
            cold = index.search(sources, None, limit=1)
            self.assertEqual(cold["sqlite_version"], apsw.sqlitelibversion())
            self.assertGreaterEqual(tuple(map(int, cold["sqlite_version"].split("."))), (3, 53, 4))
            self.assertEqual(self.ids(cold), ["first"])
            self.assertEqual(cold["refresh"]["parsed_sources"], 3)

            hot = index.search(sources, None, limit=1, cursor=cold["next_cursor"])
            self.assertEqual(self.ids(hot), ["second"])
            self.assertEqual(hot["refresh"]["parsed_sources"], 0)
            self.assertEqual(hot["refresh"]["unchanged_sources"], 3)

            recent = index.search(sources, None, recent_first=True)
            self.assertEqual(self.ids(recent), ["third", "second", "first"])
            self.assertEqual(self.ids(index.search(sources, "needle", windows=(1,))), ["second"])
            other_window = index.search(sources, "needle", windows=(2,))
            self.assertEqual(self.ids(other_window), ["third"])
            self.assertEqual(other_window["refresh"]["parsed_sources"], 0)
            self.assertEqual(self.ids(index.search(sources, "needle", sessions=(first_id,))), ["first"])
            self.assertEqual(
                self.ids(index.search(sources, "needle", source_kinds=("assistant",))),
                ["second"],
            )

            page = index.search(sources, "needle", limit=1)
            self.append(first_path, self.record(first_id, "appended", "user", "needle appended"))
            with self.assertRaises(HistoryIndexStaleError):
                index.search(sources, "needle", limit=1, cursor=page["next_cursor"])

    def test_literal_search_matches_canonical_projection_for_unicode_and_punctuation(self) -> None:
        first, first_path, first_id = self.source(
            0,
            self.record("", "unicode-first", "user", '甲中文测试 Straße quote" % _ punctuation?!, a\x00b'),
        )
        second, _, second_id = self.source(
            1,
            self.record("", "unicode-second", "assistant", "乙中文测 STRASSE quote\" % _ punctuation?!, a\x00b"),
        )
        # source() rewrites identities, so compute the canonical projection only
        # after the fixture files have their final session bindings.
        sources = [first, second]
        queries = ("甲", "中文", "中文测", "strasse", 'quote"', "% _", "?!,", "\x00")
        expected: dict[str, list[str]] = {}
        for spec, session_id in ((first, first_id), (second, second_id)):
            projection = HistorySource(Path(spec["source_path"]), session_id).index_projection()["entries"]
            for query in queries:
                expected.setdefault(query, []).extend(
                    item["locator"]["message_id"]
                    for item in projection
                    if query.casefold() in item["text"].casefold()
                )

        with HistoryIndex(self.work / "literal.sqlite") as index:
            for query in queries:
                with self.subTest(query=repr(query)):
                    actual = self.ids(index.search(sources, query))
                    self.assertEqual(actual, expected[query])

    def test_append_refreshes_only_changed_source_and_unchanged_source_is_not_reparsed(self) -> None:
        spec, path, session_id = self.source(0, self.record("", "before", "user", "before marker"))
        with HistoryIndex(self.work / "incremental.sqlite") as index:
            index.search([spec], "before")
            original = HistorySource.index_projection
            calls: list[str] = []

            def observed(source: HistorySource, *args: object, **kwargs: object) -> dict[str, object]:
                calls.append(str(source.path))
                return original(source, *args, **kwargs)

            with patch.object(HistorySource, "index_projection", observed):
                hot = index.search([spec], "before")
            self.assertEqual(calls, [])
            self.assertEqual(hot["refresh"]["unchanged_sources"], 1)

            self.append(path, self.record(session_id, "after", "user", "after marker"))
            with patch.object(HistorySource, "index_projection", observed):
                appended = index.search([spec], "after")
            self.assertEqual(calls, [str(path)])
            self.assertEqual(self.ids(appended), ["after"])
            self.assertEqual(appended["refresh"]["parsed_sources"], 1)
            self.assertEqual(appended["refresh"]["added_records"], 1)

    def test_same_size_rewrite_truncation_and_replacement_refresh_correct_bindings(self) -> None:
        spec, path, session_id = self.source(0, self.record("", "same", "user", "same-size-old"))
        with HistoryIndex(self.work / "mutations.sqlite") as index:
            index.search([spec], "same-size-old")
            replacement = self.record(session_id, "same", "user", "same-size-new")
            old_bytes = path.read_bytes()
            new_bytes = self.line(replacement)
            self.assertEqual(len(old_bytes), len(new_bytes))
            path.write_bytes(new_bytes)
            rewritten = index.search([spec], "same-size-new")
            self.assertEqual(self.ids(rewritten), ["same"])
            self.assertEqual(rewritten["refresh"]["changed_records"], 1)
            self.assertEqual(self.ids(index.search([spec], "same-size-old")), [])

            long_one = self.record(session_id, "long-one", "user", "long marker " + "x" * 300)
            long_two = self.record(session_id, "long-two", "user", "second marker " + "y" * 300)
            path.write_bytes(self.line(long_one) + self.line(long_two))
            index.search([spec], "long marker")
            short = self.record(session_id, "short", "user", "short replacement")
            path.write_bytes(self.line(short))
            truncated = index.search([spec], "short replacement")
            self.assertEqual(self.ids(truncated), ["short"])
            self.assertGreaterEqual(truncated["refresh"]["rebuilt_sources"], 1)

            external = self.work / "external-replacement.jsonl"
            external.write_bytes(self.line(self.record(session_id, "replaced", "user", "inode replacement")))
            os.replace(external, path)
            replaced = index.search([spec], "inode replacement")
            self.assertEqual(self.ids(replaced), ["replaced"])
            self.assertGreaterEqual(replaced["refresh"]["rebuilt_sources"], 1)

    def test_incomplete_tail_is_deferred_until_completed_and_bound_old_record_reads(self) -> None:
        spec, path, session_id = self.source(0, self.record("", "complete", "user", "complete marker"))
        tail = self.record(session_id, "tail", "user", "tail marker")
        partial = self.line(tail)[:-1]
        with HistoryIndex(self.work / "tail.sqlite") as index:
            initial = index.search([spec], "complete marker")
            locator = initial["entries"][0]["locator"]
            with path.open("ab") as handle:
                handle.write(partial)
            deferred = index.search([spec], "complete marker")
            self.assertEqual(self.ids(deferred), ["complete"])
            self.assertEqual(deferred["refresh"]["deferred_sources"], 1)
            source = HistorySource(path, session_id)
            self.assertEqual(source.locator("complete"), locator)
            self.assertEqual(source.read(locator)["text"], "complete marker")
            with self.assertRaises(HistoryError):
                source.page()
            with path.open("ab") as handle:
                handle.write(b"\n")
            completed = index.search([spec], "tail marker")
            self.assertEqual(self.ids(completed), ["tail"])
            self.assertEqual(completed["refresh"]["added_records"], 1)

    def test_ask_user_pairing_and_tool_filter_never_index_inputs(self) -> None:
        questions = [{"question": "Choose a mode", "header": "Mode", "options": [{"label": "Safe"}], "multiSelect": False}]
        ask = self.record(
            "",
            "ask",
            "assistant",
            [
                {"type": "text", "text": "Please choose."},
                {"type": "tool_use", "id": "ask-call", "name": "AskUserQuestion", "input": {"questions": questions, "hidden": "input marker"}},
            ],
        )
        spec, path, session_id = self.source(0, ask)
        answer = self.record(
            session_id,
            "answer",
            "user",
            [{"type": "tool_result", "tool_use_id": "ask-call", "content": "TOOL PROSE input marker"}],
            toolUseResult={"questions": questions, "answers": {"Choose a mode": "Safe"}, "annotations": {}},
        )
        with HistoryIndex(self.work / "ask.sqlite") as index:
            self.assertEqual(self.ids(index.search([spec], "Safe")), [])
            self.append(path, answer)
            paired = index.search([spec], "Safe", tool="AskUserQuestion")
            self.assertEqual(self.ids(paired), ["answer"])
            self.assertEqual(paired["entries"][0]["source_kind"], "verified_user_answer")
            self.assertEqual(paired["entries"][0]["tool_names"], ["AskUserQuestion"])
            self.assertEqual(self.ids(index.search([spec], "TOOL PROSE")), [])
            self.assertEqual(self.ids(index.search([spec], "input marker")), [])

        bash = self.record(
            "",
            "bash-call",
            "assistant",
            [{"type": "tool_use", "id": "bash-call", "name": "Bash", "input": {"command": "tool-input-marker"}}],
        )
        result = self.record(
            "",
            "bash-result",
            "user",
            [{"type": "tool_result", "tool_use_id": "bash-call", "content": "public Bash result"}],
        )
        tool_spec, _, _ = self.source(1, bash, result)
        with HistoryIndex(self.work / "tool.sqlite") as index:
            filtered = index.search([tool_spec], None, tool="Bash")
            self.assertEqual(self.ids(filtered), ["bash-result"])
            self.assertEqual(filtered["entries"][0]["tool_use_ids"], ["bash-call"])
            self.assertEqual(self.ids(index.search([tool_spec], "tool-input-marker")), [])

    def test_redaction_change_erases_old_index_and_fts_content_and_hidden_data_never_persists(self) -> None:
        late_secret = "late-secret-never-remains-in-cache"
        known_secret = "known-secret-never-indexed"
        assistant = self.record(
            "",
            "visible",
            "assistant",
            [
                {"type": "thinking", "thinking": "thinking-marker-never-indexed"},
                {"type": "redacted_thinking", "data": "redacted-thinking-marker-never-indexed"},
                {"type": "image", "source": {"data": "image-marker-never-indexed"}},
                {"type": "tool_use", "id": "hidden-tool", "name": "Bash", "input": {"command": "tool-input-marker-never-indexed"}},
                {"type": "text", "text": f"Visible API_KEY=assigned-token {known_secret} {late_secret}"},
            ],
        )
        spec, _, _ = self.source(0, assistant)
        cache = self.work / "private" / "redaction.sqlite"
        with HistoryIndex(cache) as index:
            initial = index.search([spec], "Visible", secrets=(known_secret,))
            self.assertEqual(self.ids(initial), ["visible"])
            for query in (
                "thinking-marker-never-indexed",
                "redacted-thinking-marker-never-indexed",
                "image-marker-never-indexed",
                "tool-input-marker-never-indexed",
                "assigned-token",
                known_secret,
            ):
                self.assertEqual(self.ids(index.search([spec], query, secrets=(known_secret,))), [])
            self.assertIn(late_secret.encode(), cache.read_bytes())
            changed = index.search([spec], "Visible", secrets=(known_secret, late_secret))
            self.assertEqual(self.ids(changed), ["visible"])
            self.assertEqual(changed["refresh"]["parsed_sources"], 1)
            self.assertNotIn(late_secret.encode(), cache.read_bytes())
            for marker in (
                b"thinking-marker-never-indexed",
                b"redacted-thinking-marker-never-indexed",
                b"image-marker-never-indexed",
                b"tool-input-marker-never-indexed",
                b"assigned-token",
                known_secret.encode(),
            ):
                self.assertNotIn(marker, cache.read_bytes())

    def test_parser_and_cache_failures_do_not_become_empty_results(self) -> None:
        spec, path, _ = self.source(0, self.record("", "valid", "user", "valid marker"))
        with path.open("ab") as handle:
            handle.write(b'{"malformed":}\n')
        with HistoryIndex(self.work / "parser.sqlite") as index:
            with self.assertRaises(HistoryIndexParserError):
                index.search([spec], "valid")

        corrupt = self.work / "corrupt.sqlite"
        corrupt.write_bytes(b"this is not a SQLite database")
        with self.assertRaises(HistoryIndexCacheError):
            HistoryIndex(corrupt)
        self.assertEqual(corrupt.read_bytes(), b"this is not a SQLite database")

    def test_search_validates_bounded_types_without_scanning_sources(self) -> None:
        with HistoryIndex(self.work / "bounds.sqlite") as index:
            invalid = (
                lambda: index.search((), None),
                lambda: index.search([], ""),
                lambda: index.search([], None, limit=21),
                lambda: index.search([], None, recent_first=1),
                lambda: index.search([], None, windows=(-1,)),
                lambda: index.search([], None, sessions=("not-a-uuid",)),
                lambda: index.search([], None, source_kinds=("not-a-kind",)),
                lambda: index.search([], None, tool=""),
                lambda: index.search([], None, cursor="not-a-cursor"),
            )
            for request in invalid:
                with self.subTest(request=request), self.assertRaises(HistoryIndexError):
                    request()

    def test_filtered_missing_window_does_not_block_selected_source(self) -> None:
        valid, _, _ = self.source(1, self.record("", "selected", "user", "kept marker"))
        sid = str(uuid4())
        missing = {"source_path": str(self.work / f"{sid}.jsonl"), "session_id": sid, "generation": 0}
        with HistoryIndex(self.work / "filtered.sqlite") as index:
            self.assertEqual(self.ids(index.search([missing, valid], "kept", windows=(1,))), ["selected"])

    def test_catalogue_is_not_limited_to_256_windows(self) -> None:
        sources = [self.source(i, self.record("", f"record-{i}", "user", "marker"))[0] for i in range(257)]
        with HistoryIndex(self.work / "many.sqlite") as index:
            result = index.search(sources, "marker", recent_first=True, limit=1)
            self.assertEqual(self.ids(result), ["record-256"])

    def test_redaction_change_removes_obsolete_trigram_terms(self) -> None:
        spec, _, _ = self.source(0, self.record("", "redacted", "user", "Visible qzxwkv"))
        path = self.work / "terms.sqlite"
        with HistoryIndex(path) as index:
            index.search([spec], "Visible")
            self.assertIn(b"qzx", path.read_bytes())
            index.search([spec], "Visible", secrets=("qzxwkv",))
            self.assertNotIn(b"qzx", path.read_bytes())

    def test_another_client_cannot_change_policy_between_refresh_and_results(self) -> None:
        spec, _, _ = self.source(0, self.record("", "secret", "user", "Visible confidential-fragment"))
        path = self.work / "shared.sqlite"
        with HistoryIndex(path) as first, HistoryIndex(path) as second:
            original = first._rows_for_page

            def interleaved(*args, **kwargs):
                try:
                    second.search([spec], None, secrets=())
                except HistoryIndexCacheError:
                    pass
                return original(*args, **kwargs)

            with patch.object(first, "_rows_for_page", side_effect=interleaved):
                result = first.search([spec], "Visible", secrets=("confidential-fragment",))
            self.assertNotIn("confidential-fragment", result["entries"][0]["snippet"])

    def test_private_cache_scope_and_symlink_rejection(self) -> None:
        private_cache = self.work / "private" / "nested" / "history.sqlite"
        with HistoryIndex(private_cache) as index:
            missing_session = str(uuid4())
            missing = {
                "source_path": str(self.work / f"{missing_session}.jsonl"),
                "session_id": missing_session,
                "generation": 0,
            }
            # The window filter selects nothing, so an otherwise nonexistent
            # explicit source must not be opened or scanned.
            empty = index.search([missing], None, windows=(1,))
            self.assertEqual(empty["entries"], [])
            self.assertEqual(empty["refresh"]["parsed_sources"], 0)
        self.assertEqual(stat.S_IMODE(private_cache.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(private_cache.parent.stat().st_mode), 0o700)

        target = self.work / "target.sqlite"
        target.write_bytes(b"unrelated")
        link = self.work / "linked.sqlite"
        link.symlink_to(target)
        with self.assertRaises(HistoryIndexCacheError):
            HistoryIndex(link)
        self.assertEqual(target.read_bytes(), b"unrelated")


if __name__ == "__main__":
    unittest.main()
