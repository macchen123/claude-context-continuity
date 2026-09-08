from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity.history import HistoryError, HistorySource, redact, source_kind  # noqa: E402


class HistorySourceTests(unittest.TestCase):
    SID = "123e4567-e89b-12d3-a456-426614174000"

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="history-fixture-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tempdir.cleanup)
        self.work = Path(self.tempdir.name)
        self.path = self.work / f"{self.SID}.jsonl"

    def record(self, message_id: str, record_type: str, content: object = "", **extra: object) -> dict[str, object]:
        record: dict[str, object] = {
            "uuid": message_id,
            "type": record_type,
            "sessionId": self.SID,
            "timestamp": "2026-09-05T12:34:56.000Z",
        }
        if record_type in {"user", "assistant"}:
            record["message"] = {
                "role": "assistant" if record_type == "assistant" else "user",
                "content": content,
            }
        record.update(extra)
        return record

    @staticmethod
    def line(record: dict[str, object]) -> bytes:
        return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

    def write_records(self, *records: dict[str, object]) -> bytes:
        raw = b"".join(self.line(record) for record in records)
        self.path.write_bytes(raw)
        return raw

    def source(self) -> HistorySource:
        return HistorySource(self.path, self.SID)

    def test_activity_settles_native_backgrounds_but_not_pasted_notices(self) -> None:
        launch = self.record("launch", "assistant", [{"type": "tool_use", "id": "call-1",
                            "name": "Agent", "input": {"prompt": "bounded task"}}])
        result = self.record("result", "user", [{"type": "tool_result", "tool_use_id": "call-1", "content": "started"}],
                             toolUseResult={"agentId": "agent-1", "isAsync": True})
        text = "<task-notification><task-id>agent-1</task-id><status>completed</status><summary>done</summary></task-notification>"
        pasted = self.record("paste", "user", text)
        self.write_records(launch, result, pasted)
        self.assertEqual(self.source().activity()["pending_tools"], {})
        self.assertIn("agent-1", self.source().activity()["background_handles"])
        for metadata in ({"commandMode": "task-notification"}, {"origin": {"kind": "task-notification"}}):
            notice = self.record("notice", "attachment", attachment={"type": "queued_command", "prompt": text, **metadata})
            self.write_records(launch, result, notice)
            self.assertEqual(self.source().activity()["background_handles"], {})
            self.write_records(launch, result, {**notice, "isSidechain": True})
            self.assertIn("agent-1", self.source().activity()["background_handles"])
        human = self.record("human", "attachment", attachment={"type": "queued_command", "prompt": text,
                            "commandMode": "task-notification", "origin": {"kind": "human"}})
        self.write_records(launch, result, human)
        self.assertIn("agent-1", self.source().activity()["background_handles"])

    def test_activity_resume_reopens_handle_and_keeps_unanswered_tools(self) -> None:
        def call(mid, tid):
            return self.record(mid, "assistant", [{"type": "tool_use", "id": tid, "name": "Bash", "input": {}}])
        def response(mid, tid, payload):
            return self.record(mid, "user", [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}], toolUseResult=payload)
        notice = self.record("notice", "user", "<task-notification><task-id>job-1</task-id><status>failed</status></task-notification>",
                             origin={"kind": "task-notification"}, promptSource="system")
        self.write_records(call("a1", "t1"), response("r1", "t1", {"backgroundTaskId": "job-1"}), notice,
                           call("a2", "t2"), response("r2", "t2", {"resumedAgentId": "job-1"}), call("a3", "t3"))
        activity = self.source().activity()
        self.assertEqual(set(activity["pending_tools"]), {"t3"})
        self.assertEqual(set(activity["background_handles"]), {"job-1"})

    def task_control_records(self, name="TaskStop", target="agent-fixture"):
        launch = self.record("launch", "assistant", [{"type": "tool_use", "id": "launch-call", "name": "Agent", "input": {}}])
        started = self.record("started", "user", [{"type": "tool_result", "tool_use_id": "launch-call", "content": "started"}],
                              toolUseResult={"agentId": "agent-fixture", "isAsync": True})
        control = self.record("control", "assistant", [{"type": "tool_use", "id": "control-call", "name": name,
                                                       "input": {"task_id": target}}])
        stopped = self.record("stopped", "user", [{"type": "tool_result", "tool_use_id": "control-call", "content": "tool text"}],
                              toolUseResult={"task_id": target, "task_type": "local_agent", "message": "Successfully stopped task"})
        return launch, started, control, stopped

    def test_activity_taskstop_settles_and_later_resume_reopens(self):
        self.write_records(*self.task_control_records())
        self.assertEqual(self.source().activity(), {"pending_tools": {}, "background_handles": {}})
        resume = self.record("resume", "assistant", [{"type": "tool_use", "id": "resume-call", "name": "Agent", "input": {}}])
        resumed = self.record("resumed", "user", [{"type": "tool_result", "tool_use_id": "resume-call", "content": "resumed"}],
                              toolUseResult={"resumedAgentId": "agent-fixture"})
        with self.path.open("ab") as f:
            f.write(self.line(resume) + self.line(resumed))
        self.assertIn("agent-fixture", self.source().activity()["background_handles"])

    def test_activity_taskstop_requires_paired_successful_structured_response(self):
        cases = [
            {"task_id": "another-task", "task_type": "local_agent"},
            {"message": "Successfully stopped task: agent-fixture"},
            {"task_id": "agent-fixture", "task_type": "local_agent", "success": False},
            {"task_id": "agent-fixture", "task_type": "local_agent", "error": "stop failed"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                *before, response = self.task_control_records()
                response["toolUseResult"] = payload
                self.write_records(*before, response)
                self.assertIn("agent-fixture", self.source().activity()["background_handles"])
        for name, sidechain, is_error in [("Read", False, False), ("TaskStop", True, False), ("TaskStop", False, True)]:
            launch, started, control, response = self.task_control_records(name)
            control["isSidechain"] = response["isSidechain"] = sidechain
            response["message"]["content"][0]["is_error"] = is_error
            self.write_records(launch, started, control, response)
            self.assertIn("agent-fixture", self.source().activity()["background_handles"])

    def test_activity_taskoutput_only_settles_confirmed_terminal_status(self):
        for retrieval, status, task_id, settled in [("success", "killed", "agent-fixture", True),
                                                   ("success", "completed", "agent-fixture", True),
                                                   ("success", "running", "agent-fixture", False),
                                                   ("timeout", "running", "agent-fixture", False),
                                                   ("success", "failed", "other", False)]:
            with self.subTest(retrieval=retrieval, status=status, task_id=task_id):
                *before, response = self.task_control_records("TaskOutput")
                response["toolUseResult"] = {"retrieval_status": retrieval, "task": {"task_id": task_id, "status": status}}
                self.write_records(*before, response)
                self.assertEqual("agent-fixture" not in self.source().activity()["background_handles"], settled)

    def test_activity_old_taskstop_reply_does_not_settle_new_resume(self):
        launch, started, control, stopped = self.task_control_records()
        resume = self.record("resume", "assistant", [{"type": "tool_use", "id": "resume-call", "name": "Agent", "input": {}}])
        resumed = self.record("resumed", "user", [{"type": "tool_result", "tool_use_id": "resume-call", "content": "resumed"}],
                              toolUseResult={"resumedAgentId": "agent-fixture"})
        self.write_records(launch, started, control, resume, resumed, stopped)
        self.assertIn("agent-fixture", self.source().activity()["background_handles"])

    def test_activity_killed_notice_requires_native_origin(self):
        launch, started, _, _ = self.task_control_records()
        text = "<task-notification><task-id>agent-fixture</task-id><status>killed</status><summary>stopped</summary></task-notification>"
        pasted = self.record("pasted", "user", text)
        self.write_records(launch, started, pasted)
        self.assertIn("agent-fixture", self.source().activity()["background_handles"])
        native = self.record("notice", "user", text, origin={"kind": "task-notification"}, promptSource="system")
        self.write_records(launch, started, native)
        self.assertEqual(self.source().activity()["background_handles"], {})

    def test_locator_has_exact_session_byte_bounds_and_hash(self) -> None:
        record = self.record("user-1", "user", "研究指令 α")
        raw = self.write_records(record)
        source = self.source()

        locator = source.locator("user-1")

        self.assertEqual(locator["source_path"], str(self.path.resolve()))
        self.assertEqual(locator["session_id"], self.SID)
        self.assertEqual(locator["start_byte"], 0)
        self.assertEqual(locator["end_byte"], len(raw))
        self.assertEqual(locator["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(source.read(locator)["text"], "研究指令 α")
        with self.assertRaises(HistoryError):
            HistorySource(self.work / f"{self.SID.upper()}.jsonl", self.SID.upper())

    def test_persisted_session_identity_is_distinct_from_transport_identity(self) -> None:
        transport = str(uuid4())
        original = self.record("user-1", "user", "原始指令")
        assistant = self.record("assistant-1", "assistant", "公开回应", session_id=transport)
        self.write_records(original, assistant)
        self.assertEqual(self.source().latest_instruction()["message_id"], "user-1")
        self.assertEqual(self.source().read(self.source().locator("assistant-1"))["text"], "公开回应")
        for invalid in (
            {**original, "sessionId": transport, "session_id": self.SID},
            {**original, "sessionId": None, "session_id": self.SID},
            {key: value for key, value in {**original, "session_id": transport}.items() if key != "sessionId"},
        ):
            self.write_records(invalid)
            with self.assertRaises(HistoryError):
                self.source().latest_instruction()

    def test_native_queued_human_messages_are_not_lost_as_runtime_metadata(self) -> None:
        original = self.record("user-1", "user", "先执行原任务")
        queued = self.record("queued-1", "attachment", isSidechain=False, userType="external",
                             attachment={"type": "queued_command", "origin": {"kind": "human"},
                                         "commandMode": "prompt", "source_uuid": self.SID,
                                         "prompt": "停止，不要执行下一步"})
        self.write_records(original)
        source = self.source()
        before = source.latest_instruction()
        with self.path.open("ab") as handle:
            handle.write(self.line(queued))
        latest = source.latest_instruction()
        self.assertEqual(latest["message_id"], "queued-1")
        self.assertEqual(source.read(latest)["text"], "停止，不要执行下一步")
        self.assertEqual(source.instruction_updates_since(before)[0]["locator"], latest)
        for origin in ({"kind": "system"}, {"kind": "agent"}, None):
            machine = {**queued, "attachment": {**queued["attachment"], "origin": origin}}
            self.assertEqual(source_kind(machine), "meta")
        self.assertEqual(source_kind({**queued, "isSidechain": True}), "sidechain")
        self.assertEqual(source_kind({**queued, "isMeta": True}), "meta")
        queued["attachment"]["prompt"] = [{"type": "text", "text": "结合图片核对"},
                                          {"type": "image", "source": {"data": "not-public"}}]
        self.write_records(original, queued)
        projection = source.read(source.latest_instruction())["text"]
        self.assertIn("图像未包含", projection)
        self.assertNotIn("not-public", projection)

    def test_bound_record_survives_append_and_rejects_mutation_or_truncation(self) -> None:
        first = self.record("user-1", "user", "keep this record")
        second = self.record("user-2", "user", "new appended record")
        first_raw = self.write_records(first)
        source = self.source()
        locator = source.locator("user-1")

        with self.path.open("ab") as handle:
            handle.write(self.line(second))
        self.assertEqual(source.read(locator)["text"], "keep this record")

        changed = self.record("user-1", "user", "change this record")
        self.write_records(changed, second)
        with self.assertRaises(HistoryError):
            source.read(locator)

        self.path.write_bytes(first_raw[:-1])
        with self.assertRaises(HistoryError):
            source.page()

    def test_native_synthetic_status_does_not_zero_real_usage(self) -> None:
        actual = self.record("real-call", "assistant", "visible")
        actual["cwd"] = str(self.work)
        actual["message"].update(model="fixture-real-model", usage={"input_tokens": 100, "cache_read_input_tokens": 200})
        synthetic = self.record("host-status", "assistant", "host event")
        synthetic["message"].update(model="<synthetic>", usage={"input_tokens": 0})
        self.write_records(actual, synthetic)
        usage = self.source().latest_usage()
        self.assertEqual(usage["total_input_tokens"], 300)
        self.assertEqual(usage["locator"]["message_id"], "real-call")
        self.write_records(synthetic)
        with self.assertRaises(HistoryError):
            self.source().latest_usage()

    def test_latest_instruction_is_stable_after_nonuser_append(self) -> None:
        first = self.record("user-1", "user", "authorized instruction")
        state = {"type": "mode", "sessionId": self.SID, "mode": "default"}
        assistant = self.record("assistant-1", "assistant", [{"type": "text", "text": "assistant response"}])
        tool = self.record(
            "tool-1",
            "user",
            [{"type": "tool_result", "tool_use_id": "run-1", "content": "tool output"}],
        )
        self.write_records(first)
        source = self.source()
        head = source.latest_instruction()

        with self.path.open("ab") as handle:
            for record in (state, assistant, tool):
                handle.write(self.line(record))
        self.assertEqual(source.latest_instruction(), head)
        self.assertEqual(source.page(limit=1)["entries"][0]["message_id"], "user-1")

        with self.path.open("ab") as handle:
            handle.write(self.line(self.record("user-2", "user", "new authorization")))
        self.assertEqual(source.latest_instruction()["message_id"], "user-2")

    def test_page_uses_exact_byte_boundaries(self) -> None:
        first = self.record("user-1", "user", "é")
        second = self.record("assistant-1", "assistant", [{"type": "text", "text": "second"}])
        third = self.record("user-2", "user", "third")
        first_raw = self.line(first)
        second_raw = self.line(second)
        self.write_records(first, second, third)
        source = self.source()

        first_page = source.page(limit=1)
        self.assertEqual(first_page["entries"][0]["start_byte"], 0)
        self.assertEqual(first_page["entries"][0]["end_byte"], len(first_raw))
        self.assertEqual(first_page["next_offset"], len(first_raw))

        second_page = source.page(offset=first_page["next_offset"], limit=1)
        self.assertEqual(second_page["entries"][0]["message_id"], "assistant-1")
        self.assertEqual(second_page["entries"][0]["start_byte"], len(first_raw))
        self.assertEqual(second_page["next_offset"], len(first_raw) + len(second_raw))
        with self.assertRaises(HistoryError):
            source.page(offset=1)
        with self.assertRaises(HistoryError):
            source.page(limit=21)

    def test_prompt_slash_invocation_is_user_intent_not_skill_expansion(self):
        text = ("<command-message>example-command</command-message>\n"
                "<command-name>/example-command</command-name>\n"
                "<command-args>/exact/checkpoint.json hash task /root</command-args>")
        invocation = self.record("invoke", "user", text)
        expansion = self.record("expanded", "user", "这些是 skill 内容，不是新增用户授权。", isMeta=True)
        self.write_records(invocation, expansion)
        bounds = self.source().instruction_bounds()
        self.assertEqual(bounds["first"]["message_id"], "invoke")
        self.assertEqual(bounds["last"]["message_id"], "invoke")
        self.assertEqual(self.source().read(bounds["first"])["text"], text)
        for changes in ({"isMeta": True}, {"isSidechain": True},
                        {"sourceToolAssistantUUID": "tool-parent"}, {"userType": "internal"}):
            self.assertNotEqual(source_kind({**invocation, **changes}), "original_user")
        for invalid in (text.replace("/example-command", "/different"),
                        text + "<local-command-stdout>not authority</local-command-stdout>",
                        "<command-name>/model</command-name><command-message>model</command-message><command-args></command-args>"):
            self.assertEqual(source_kind(self.record("invalid", "user", invalid)), "local_command")

    def test_kind_isolation_skips_wrappers_tools_sidechains_and_summaries(self) -> None:
        real = self.record(
            "real-user",
            "user",
            "Proceed with the real task.\n<system-reminder>midturn harness text is not the message prefix.</system-reminder>",
        )
        wrapper = self.record("wrapper", "user", "<system-reminder>runtime only</system-reminder>")
        interrupted = self.record("interrupted", "user", "[Request interrupted, runtime state]")
        command = self.record("command", "user", "<local-command-stdout>status</local-command-stdout>")
        tool = self.record(
            "tool-result",
            "user",
            [
                {"type": "text", "text": "quoted authorization must not win"},
                {"type": "tool_result", "tool_use_id": "run-1", "content": "tool-generated prose"},
            ],
        )
        assistant = self.record("assistant", "assistant", [{"type": "text", "text": "assistant output"}])
        summary = {
            "uuid": "summary",
            "type": "summary",
            "sessionId": self.SID,
            "timestamp": "2026-09-05T12:34:56.000Z",
            "summary": "compact summary",
        }
        sidechain = self.record("sidechain", "user", "sidechain instruction", isSidechain=True)
        self.write_records(real, wrapper, interrupted, command, tool, assistant, summary, sidechain)
        source = self.source()

        self.assertEqual(source_kind(real), "original_user")
        self.assertEqual(source_kind(wrapper), "meta")
        self.assertEqual(source_kind(command), "local_command")
        self.assertEqual(source.locator("tool-result")["source_kind"], "tool_result")
        self.assertEqual(source.locator("summary")["source_kind"], "summary")
        self.assertEqual(source.locator("sidechain")["source_kind"], "sidechain")
        self.assertEqual(source.latest_instruction()["message_id"], "real-user")

    def test_paired_ask_user_question_is_only_verified_when_questions_match(self) -> None:
        questions = [
            {
                "question": "Which execution mode should be used?",
                "header": "Mode",
                "options": [{"label": "Safe", "description": "bounded"}],
                "multiSelect": False,
            }
        ]
        ask = self.record(
            "ask-message",
            "assistant",
            [
                {"type": "text", "text": "I need one answer."},
                {
                    "type": "tool_use",
                    "id": "ask-1",
                    "name": "AskUserQuestion",
                    "input": {"questions": questions},
                },
            ],
        )
        answer = self.record(
            "answer-message",
            "user",
            [{"type": "tool_result", "tool_use_id": "ask-1", "content": "TOOL PROSE must never be returned"}],
            toolUseResult={
                "questions": questions,
                "answers": {"Which execution mode should be used?": "Safe"},
                "annotations": {"transport": "ui"},
            },
        )
        bad_ask = self.record(
            "bad-ask-message",
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "ask-2",
                    "name": "AskUserQuestion",
                    "input": {"questions": questions},
                }
            ],
        )
        mismatch = self.record(
            "mismatch-message",
            "user",
            [{"type": "tool_result", "tool_use_id": "ask-2", "content": "tool prose"}],
            toolUseResult={
                "questions": [{"question": "Different question"}],
                "answers": {"Different question": "Unsafe"},
                "annotations": {},
            },
        )
        self.write_records(ask, answer, bad_ask, mismatch)
        source = self.source()

        answer_locator = source.locator("answer-message")
        self.assertEqual(answer_locator["source_kind"], "verified_user_answer")
        self.assertEqual(source.locator("mismatch-message")["source_kind"], "tool_result")
        self.assertEqual(source.latest_instruction()["message_id"], "answer-message")
        self.assertEqual(source.tool_result("ask-1")["tool_use_id"], "ask-1")

        projection = source.read_answer(answer_locator)
        self.assertIn("Question: Which execution mode should be used?", projection["text"])
        self.assertIn("Answer: Safe", projection["text"])
        self.assertNotIn("TOOL PROSE", projection["text"])
        self.assertEqual(projection["pairing_locator"]["message_id"], "ask-message")
        self.assertNotIn("TOOL PROSE", source.read(answer_locator)["text"])

    def test_projections_omit_thinking_tool_inputs_and_data_and_redact_credentials(self) -> None:
        assistant = self.record(
            "assistant-1",
            "assistant",
            [
                {"type": "thinking", "thinking": "hidden-thinking-secret"},
                {"type": "redacted_thinking", "data": "redacted-thinking-secret"},
                {"type": "image", "source": {"data": "image-data-secret"}},
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Bash",
                    "input": {"command": "arbitrary-tool-input-secret"},
                },
                {
                    "type": "text",
                    "text": "Visible [Doe2024] at 2026-09-05; API_KEY=assigned-token; known-super-secret",
                },
            ],
        )
        self.write_records(assistant)
        source = self.source()

        text = source.read(source.locator("assistant-1"), secrets=("known-super-secret",))["text"]
        self.assertIn("Visible [Doe2024] at 2026-09-05", text)
        self.assertIn("API_KEY=[REDACTED]", text)
        for forbidden in (
            "hidden-thinking-secret",
            "redacted-thinking-secret",
            "image-data-secret",
            "arbitrary-tool-input-secret",
            "assigned-token",
            "known-super-secret",
        ):
            self.assertNotIn(forbidden, text)
        self.assertEqual(redact("token=abc"), "token=[REDACTED]")

    def test_search_pages_public_text_and_returns_readable_bound_locators(self) -> None:
        self.write_records(
            self.record("first", "user", "保留 Straße 比较结果"),
            self.record("skip", "assistant", "无关内容"),
            self.record("second", "assistant", "STRASSE 后续验证"),
        )
        source = self.source()
        first = source.search("strasse", limit=1)
        self.assertEqual(len(first["entries"]), 1)
        locator = first["entries"][0]["locator"]
        self.assertEqual(source.read(locator)["text"], "保留 Straße 比较结果")
        second = source.search("strasse", offset=first["next_offset"])
        self.assertEqual(second["entries"][0]["locator"]["message_id"], "second")
        self.assertIsNone(second["next_offset"])
        limited = source.search("not present", scan_limit=1)
        self.assertEqual(limited["entries"], [])
        self.assertEqual(limited["scanned_records"], 1)
        self.assertIsNotNone(limited["next_offset"])
        for kwargs in ({"query": ""}, {"query": "x", "offset": 1},
                       {"query": "x", "limit": 21}, {"query": "x", "scan_limit": 201}):
            with self.assertRaises(HistoryError):
                source.search(**kwargs)

    def test_search_cannot_match_hidden_text_tool_inputs_or_redacted_secrets(self) -> None:
        self.write_records(self.record("assistant", "assistant", [
            {"type": "thinking", "thinking": "hidden-marker"},
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "input-marker"}},
            {"type": "text", "text": "Visible API_KEY=credential-marker known-marker"},
        ]))
        source = self.source()
        for query in ("hidden-marker", "input-marker", "credential-marker", "known-marker"):
            self.assertEqual(source.search(query, secrets=("known-marker",))["entries"], [])
        hit = source.search("Visible", secrets=("known-marker",))["entries"][0]
        self.assertIn("[REDACTED]", hit["snippet"])
        locator = hit["locator"]
        self.write_records(self.record("assistant", "assistant", "changed"))
        with self.assertRaises(HistoryError):
            source.read(locator)

    def test_saved_output_requires_referenced_confined_nonsymlink_utf8_file(self) -> None:
        output_root = self.work / self.SID / "tool-results"
        output_root.mkdir(parents=True)
        inside = output_root / "result.txt"
        inside.write_text("artifact API_KEY=artifact-secret\nvisible", encoding="utf-8")
        outside = self.work / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        escape = output_root / "escape.txt"
        escape.symlink_to(outside)
        good = self.record(
            "saved-good",
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "save-1",
                    "content": [{"type": "text", "text": f"Saved output: {inside}"}],
                }
            ],
        )
        escaped = self.record(
            "saved-escape",
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "save-2",
                    "content": [{"type": "text", "text": f"Saved output: {escape}"}],
                }
            ],
        )
        self.write_records(good, escaped)
        source = self.source()
        expected_hash = hashlib.sha256(inside.read_bytes()).hexdigest()

        result = source.saved_output(source.tool_result("save-1"), inside, expected_hash)
        self.assertEqual(result["sha256"], expected_hash)
        self.assertIn("API_KEY=[REDACTED]", result["text"])
        self.assertNotIn("artifact-secret", result["text"])
        with self.assertRaises(HistoryError):
            source.saved_output(source.tool_result("save-1"), outside, hashlib.sha256(outside.read_bytes()).hexdigest())
        with self.assertRaises(HistoryError):
            source.saved_output(source.tool_result("save-2"), escape, hashlib.sha256(outside.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
