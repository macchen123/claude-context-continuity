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

from claude_context_continuity import history_index  # noqa: E402
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
    def peer_message(sender: str, body: str) -> tuple[str, str]:
        envelope = (f'<cross-session-message from="{sender}" from-name="fixture-peer" '
                    f'from-mode="default">\n{body}\n</cross-session-message>')
        return envelope, ("Another Claude session sent a message:\n" + envelope
                          + "\n<system-reminder>Native host safety footer.</system-reminder>")

    @staticmethod
    def ids(result: dict[str, object]) -> list[str]:
        return [entry["locator"]["message_id"] for entry in result["entries"]]  # type: ignore[index]

    def test_extension_metadata_preserves_search_and_exact_locators(self) -> None:
        spec, path, sid = self.source(0, self.record("", "user-1", "user", "authorized needle"))
        with HistoryIndex(self.work / "extensions.sqlite") as index:
            before = index.search([spec], "needle")
            for kind in ("tools-used", "tools-used", "future-native-state"):
                self.append(path, {"type": kind, "sessionId": sid, "tools": ["Bash"],
                                   "message": {"role": "user", "content": "metadata-only-marker"}})
            after = index.search([spec], "needle")
            self.assertEqual(self.ids(after), ["user-1"])
            locator = after["entries"][0]["locator"]
            self.assertEqual(locator, before["entries"][0]["locator"])
            self.assertEqual(HistorySource(path, sid).read(locator)["text"], "authorized needle")
            self.assertEqual(index.search([spec], "metadata-only-marker")["entries"], [])

    def test_fts_errors_are_reported_without_silent_scan_retry(self) -> None:
        spec, _, _ = self.source(0, self.record("", "user-1", "user", "authorized needle"))
        with HistoryIndex(self.work / "fts-error.sqlite") as index:
            with patch.object(index, "_query_rows", side_effect=apsw.SQLError("syntax error")) as query:
                with self.assertRaises(HistoryIndexCacheError):
                    index.search([spec], "needle")
            query.assert_called_once()

    def test_appended_pr_link_without_uuid_does_not_block_search_or_become_instruction(self) -> None:
        spec, path, sid = self.source(0, self.record("", "user-1", "user", "authorized needle"))
        with HistoryIndex(self.work / "pr-link.sqlite") as index:
            before = index.search([spec], "needle", source_kinds=("original_user",))
            link = {"type": "pr-link", "sessionId": sid, "prNumber": 1,
                    "prUrl": "https://github.com/example/pr-metadata-only/pull/1",
                    "prRepository": "example/pr-metadata-only", "timestamp": "2026-09-18T00:00:00.000Z"}
            self.append(path, link)
            self.append(path, link)
            after = index.search([spec], "needle", source_kinds=("original_user",))
            self.assertEqual(self.ids(after), ["user-1"])
            self.assertEqual(after["entries"][0]["locator"], before["entries"][0]["locator"])
            self.assertEqual(index.search([spec], "pr-metadata-only")["entries"], [])

    def test_appended_agent_name_without_uuid_does_not_block_search_or_become_instruction(self) -> None:
        spec, path, sid = self.source(0, self.record("", "user-1", "user", "authorized needle"))
        with HistoryIndex(self.work / "agent-name.sqlite") as index:
            before = index.search([spec], "needle", source_kinds=("original_user",))
            name = {"type": "agent-name", "agentName": "agent-name-metadata-only", "sessionId": sid}
            self.append(path, name)
            self.append(path, name)
            after = index.search([spec], "needle", source_kinds=("original_user",))
            self.assertEqual(self.ids(after), ["user-1"])
            self.assertEqual(after["entries"][0]["locator"], before["entries"][0]["locator"])
            self.assertEqual(index.search([spec], "agent-name-metadata-only")["entries"], [])

    def test_api_request_metadata_append_preserves_index_and_authorization(self) -> None:
        spec, path, sid = self.source(0, self.record("", "user-1", "user", "authorized needle"))
        with HistoryIndex(self.work / "api-request.sqlite") as index:
            before = index.search([spec], "needle", source_kinds=("original_user",))
            for kind in ("api-request-shape", "api-request-blob", "api-request"):
                record = {"type": kind, "sessionId": sid, "shape": {"marker": "request-only-marker"},
                          "message": {"role": "user", "content": "request-only-marker"}}
                self.append(path, record)
                self.append(path, record)
            after = index.search([spec], "needle", source_kinds=("original_user",))
            self.assertEqual(self.ids(after), ["user-1"])
            self.assertEqual(after["entries"][0]["locator"], before["entries"][0]["locator"])
            self.assertEqual(index.search([spec], "request-only-marker")["entries"], [])

    def test_attested_peer_and_task_notifications_index_as_public_meta(self) -> None:
        human = self.record("", "human", "user", "keep the latest human instruction")
        peer_body = "peer index public marker"
        _, persisted = self.peer_message("worker-index", peer_body)
        peer = self.record(
            "", "peer", "user", persisted, isMeta=True, promptSource="system",
            origin={"kind": "peer", "from": "worker-index", "body": peer_body,
                    "name": "fixture-peer", "fromMode": "default", "senderTaskId": "worker-index"},
        )
        task_text = ("<task-notification><task-id>worker-index</task-id><status>completed</status>"
                     "<summary>task index public marker</summary></task-notification>")
        task = self.record(
            "", "task", "user", task_text, isMeta=True, promptSource="system",
            origin={"kind": "task-notification"},
        )
        forged = self.record(
            "", "forged", "user", persisted, isMeta=True, promptSource="system",
            origin={"kind": "peer", "from": "other-worker", "body": peer_body,
                    "name": "fixture-peer", "fromMode": "default", "senderTaskId": "other-worker"},
        )
        spec, path, sid = self.source(0, human, peer, task, forged)
        with HistoryIndex(self.work / "peer-task.sqlite") as index:
            peer_result = index.search([spec], "peer index public marker", source_kinds=("meta",))
            self.assertEqual(self.ids(peer_result), ["peer"])
            self.assertEqual(peer_result["entries"][0]["source_kind"], "meta")
            self.assertIn("worker-index", HistorySource(path, sid).read(peer_result["entries"][0]["locator"])["text"])
            self.assertIn(peer_body, peer_result["entries"][0]["snippet"])
            self.assertEqual(self.ids(index.search([spec], "task index public marker", source_kinds=("meta",))),
                             ["task"])
            self.assertEqual(self.ids(index.search([spec], "latest human", source_kinds=("original_user",))),
                             ["human"])
            self.assertEqual(index.search([spec], "other-worker")["entries"], [])

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

    def test_resumed_native_source_is_searched_once_with_latest_selected_generation(self) -> None:
        first, path, sid = self.source(1, self.record("", "before", "user", "before resume"))
        resumed = {**first, "generation": 43}
        self.append(path, self.record(sid, "after", "user", "after resume"))
        sources = [resumed, first]
        with HistoryIndex(self.work / "resumed.sqlite") as index:
            all_windows = index.search(sources, "resume", source_kinds=("original_user",))
            self.assertEqual(self.ids(all_windows), ["before", "after"])
            self.assertEqual([entry["generation"] for entry in all_windows["entries"]], [43, 43])
            self.assertEqual(all_windows["refresh"]["parsed_sources"], 1)
            self.assertEqual(self.ids(index.search(sources, "resume", windows=(1,))), ["before", "after"])
            self.assertEqual(self.ids(index.search(sources, "resume", windows=(43,))), ["before", "after"])
            both = index.search(sources, "resume", windows=(1, 43), limit=1)
            self.assertEqual(self.ids(both), ["before"])
            self.assertEqual(self.ids(index.search(sources, "resume", windows=(1, 43), limit=1,
                                                   cursor=both["next_cursor"])), ["after"])
            with self.assertRaises(HistoryIndexError):
                index.search([first, first], None)
            with self.assertRaises(HistoryIndexError):
                index.search([first, resumed, first], None)
            alias = self.work / "alias"
            alias.mkdir()
            (alias / path.name).symlink_to(path)
            with self.assertRaises(HistoryIndexError):
                index.search([first, {**resumed, "source_path": str(alias / path.name)}], None)

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

    def test_redaction_policy_version_reprojects_cached_peer_meta(self) -> None:
        self.assertGreaterEqual(history_index._REDACTION_POLICY_VERSION, 2)
        peer_body = "peer cache policy marker"
        _, persisted = self.peer_message("worker-cache", peer_body)
        peer = self.record(
            "", "peer", "user", persisted, isMeta=True, promptSource="system",
            origin={"kind": "peer", "from": "worker-cache", "body": peer_body,
                    "name": "fixture-peer", "fromMode": "default", "senderTaskId": "worker-cache"},
        )
        spec, _, _ = self.source(0, peer)
        cache = self.work / "peer-policy.sqlite"
        old_version = history_index._REDACTION_POLICY_VERSION - 1
        with patch.object(history_index, "_REDACTION_POLICY_VERSION", old_version):
            with HistoryIndex(cache) as index:
                self.assertEqual(self.ids(index.search([spec], peer_body)), ["peer"])
        with HistoryIndex(cache) as index:
            refreshed = index.search([spec], peer_body)
        self.assertEqual(self.ids(refreshed), ["peer"])
        self.assertEqual(refreshed["refresh"]["parsed_sources"], 1)
        self.assertEqual(refreshed["refresh"]["unchanged_sources"], 0)

    def test_current_notice_policy_reprojects_previously_hidden_meta(self) -> None:
        self.assertGreaterEqual(history_index._REDACTION_POLICY_VERSION, 4)
        body = "current peer marker"
        _, persisted = self.peer_message("worker-current", body)
        peer = self.record("", "peer", "user", persisted, promptSource="system",
            origin={"kind": "peer", "from": "worker-current", "name": "fixture-peer",
                    "fromMode": "default", "body": body})
        text = ("<task-notification><task-id>worker-current</task-id><status>completed</status>"
                "<summary>current task marker</summary></task-notification>")
        task = self.record("", "task", "user", text, promptSource="system",
            origin={"kind": "task-notification", "producer": "session-task", "runId": "run-1"})
        spec, path, sid = self.source(0, peer, task)
        previous = HistorySource(path, sid).index_projection()
        previous["entries"] = []
        cache = self.work / "current-notice-policy.sqlite"
        with patch.object(history_index, "_REDACTION_POLICY_VERSION", 3), \
                patch.object(HistorySource, "index_projection", return_value=previous):
            with HistoryIndex(cache) as index:
                self.assertEqual(index.search([spec], "current")["entries"], [])
        with HistoryIndex(cache) as index:
            refreshed = index.search([spec], "current", source_kinds=("meta",))
            self.assertEqual(set(self.ids(refreshed)), {"peer", "task"})
            self.assertEqual(refreshed["refresh"]["parsed_sources"], 1)
            self.assertEqual(index.search([spec], None, source_kinds=("original_user",))["entries"], [])

    def test_native_loop_policy_rebuilds_hidden_command_intent(self) -> None:
        self.assertGreaterEqual(history_index._REDACTION_POLICY_VERSION, 5)
        command = self.record("", "loop", "user", "<command-name>/loop</command-name>\n"
            "<command-message>loop</command-message>\n<command-args>unique loop task</command-args>",
            userType="external", promptId=str(uuid4()))
        spec, path, sid = self.source(0, command)
        previous = HistorySource(path, sid).index_projection()
        previous["entries"] = []
        cache = self.work / "native-loop-policy.sqlite"
        with patch.object(history_index, "_REDACTION_POLICY_VERSION", 4), \
                patch.object(HistorySource, "index_projection", return_value=previous):
            with HistoryIndex(cache) as index:
                self.assertEqual(index.search([spec], "unique loop task")["entries"], [])
        with HistoryIndex(cache) as index:
            result = index.search([spec], "unique loop task", source_kinds=("local_command",))
            self.assertEqual(self.ids(result), ["loop"])
            self.assertEqual(index.search([spec], None, source_kinds=("original_user",))["entries"], [])
            self.assertEqual(result["refresh"]["parsed_sources"], 1)
            self.assertIn("unique loop task", HistorySource(path, sid).read(result["entries"][0]["locator"])["text"])

    def test_question_policy_rebuilds_cached_answer_authority(self) -> None:
        self.assertGreaterEqual(history_index._REDACTION_POLICY_VERSION, 3)
        questions = [{"question": "Publish?"}]
        ask = self.record("", "ask", "assistant", [{"type": "tool_use", "name": "AskUserQuestion",
            "id": "ask-1", "input": {"questions": questions}}])
        result = self.record("", "answer", "user", [{"type": "tool_result", "tool_use_id": "ask-1",
            "is_error": True, "content": "Rejected; do not publish."}], toolUseResult={
            "questions": questions, "answers": {"Publish?": "Yes"}, "annotations": {}})
        spec, path, sid = self.source(0, ask, result)
        previous = HistorySource(path, sid).index_projection()
        previous["entries"][0]["source_kind"] = "verified_user_answer"
        previous["entries"][0]["locator"]["source_kind"] = "verified_user_answer"
        previous["entries"][0]["text"] = "Question: Publish?\nAnswer: Yes"
        previous["entries"][0]["total_chars"] = len(previous["entries"][0]["text"])
        cache = self.work / "question-policy.sqlite"
        with patch.object(history_index, "_REDACTION_POLICY_VERSION", 2), \
                patch.object(HistorySource, "index_projection", return_value=previous):
            with HistoryIndex(cache) as index:
                self.assertEqual(self.ids(index.search([spec], None, source_kinds=("verified_user_answer",))), ["answer"])
        with HistoryIndex(cache) as index:
            refreshed = index.search([spec], None, source_kinds=("verified_user_answer",))
            self.assertEqual(self.ids(refreshed), [])
            self.assertEqual(refreshed["refresh"]["parsed_sources"], 1)
            self.assertEqual(self.ids(index.search([spec], "Rejected", source_kinds=("tool_result",))), ["answer"])

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


