"""Targeted, filesystem-isolated checks for durable native CronCreate compatibility."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import durable_cron  # noqa: E402


class DurableCronTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=TESTS)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.native = self.cwd / ".claude"
        self.native.mkdir()
        self.runtime_directory = self.root / "runtime"
        self.runtime_directory.mkdir()
        self.tasks_path = self.native / "scheduled_tasks.json"
        self.lock_path = self.native / "scheduled_tasks.lock"
        self.pid = os.getpid()
        self.proc_start = "fixture-process-start"
        self.startup_session = str(uuid4())
        self.current_session = str(uuid4())
        self.lock_session = str(uuid4())
        self.task = self._task()
        self.other = {
            "id": "other-task",
            "prompt": "native sibling prompt",
            "cron": "0 3 * * *",
            "recurring": True,
            "createdAt": 2345678901,
            "createdBySessionId": self.current_session,
            "createdByPid": self.pid,
            "createdByProcStart": self.proc_start,
            "lastFiredAt": 444,
            "nativeExtra": {"preserve": True},
        }
        self.write_native([self.task, self.other])

    def _task(self, *, session_id: str | None = None) -> dict:
        return {
            "id": "created-task",
            "prompt": "check the exact durable native task",
            "cron": "*/15 * * * *",
            "recurring": True,
            "createdAt": 1234567890,
            "createdBySessionId": session_id or self.current_session,
            "createdByPid": self.pid,
            "createdByProcStart": self.proc_start,
            "lastFiredAt": 111,
            "nativeExtra": {"retain": ["all", "native", "fields"]},
        }

    def write_native(self, tasks: list[dict], *, lock_pid: int | None = None,
                     lock_proc_start: str | None = None, lock_session: str | None = None) -> None:
        self.tasks_path.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8")
        self.lock_path.write_text(json.dumps({
            "sessionId": lock_session or self.lock_session,
            "pid": self.pid if lock_pid is None else lock_pid,
            "procStart": self.proc_start if lock_proc_start is None else lock_proc_start,
            "acquiredAt": 3456789012,
        }), encoding="utf-8")

    def native_tasks(self) -> dict:
        return json.loads(self.tasks_path.read_text(encoding="utf-8"))

    def state(self, *, current_session: str | None = None, startup_session: str | None = None,
              enabled: bool = True) -> dict:
        return {
            "cwd": str(self.cwd),
            "session_id": current_session or self.current_session,
            "owned_pid": self.pid,
            "tmux": {"pane_pid": self.pid},
            "durable_cron_compat": {
                "enabled": enabled,
                "scheduler_session_id": startup_session or self.startup_session,
            },
        }

    def event(self, *, current_session: str | None = None, durable: bool = True,
              response: dict | None = None) -> dict:
        return {
            "hook_event_name": "PostToolUse",
            "tool_name": "CronCreate",
            "session_id": current_session or self.current_session,
            "tool_input": {
                "prompt": self.task["prompt"],
                "cron": self.task["cron"],
                "recurring": self.task["recurring"],
                "durable": durable,
            },
            "tool_response": response or {"data": {"id": self.task["id"], "durable": True}},
        }

    def receipt_paths(self) -> list[Path]:
        path = self.runtime_directory / "cron_compat"
        return sorted(path.glob("undo-*.json")) if path.exists() else []

    def repair(self, **kwargs) -> dict:
        return durable_cron.repair_created_task(self.state(), self.event(**kwargs), self.runtime_directory)

    def _assert_post_transition(self, source: str) -> None:
        current = str(uuid4())
        self.task = self._task(session_id=current)
        # The native lock may hold either an old or new session; it is not startup authority.
        self.write_native([self.task, self.other], lock_session=current if source == "clear" else self.lock_session)
        response = {"id": self.task["id"], "durable": True} if source == "clear" else None
        outcome = durable_cron.repair_created_task(
            self.state(current_session=current), self.event(current_session=current, response=response), self.runtime_directory)
        self.assertEqual(outcome, {
            "status": "applied", "task_id": "created-task", "reason": "bound_startup_session",
        })
        bound = self.native_tasks()["tasks"][0]
        self.assertEqual(bound["createdBySessionId"], self.startup_session)
        self.assertEqual(bound["createdByPid"], self.pid)
        self.assertEqual(bound["createdByProcStart"], self.proc_start)

    def test_post_clear_binds_exact_native_creator_session(self) -> None:
        self._assert_post_transition("clear")

    def test_post_resume_binds_exact_native_creator_session(self) -> None:
        self._assert_post_transition("resume")

    def test_local_native_durable_session_filter_distinguishes_old_and_new_sessions(self) -> None:
        old = {"createdBySessionId": self.startup_session}
        new = {"createdBySessionId": self.current_session}
        local_native_filter = lambda task: task.get("createdBySessionId") == self.startup_session
        self.assertTrue(local_native_filter(old))
        self.assertFalse(local_native_filter(new))
        self.assertEqual(durable_cron._native_durable_eligible(old, self.startup_session), local_native_filter(old))
        self.assertEqual(durable_cron._native_durable_eligible(new, self.startup_session), local_native_filter(new))

    def test_repair_changes_only_creator_session_and_private_undo_receipt(self) -> None:
        before = copy.deepcopy(self.native_tasks())
        outcome = self.repair()
        self.assertEqual(outcome["status"], "applied")
        after = self.native_tasks()
        expected_task = copy.deepcopy(before["tasks"][0])
        expected_task["createdBySessionId"] = self.startup_session
        self.assertEqual(after["tasks"][0], expected_task)
        self.assertEqual(after["tasks"][1], before["tasks"][1])
        self.assertEqual(after["tasks"][0]["prompt"], before["tasks"][0]["prompt"])
        self.assertEqual(after["tasks"][0]["cron"], before["tasks"][0]["cron"])
        self.assertEqual(after["tasks"][0]["lastFiredAt"], before["tasks"][0]["lastFiredAt"])
        receipts = self.receipt_paths()
        self.assertEqual(len(receipts), 1)
        receipt_text = receipts[0].read_text(encoding="utf-8")
        self.assertNotIn(self.task["prompt"], receipt_text)
        receipt = json.loads(receipt_text)
        self.assertEqual(receipt["original_session_id"], self.current_session)
        self.assertEqual(receipt["bound_session_id"], self.startup_session)
        self.assertEqual(receipt["pid"], self.pid)
        self.assertEqual(receipt["proc_start"], self.proc_start)

    def test_same_startup_session_is_a_noop_without_receipt(self) -> None:
        self.task = self._task(session_id=self.startup_session)
        self.write_native([self.task, self.other])
        before = self.tasks_path.read_bytes()
        outcome = durable_cron.repair_created_task(
            self.state(current_session=self.startup_session, startup_session=self.startup_session),
            self.event(current_session=self.startup_session), self.runtime_directory)
        self.assertEqual(outcome["status"], "not_needed")
        self.assertEqual(outcome["reason"], "already_startup_bound")
        self.assertEqual(self.tasks_path.read_bytes(), before)
        self.assertEqual(self.receipt_paths(), [])

    def test_disabled_and_session_only_calls_leave_native_files_untouched(self) -> None:
        before = self.tasks_path.read_bytes()
        disabled = durable_cron.repair_created_task(self.state(enabled=False), self.event(), self.runtime_directory)
        self.assertEqual(disabled, {"status": "skipped", "task_id": None, "reason": "disabled"})
        session_only = self.repair(durable=False)
        self.assertEqual(session_only["status"], "skipped")
        self.assertEqual(session_only["reason"], "session_only_task")
        non_durable_result = self.repair(response={"id": self.task["id"], "durable": False})
        self.assertEqual(non_durable_result["status"], "skipped")
        self.assertEqual(non_durable_result["reason"], "response_not_durable")
        self.assertEqual(self.tasks_path.read_bytes(), before)
        self.assertEqual(self.receipt_paths(), [])

    def test_native_defaults_and_null_agent_fields_are_supported(self) -> None:
        event = self.event()
        del event["tool_input"]["recurring"]
        event["agent_id"] = None
        outcome = durable_cron.repair_created_task(self.state(), event, self.runtime_directory)
        self.assertEqual(outcome["status"], "applied")
        self.assertTrue(self.native_tasks()["tasks"][0]["recurring"])

    def test_large_native_task_file_is_not_limited_by_handoff_packet_size(self) -> None:
        self.other["prompt"] = "x" * 100_000
        self.write_native([self.task, self.other])
        self.assertGreater(self.tasks_path.stat().st_size, 64 * 1024)
        self.assertEqual(self.repair()["status"], "applied")
        self.assertEqual(self.native_tasks()["tasks"][1], self.other)
        self.tasks_path.write_bytes(b" " * (durable_cron._MAX_JSON_BYTES + 1))
        self.assertEqual(self.repair()["reason"], "oversized_json")

    def test_restore_refuses_a_new_live_scheduler_in_the_same_directory(self) -> None:
        self.assertEqual(self.repair()["status"], "applied")
        lock = json.loads(self.lock_path.read_text())
        lock["pid"] = self.pid + 1
        lock["procStart"] = "new-scheduler-start"
        self.lock_path.write_text(json.dumps(lock))
        before = self.tasks_path.read_bytes()
        def process_status(pid, signal):
            if pid == self.pid:
                raise ProcessLookupError
        with patch("claude_context_continuity.durable_cron.os.kill", side_effect=process_status):
            result = durable_cron.restore_bindings(self.state(), self.runtime_directory)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "native_scheduler_alive")
        self.assertEqual(self.tasks_path.read_bytes(), before)

    def test_duplicate_post_tool_use_is_idempotent(self) -> None:
        first = self.repair()
        second = self.repair()
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second, {
            "status": "not_needed", "task_id": "created-task", "reason": "already_startup_bound",
        })
        self.assertEqual(len(self.receipt_paths()), 1)

    def test_foreign_process_proc_start_and_session_are_skipped(self) -> None:
        cases = {
            "pid": {"createdByPid": self.pid + 1},
            "proc_start": {"createdByProcStart": "foreign-process-start"},
            "session": {"createdBySessionId": str(uuid4())},
        }
        for label, changes in cases.items():
            with self.subTest(label=label):
                task = self._task()
                task.update(changes)
                self.write_native([task, self.other],
                                  lock_pid=task["createdByPid"] if label == "pid" else self.pid,
                                  lock_proc_start=(self.proc_start if label == "proc_start"
                                                   else task["createdByProcStart"]))
                before = self.tasks_path.read_bytes()
                outcome = self.repair()
                self.assertEqual(outcome["status"], "skipped")
                self.assertIn(outcome["reason"], {"process_mismatch", "creator_session_mismatch"})
                self.assertEqual(self.tasks_path.read_bytes(), before)
                self.assertEqual(self.receipt_paths(), [])

    def test_restore_preserves_runtime_last_fired_at_after_owner_exits(self) -> None:
        self.assertEqual(self.repair()["status"], "applied")
        native = self.native_tasks()
        native["tasks"][0]["lastFiredAt"] = 999999
        self.tasks_path.write_text(json.dumps(native), encoding="utf-8")
        with patch("claude_context_continuity.durable_cron.os.kill", side_effect=ProcessLookupError):
            outcome = durable_cron.restore_bindings(self.state(), self.runtime_directory)
        self.assertEqual(outcome, {"status": "applied", "task_id": "created-task", "reason": "restored"})
        restored = self.native_tasks()["tasks"][0]
        self.assertEqual(restored["createdBySessionId"], self.current_session)
        self.assertEqual(restored["lastFiredAt"], 999999)

    def _assert_restore_skips_external_or_cancelled_task(self, case: str) -> None:
        self.assertEqual(self.repair()["status"], "applied")
        native = self.native_tasks()
        if case == "cancelled":
            native["tasks"] = [self.other]
        else:
            native["tasks"][0]["prompt"] = "externally edited native prompt"
        self.tasks_path.write_text(json.dumps(native), encoding="utf-8")
        with patch("claude_context_continuity.durable_cron.os.kill", side_effect=ProcessLookupError):
            outcome = durable_cron.restore_bindings(self.state(), self.runtime_directory)
        self.assertEqual(outcome["status"], "skipped")
        current = self.native_tasks()
        if case == "cancelled":
            self.assertEqual(current["tasks"], [self.other])
        else:
            self.assertEqual(current["tasks"][0]["createdBySessionId"], self.startup_session)

    def test_cancelled_task_is_never_resurrected(self) -> None:
        self._assert_restore_skips_external_or_cancelled_task("cancelled")

    def test_edited_task_is_never_restored(self) -> None:
        self._assert_restore_skips_external_or_cancelled_task("edited")

    def test_restore_refuses_while_owned_process_is_live(self) -> None:
        self.assertEqual(self.repair()["status"], "applied")
        before = self.tasks_path.read_bytes()
        outcome = durable_cron.restore_bindings(self.state(), self.runtime_directory)
        self.assertEqual(outcome["status"], "error")
        self.assertEqual(outcome["reason"], "owned_process_alive")
        self.assertEqual(self.tasks_path.read_bytes(), before)

    def test_changed_native_snapshot_is_not_overwritten(self) -> None:
        original_replace = durable_cron._replace_if_unchanged

        def race(path, snapshot, value, mode):
            if path == self.tasks_path:
                changed = self.native_tasks()
                changed["tasks"][1]["nativeExtra"]["raced"] = True
                self.tasks_path.write_text(json.dumps(changed), encoding="utf-8")
            return original_replace(path, snapshot, value, mode)

        with patch.object(durable_cron, "_replace_if_unchanged", side_effect=race):
            outcome = self.repair()
        self.assertEqual(outcome["status"], "error")
        self.assertEqual(outcome["reason"], "changed_snapshot")
        current = self.native_tasks()
        self.assertEqual(current["tasks"][0]["createdBySessionId"], self.current_session)
        self.assertTrue(current["tasks"][1]["nativeExtra"]["raced"])
        self.assertEqual(len(self.receipt_paths()), 1)  # Safe crash-style receipt remains for a later exact retry.

    def test_symlink_and_malformed_native_inputs_are_rejected_without_parent_mutation(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text('{"tasks":[]}', encoding="utf-8")
        self.tasks_path.unlink()
        self.tasks_path.symlink_to(outside)
        result = self.repair()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "unsafe_path")
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"tasks":[]}')
        self.tasks_path.unlink()
        self.tasks_path.write_text("{ malformed", encoding="utf-8")
        malformed = self.repair()
        self.assertEqual(malformed["status"], "error")
        self.assertEqual(malformed["reason"], "malformed_json")
        self.assertEqual(self.receipt_paths(), [])

    def test_lock_symlink_duplicate_ids_and_ambiguous_response_are_rejected(self) -> None:
        self.lock_path.unlink()
        self.lock_path.symlink_to(self.root / "outside-lock")
        result = self.repair()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "unsafe_path")
        self.lock_path.unlink()
        self.write_native([self.task, copy.deepcopy(self.task)])
        duplicate = self.repair()
        self.assertEqual(duplicate["status"], "error")
        self.assertEqual(duplicate["reason"], "duplicate_task_id")
        self.write_native([self.task, self.other])
        before = self.tasks_path.read_bytes()
        ambiguous = self.repair(response={"id": self.task["id"], "data": {"id": self.task["id"]}})
        self.assertEqual(ambiguous["status"], "skipped")
        self.assertEqual(ambiguous["reason"], "ambiguous_response")
        self.assertEqual(self.tasks_path.read_bytes(), before)

    def test_missing_owner_and_missing_native_parent_do_not_create_any_parent_paths(self) -> None:
        state = self.state()
        del state["owned_pid"]
        missing_owner = durable_cron.repair_created_task(state, self.event(), self.runtime_directory)
        self.assertEqual(missing_owner["status"], "error")
        bare_cwd = self.root / "bare-workspace"
        bare_cwd.mkdir()
        state = self.state()
        state["cwd"] = str(bare_cwd)
        before_children = sorted(child.name for child in self.root.iterdir())
        missing_native = durable_cron.repair_created_task(state, self.event(), self.runtime_directory)
        self.assertEqual(missing_native["status"], "skipped")
        self.assertEqual(missing_native["reason"], "task_file_missing")
        self.assertFalse((bare_cwd / ".claude").exists())
        self.assertFalse((self.runtime_directory / "cron_compat").exists())
        self.assertEqual(sorted(child.name for child in self.root.iterdir()), before_children)


if __name__ == "__main__":
    unittest.main()
