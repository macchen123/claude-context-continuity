"""Native loader path/identity/integrity contracts; synthetic histories only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from claude_context_continuity import agent_history, core


class AgentHistoryTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".cache" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch, prefix="agent-history-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.old, self.new = str(uuid4()), str(uuid4())
        self.agent = "a1234567890abcdef"
        self.base = self.project / self.old / "subagents"
        self.base.mkdir(parents=True)
        self.source = self.base / f"agent-{self.agent}.jsonl"
        self.source.write_bytes(self.row())
        self.relative = Path(f"agent-{self.agent}.meta.json")
        self.record = {"project": self.project, "path": self.base / self.relative, "relative": self.relative}
        self.record["path"].write_text(json.dumps({"agentType": "worker"}))
        self.target = self.project / self.new / "subagents"
        self.destination = self.target / self.source.name

    def row(self, *, sid=None, agent=None, text="fixture"):
        return (json.dumps({"type": "assistant", "agentId": agent or self.agent, "sessionId": sid or self.old,
            "isSidechain": True, "uuid": str(uuid4()), "message": {"role": "assistant", "content": text}}) + "\n").encode()

    def establish(self, records=None, sid=None):
        return agent_history.establish(self.agent, records or [self.record], (self.project, self.target),
                                       {self.old, self.new, *([sid] if sid else [])}, self.root / "runtime")

    def test_one_stream_remains_live_across_two_rotations(self):
        before = self.source.read_bytes()
        first = self.establish()
        self.assertEqual(first["status"], "linked")
        self.assertTrue(os.path.samefile(self.source, self.destination))
        with self.source.open("ab") as stream:
            stream.write(self.row(text="native append"))
        self.assertEqual(self.source.read_bytes(), self.destination.read_bytes())
        third = str(uuid4())
        self.record = {**self.record, "project": self.project, "path": self.target / self.relative}
        self.target = self.project / third / "subagents"
        self.destination = self.target / self.source.name
        self.establish(sid=third)
        self.assertTrue(os.path.samefile(self.source, self.destination))
        self.assertTrue(self.source.read_bytes().startswith(before))
        modified = self.destination.stat().st_mtime_ns
        self.establish(sid=third)
        self.assertEqual(self.destination.stat().st_mtime_ns, modified)
        self.assertEqual(len(list((self.root / "runtime/agent_history").glob("*/applied.json"))), 2)

    def test_current_native_stream_does_not_require_publication_snapshot(self):
        self.target, self.destination = self.base, self.source
        valid = self.source.read_bytes()
        for tail in (b'{"type":', self.row(text="x" * (agent_history._MAX_LINE + 1))):
            with self.subTest(tail_bytes=len(tail)):
                self.source.write_bytes(valid + tail)
                with patch.object(agent_history, "_snapshot", side_effect=AssertionError("not publishing")):
                    self.assertEqual(self.establish()["status"], "available")

    def test_established_live_stream_tolerates_partial_and_large_appends(self):
        self.establish()
        valid = self.source.read_bytes()
        for tail in (b'{"type":', self.row(text="x" * (agent_history._MAX_LINE + 1))):
            with self.subTest(tail_bytes=len(tail)):
                self.source.write_bytes(valid + tail)
                with patch.object(agent_history, "_snapshot", side_effect=AssertionError("not publishing")):
                    self.assertEqual(self.establish()["status"], "available")
                self.assertTrue(os.path.samefile(self.source, self.destination))

    def test_initial_publication_still_rejects_large_record(self):
        self.source.write_bytes(self.row(text="x" * (agent_history._MAX_LINE + 1)))
        with self.assertRaisesRegex(ValueError, "incomplete_or_oversized"):
            self.establish()
        self.assertFalse(self.destination.exists())

    def test_missing_empty_partial_malformed_foreign_history_fails_closed(self):
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, "original_agent_history_missing"):
            self.establish()
        for raw in (b"", b"{\"type\":", b"not-json\n", self.row(agent="a99999999"), self.row(sid=str(uuid4()))):
            with self.subTest(raw=raw[:50]):
                self.source.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.establish()
                self.assertFalse(self.destination.exists())

    def test_incomplete_native_append_can_settle_then_restore(self):
        raw = self.row()
        self.source.write_bytes(raw[:-1])
        with self.assertRaisesRegex(ValueError, "incomplete_or_oversized"):
            self.establish()
        self.source.write_bytes(raw)
        self.assertEqual(self.establish()["status"], "linked")

    def test_distinct_even_identical_streams_are_not_merged_or_replaced(self):
        self.target.mkdir(parents=True)
        self.destination.write_bytes(self.source.read_bytes())
        before = self.destination.stat()
        with self.assertRaisesRegex(ValueError, "destination_agent_history_conflict"):
            self.establish()
        self.assertEqual(self.destination.stat(), before)
        second = {**self.record, "path": self.target / self.relative}
        with self.assertRaisesRegex(ValueError, "divergent_agent_histories"):
            self.establish(records=[self.record, second])

    def fragment(self, *, parent=None, competing=False):
        root, bridge, end = str(uuid4()), str(uuid4()), str(uuid4())
        def row(kind, uid, previous, text):
            return (json.dumps({"type": kind, "uuid": uid, "parentUuid": previous,
                "agentId": self.agent, "sessionId": self.old, "isSidechain": True,
                "message": {"role": kind, "content": text}}) + "\n").encode()
        original = row("user", root, None, "original prompt") + row("assistant", bridge, root, "original tool result")
        if competing:
            original += row("user", str(uuid4()), bridge, "competing branch")
        suffix = row("attachment", str(uuid4()), parent or bridge, "native attachment")
        suffix += row("user", end, parent or bridge, "resume message")
        self.source.write_bytes(original)
        self.target.mkdir(parents=True, exist_ok=True)
        self.destination.write_bytes(suffix)
        second = {**self.record, "path": self.target / self.relative}
        return [self.record, second], original, suffix

    def test_native_fragments_publish_complete_history_and_preserve_originals(self):
        records, original, suffix = self.fragment()
        old_identity = self.destination.stat().st_ino
        result = self.establish(records=records)
        self.assertEqual(result["status"], "reconstructed")
        self.assertEqual(self.destination.read_bytes(), original + suffix)
        self.assertEqual(self.source.read_bytes(), original)
        receipt = core.read_json(result["receipt"])
        self.assertEqual([Path(row["preserved_path"]).read_bytes() for row in receipt["sources"]], [original, suffix])
        self.assertNotEqual(self.destination.stat().st_ino, old_identity)
        before = self.destination.stat()
        self.assertEqual(self.establish(records=records)["status"], "available")
        self.assertEqual(self.destination.stat(), before)
        # The repaired stream is native-writer-owned, even while appending a partial/large record.
        for tail in (b'{"type":', self.row(text="x" * (agent_history._MAX_LINE + 1))):
            self.destination.write_bytes(original + suffix + tail)
            self.assertEqual(self.establish(records=records)["status"], "available")

    def test_contained_suffix_does_not_hide_missing_predecessor(self):
        rows = [json.loads(self.row()), json.loads(self.row())]
        rows[0]["parentUuid"] = str(uuid4())
        rows[1]["parentUuid"] = rows[0]["uuid"]
        original = b"".join((json.dumps(row) + "\n").encode() for row in rows)
        suffix = (json.dumps(rows[1]) + "\n").encode()
        self.source.write_bytes(original)
        self.target.mkdir(parents=True)
        self.destination.write_bytes(suffix)
        second = {**self.record, "path": self.target / self.relative}
        before = self.destination.stat()
        with self.assertRaisesRegex(ValueError, "missing_agent_history_predecessor"):
            self.establish(records=[self.record, second])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.destination.read_bytes(), suffix)
        self.assertEqual(self.destination.stat(), before)

    def test_missing_predecessor_and_competing_branch_do_not_publish(self):
        for missing, competing in ((True, False), (False, True)):
            with self.subTest(missing=missing, competing=competing):
                records, original, suffix = self.fragment(parent=str(uuid4()) if missing else None, competing=competing)
                before = self.destination.stat()
                with self.assertRaisesRegex(ValueError, "missing_agent_history_predecessor|divergent_agent_histories"):
                    self.establish(records=records)
                self.assertEqual(self.source.read_bytes(), original)
                self.assertEqual(self.destination.read_bytes(), suffix)
                self.assertEqual(self.destination.stat(), before)

    def test_fragment_recovery_survives_another_rotation(self):
        records, original, suffix = self.fragment()
        complete = self.establish(records=records)
        third = str(uuid4())
        self.target = self.project / third / "subagents"
        self.destination = self.target / self.source.name
        result = self.establish(records=records, sid=third)
        self.assertTrue(os.path.samefile(complete["path"], result["path"]))
        self.assertEqual(self.destination.read_bytes(), original + suffix)

    def test_current_suffix_alias_is_replaced_without_changing_earlier_suffix(self):
        records, original, suffix = self.fragment()
        suffix_path = self.destination
        third = str(uuid4())
        self.target = self.project / third / "subagents"
        self.target.mkdir(parents=True)
        self.destination = self.target / self.source.name
        os.link(suffix_path, self.destination)
        current_record = {**self.record, "path": self.target / self.relative}
        result = self.establish(records=[*records, current_record], sid=third)
        self.assertEqual(result["status"], "reconstructed")
        self.assertEqual(self.destination.read_bytes(), original + suffix)
        self.assertEqual(suffix_path.read_bytes(), suffix)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(os.path.samefile(suffix_path, self.destination))
        with self.destination.open("ab") as stream:
            stream.write(b'{"type":')
        self.assertEqual(self.establish(records=[*records, current_record], sid=third)["status"], "available")

    def test_fragment_chain_does_not_replace_unregistered_destination(self):
        records, original, suffix = self.fragment()
        third = str(uuid4())
        self.target = self.project / third / "subagents"
        self.target.mkdir(parents=True)
        self.destination = self.target / self.source.name
        foreign = self.row(text="unregistered stream")
        self.destination.write_bytes(foreign)
        before = self.destination.stat()
        with self.assertRaisesRegex(ValueError, "destination_agent_history_conflict"):
            self.establish(records=records, sid=third)
        self.assertEqual(self.destination.stat(), before)
        self.assertEqual(self.destination.read_bytes(), foreign)
        self.assertEqual(self.source.read_bytes(), original)

    def test_fragment_source_change_before_publication_does_not_replace_destination(self):
        records, original, suffix = self.fragment()
        atomic = core.atomic
        def change_source(path, value, **kwargs):
            result = atomic(path, value, **kwargs)
            if Path(path).name == "intent.json":
                with self.source.open("ab") as stream:
                    stream.write(self.row(text="native append"))
            return result
        before = self.destination.stat()
        with patch.object(core, "atomic", side_effect=change_source):
            with self.assertRaisesRegex(ValueError, "agent_history_changed_before_publish"):
                self.establish(records=records)
        self.assertEqual(self.destination.read_bytes(), suffix)
        self.assertEqual(self.destination.stat().st_ino, before.st_ino)
        self.assertTrue(self.source.read_bytes().startswith(original))

    def test_fragment_publication_receipt_interruption_recovers(self):
        records, original, suffix = self.fragment()
        atomic = core.atomic
        def fail_receipt(path, value, **kwargs):
            if Path(path).name == "applied.json":
                raise OSError("fixture receipt interruption")
            return atomic(path, value, **kwargs)
        with patch.object(core, "atomic", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.establish(records=records)
        self.assertEqual(self.destination.read_bytes(), original + suffix)
        self.establish(records=records)
        self.assertEqual(self.destination.read_bytes(), original + suffix)
        self.assertEqual(self.source.read_bytes(), original)

    def test_fragment_chain_order_is_native_not_catalogue_order(self):
        records, original, suffix = self.fragment()
        third = self.project / str(uuid4()) / "subagents"
        third.mkdir(parents=True)
        last_parent = json.loads(suffix.splitlines()[-1])["uuid"]
        final = (json.dumps({"type": "assistant", "uuid": str(uuid4()), "parentUuid": last_parent,
                 "agentId": self.agent, "sessionId": self.old, "isSidechain": True}) + "\n").encode()
        (third / self.source.name).write_bytes(final)
        third_record = {**self.record, "path": third / self.relative}
        result = self.establish(records=[third_record, *reversed(records)])
        self.assertEqual(Path(result["path"]).read_bytes(), original + suffix + final)

    def test_two_continuations_of_same_predecessor_are_a_branch(self):
        records, original, suffix = self.fragment()
        third = self.project / str(uuid4()) / "subagents"
        third.mkdir(parents=True)
        branch = json.loads(suffix.splitlines()[-1])
        branch["uuid"] = str(uuid4())
        (third / self.source.name).write_text(json.dumps(branch) + "\n")
        third_record = {**self.record, "path": third / self.relative}
        with self.assertRaisesRegex(ValueError, "divergent_agent_histories"):
            self.establish(records=[*records, third_record])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.destination.read_bytes(), suffix)

    def test_symlink_sources_and_destinations_are_not_followed(self):
        original = self.source.with_suffix(".original")
        self.source.rename(original)
        self.source.symlink_to(original)
        with self.assertRaises(core.ContinuityError):
            self.establish()
        self.source.unlink()
        original.rename(self.source)
        self.target.mkdir(parents=True)
        self.destination.symlink_to(self.source)
        with self.assertRaises(core.ContinuityError):
            self.establish()
        self.assertTrue(self.destination.is_symlink())

    def test_competing_destination_is_never_overwritten(self):
        native_link = os.link
        foreign = self.row(agent="a99999999")
        def compete(source, destination, **kwargs):
            if Path(destination) == self.destination:
                Path(destination).write_bytes(foreign)
            return native_link(source, destination, **kwargs)
        with patch.object(agent_history.os, "link", side_effect=compete):
            with self.assertRaisesRegex(ValueError, "concurrent_agent_history_publication"):
                self.establish()
        self.assertEqual(self.destination.read_bytes(), foreign)
        self.assertFalse(list((self.root / "runtime/agent_history").glob("*/applied.json")))

    def test_source_change_before_link_does_not_publish_history(self):
        atomic = core.atomic
        def change_source(path, value, **kwargs):
            result = atomic(path, value, **kwargs)
            if Path(path).name == "intent.json":
                self.source.write_bytes(self.row(text="concurrent rewrite"))
            return result
        with patch.object(core, "atomic", side_effect=change_source):
            with self.assertRaisesRegex(ValueError, "agent_history_changed_before_publish"):
                self.establish()
        self.assertFalse(self.destination.exists())

    def test_unsupported_links_fail_without_snapshot_fallback(self):
        native_link = os.link
        def unsupported(source, destination, **kwargs):
            if Path(destination) == self.destination:
                raise OSError("fixture filesystem")
            return native_link(source, destination, **kwargs)
        with patch.object(agent_history.os, "link", side_effect=unsupported):
            with self.assertRaisesRegex(ValueError, "native_history_link_unavailable"):
                self.establish()
        self.assertFalse(self.destination.exists())

    def test_receipt_failure_after_link_recovers_without_republication(self):
        atomic = core.atomic
        def fail_receipt(path, value, **kwargs):
            if Path(path).name == "applied.json":
                raise OSError("fixture receipt interruption")
            return atomic(path, value, **kwargs)
        with patch.object(core, "atomic", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.establish()
        with patch.object(agent_history.os, "link") as link:
            self.assertEqual(self.establish()["status"], "available")
            link.assert_not_called()
        self.assertTrue(os.path.samefile(self.source, self.destination))

    def test_catalogued_source_in_another_project_root_and_nested_path(self):
        nested = Path("nested") / self.relative
        old = self.record["path"]
        self.record = {**self.record, "relative": nested, "path": self.base / nested}
        self.record["path"].parent.mkdir()
        old.rename(self.record["path"])
        self.source.rename(self.record["path"].with_name(self.source.name))
        second_project = self.root / "new-project"
        second_project.mkdir()
        self.project = second_project
        self.target = self.project / self.new / "subagents"
        result = self.establish()
        self.assertTrue(Path(result["path"]).is_relative_to(self.target / "nested"))


if __name__ == "__main__":
    unittest.main()