def test_retained_index_scale_measures_decode_projection_and_validation_work(tmp_path):
    specs = []
    total_bytes = 0
    live_path = None
    live_sid = None
    live_raw = b""
    for window in range(160):
        sid = str(uuid4())
        path = tmp_path / f"{sid}.jsonl"
        records = [HistoryIndexTests().record(sid, f"u-{i}", "user", "public window instruction " + "x" * 200)
                   for i in range(128)]
        if window == 159:
            for agent in range(1_000):
                records.extend([
                    HistoryIndexTests().record(sid, f"a-{agent}", "assistant", [
                        {"type": "tool_use", "id": f"call-{agent}", "name": "Agent",
                         "input": {"name": f"fixture-agent-{agent}", "prompt": "private-tool-input-not-indexed"}}]),
                    HistoryIndexTests().record(sid, f"r-{agent}", "user", [
                        {"type": "tool_result", "tool_use_id": f"call-{agent}", "content": "public agent result"}],
                        toolUseResult={"agentId": f"agent-{agent}", "isAsync": True}),
                ])
        raw = b"".join(HistoryIndexTests.line(record) for record in records)
        path.write_bytes(raw)
        total_bytes += len(raw)
        specs.append({"source_path": str(path), "session_id": sid, "generation": window})
        live_path, live_sid, live_raw = path, sid, raw
    cache = tmp_path / "scale.sqlite"
    with HistoryIndex(cache) as index:
        cold = index.search(specs, None)["refresh"]
        assert cold["parsed_records"] == cold["projected_records"] == 160 * 128 + 2_000
        assert cold["parsed_bytes"] == cold["read_bytes"] == total_bytes
        hot = index.search(specs, None)["refresh"]
        assert hot["unchanged_sources"] == 160
        assert hot["parsed_records"] == hot["parsed_bytes"] == hot["read_bytes"] == 0
        append = HistoryIndexTests.line(HistoryIndexTests().record(live_sid, "append", "user", "incremental marker"))
        with live_path.open("ab") as handle:
            handle.write(append)
        warm = index.search(specs, "incremental marker")["refresh"]
        assert warm["parsed_sources"] == warm["parsed_records"] == warm["projected_records"] == 1
        assert warm["unchanged_sources"] == 159
        assert warm["parsed_bytes"] == len(append)
        assert warm["validated_bytes"] == len(live_raw)
        assert warm["read_bytes"] == len(live_raw) + len(append)
        assert warm["added_records"] == 1
    # The SQLite public cache survives reopen, but the canonical parser state
    # intentionally does not. A separately launched process skips unchanged
    # sources and must parse a changed source from the beginning.
    with HistoryIndex(cache) as index:
        reopened = index.search(specs, None)["refresh"]
        assert reopened["read_bytes"] == reopened["parsed_records"] == 0
        with live_path.open("ab") as handle:
            handle.write(HistoryIndexTests.line(HistoryIndexTests().record(live_sid, "append-2", "user", "second marker")))
        separate = index.search(specs, "second marker")["refresh"]
        assert separate["parsed_records"] == 128 + 2_000 + 2
        assert separate["parsed_sources"] == 1
    print(json.dumps({"scenario": "160 windows / 1000 Agent calls", "cold": cold, "unchanged": hot,
                      "append": warm, "reopened_unchanged": reopened, "reopened_append": separate}, sort_keys=True))


def test_incremental_index_reprojects_revoked_answers_and_reports_conflicting_tools(tmp_path):
    import pytest
    sid = str(uuid4())
    fixture = HistoryIndexTests()
    questions = [{"question": "Publish?"}]
    ask = fixture.record(sid, "ask", "assistant", [{"type": "tool_use", "id": "q", "name": "AskUserQuestion",
                                                   "input": {"questions": questions}}])
    answer = fixture.record(sid, "answer", "user", [{"type": "tool_result", "tool_use_id": "q", "content": "public result"}],
                            toolUseResult={"questions": questions, "answers": {"Publish?": "No"}, "annotations": {}})
    path = tmp_path / f"{sid}.jsonl"
    path.write_bytes(fixture.line(ask) + fixture.line(answer))
    spec = {"source_path": str(path), "session_id": sid, "generation": 0}
    with HistoryIndex(tmp_path / "answers.sqlite") as index:
        assert index.search([spec], None)["entries"][0]["source_kind"] == "verified_user_answer"
        fixture.append(path, {**answer, "uuid": "second-result"})
        result = index.search([spec], None)
        assert {entry["source_kind"] for entry in result["entries"]} == {"tool_result"}
        assert result["refresh"]["changed_records"] == 1
        assert result["refresh"]["added_records"] == 1
        assert result["refresh"]["parsed_records"] == 1
        fixture.append(path, fixture.record(sid, "conflict", "assistant", [
            {"type": "tool_use", "id": "q", "name": "Bash", "input": {}}]))
        with pytest.raises(HistoryIndexParserError):
            index.search([spec], None)


def test_incremental_index_rolls_back_failed_merge_without_acknowledging_revision(tmp_path):
    import pytest
    sid = str(uuid4())
    fixture = HistoryIndexTests()
    path = tmp_path / f"{sid}.jsonl"
    path.write_bytes(fixture.line(fixture.record(sid, "first", "user", "first public text")))
    spec = {"source_path": str(path), "session_id": sid, "generation": 0}
    with HistoryIndex(tmp_path / "rollback.sqlite") as index:
        index.search([spec], None)
        fixture.append(path, fixture.record(sid, "second", "user", "second public text"))
        with patch.object(index, "_insert_record", side_effect=apsw.SQLError("injected failure")):
            with pytest.raises(HistoryIndexCacheError):
                index.search([spec], None)
        result = index.search([spec], None)
        assert fixture.ids(result) == ["first", "second"]
        assert result["refresh"]["parsed_records"] == 0
        assert result["refresh"]["added_records"] == 1


def test_external_cache_refresh_cannot_make_retained_delta_keep_stale_prefix(tmp_path):
    sid = str(uuid4())
    fixture = HistoryIndexTests()
    path = tmp_path / f"{sid}.jsonl"
    old = fixture.line(fixture.record(sid, "same", "user", "original text"))
    path.write_bytes(old)
    spec = {"source_path": str(path), "session_id": sid, "generation": 0}
    cache = tmp_path / "shared.sqlite"
    with HistoryIndex(cache) as first:
        first.search([spec], None)
        path.write_bytes(fixture.line(fixture.record(sid, "same", "user", "rewritten text")))
        with HistoryIndex(cache) as second:
            assert second.search([spec], "rewritten")["entries"]
        path.write_bytes(old + fixture.line(fixture.record(sid, "append", "user", "new append")))
        result = first.search([spec], "original")
        assert fixture.ids(result) == ["same"]
        assert first.search([spec], "rewritten")["entries"] == []
        assert fixture.ids(first.search([spec], "new append")) == ["append"]


def test_canonical_projection_spans_unconsumed_revisions_without_copying_unchanged_entries(tmp_path):
    sid = str(uuid4())
    fixture = HistoryIndexTests()
    path = tmp_path / f"{sid}.jsonl"
    path.write_bytes(fixture.line(fixture.record(sid, "baseline", "user", "original public text")))
    source = HistorySource(path, sid)
    first = source.index_projection()
    fixture.append(path, fixture.record(sid, "second", "user", "second public text"))
    source.index_projection(since=first["revision"])
    fixture.append(path, fixture.record(sid, "third", "user", "third public text"))
    source.index_projection()
    delta = source.index_projection(since=first["revision"])
    assert not delta["reset"]
    assert [entry["locator"]["message_id"] for entry in delta["entries"]] == ["second", "third"]
    assert delta["removed_start_bytes"] == []
    assert source.index_projection(since=delta["revision"])["entries"] == []
    assert source.index_projection(since=delta["revision"] + 100)["reset"]
    assert len(source.index_projection()["entries"]) == 3


def test_pending_append_cannot_make_an_adjacent_future_projection_revision_valid(tmp_path):
    sid = str(uuid4())
    fixture = HistoryIndexTests()
    path = tmp_path / f"{sid}.jsonl"
    path.write_bytes(fixture.line(fixture.record(sid, "baseline", "user", "original public text")))
    source = HistorySource(path, sid)
    baseline = source.index_projection()
    fixture.append(path, fixture.record(sid, "pending", "user", "pending public text"))
    future = source.index_projection(since=baseline["revision"] + 1)
    assert future["revision"] == baseline["revision"] + 1
    assert future["reset"]
    assert [entry["locator"]["message_id"] for entry in future["entries"]] == ["baseline", "pending"]
    committed = source.index_projection(since=baseline["revision"])
    assert not committed["reset"]
    assert [entry["locator"]["message_id"] for entry in committed["entries"]] == ["pending"]


if __name__ == "__main__":
    unittest.main()
