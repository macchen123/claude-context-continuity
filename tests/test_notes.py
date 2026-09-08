from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity.notes import (  # noqa: E402
    AUTHORITY_NOTICE,
    MAX_NOTE_BYTES,
    NotesConflictError,
    NotesError,
    NotesStore,
)


class NotesStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="notes-fixture-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tempdir.cleanup)
        self.work = Path(self.tempdir.name)
        self.root = self.work / "runtime"
        self.root.mkdir()
        self.store = NotesStore(self.root)

    def test_create_list_read_search_append_and_write(self) -> None:
        first = self.store.write("plan", "first note", None)
        self.assertTrue(first["created"])
        self.assertTrue(first["non_authoritative"])
        self.assertIn("non-authoritative", AUTHORITY_NOTICE)
        self.assertEqual(first["sha256"], hashlib.sha256(b"first note").hexdigest())

        self.store.write("summary", "second note", None)
        listing = self.store.list(0, 1)
        self.assertEqual(listing["total"], 2)
        self.assertEqual([entry["name"] for entry in listing["entries"]], ["plan"])
        self.assertEqual(listing["next_offset"], 1)
        self.assertEqual(self.store.list(listing["next_offset"], 1)["entries"][0]["name"], "summary")

        first_page = self.store.read("plan", 0, 5)
        second_page = self.store.read("plan", first_page["next_start"], 5)
        self.assertEqual(first_page["text"] + second_page["text"], "first note")
        self.assertIsNone(second_page["next_start"])

        appended = self.store.append("plan", " + follow-up", first["sha256"])
        self.assertFalse(appended["created"])
        self.assertEqual(self.store.read("plan")["text"], "first note + follow-up")
        self.assertEqual(
            self.store.read("plan", revision=first["revision"])["text"],
            "first note",
        )

        search = self.store.search("follow-up", 0, 1)
        self.assertEqual(search["entries"][0]["name"], "plan")
        self.assertIn("follow-up", search["entries"][0]["snippet"])

        replacement = self.store.write("plan", "replacement", appended["sha256"])
        self.assertEqual(self.store.read("plan")["text"], "replacement")
        self.assertEqual(replacement["revision"], replacement["sha256"])

    def test_stale_hash_and_explicit_preconditions_are_rejected(self) -> None:
        created = self.store.write("plan", "first", None)
        current = self.store.append("plan", " second", created["sha256"])
        empty_creation = self.store.write("empty", "created with empty precondition", "")
        self.assertTrue(empty_creation["created"])
        with self.assertRaises(NotesConflictError):
            self.store.write("empty", "empty no longer means current", "")

        with self.assertRaises(NotesConflictError):
            self.store.write("plan", "stale", created["sha256"])
        with self.assertRaises(NotesConflictError):
            self.store.append("plan", "missing expected", None)
        with self.assertRaises(NotesConflictError):
            self.store.write("new-note", "wrong creation condition", "0" * 64)
        with self.assertRaises(TypeError):
            self.store.write("missing-argument", "text")  # type: ignore[call-arg]
        with self.assertRaises(NotesError):
            self.store.write("bounded", "x" * (MAX_NOTE_BYTES + 1), None)
        self.assertEqual(self.store.read("plan")["sha256"], current["sha256"])

    def test_traversal_and_symlinks_are_rejected(self) -> None:
        for name in ("", ".", "..", "../outside", "nested/name", "nested\\name", "Upper"):
            with self.subTest(name=name):
                with self.assertRaises(NotesError):
                    self.store.write(name, "text", None)
        self.assertFalse((self.root / "notes").exists())

        self.store.write("safe", "content", None)
        outside = self.work / "outside"
        outside.mkdir()
        os.symlink(outside, self.root / "notes" / "linked")
        with self.assertRaises(NotesError):
            self.store.read("linked")
        with self.assertRaises(NotesError):
            self.store.list()

    def test_secret_redaction_is_persisted_and_used_for_search(self) -> None:
        secret = "known-super-secret"
        created = self.store.write(
            "private",
            "token=abc known-super-secret",
            None,
            secrets=(secret,),
        )
        view = self.store.read("private", secrets=(secret,))
        self.assertNotIn(secret, view["text"])
        self.assertNotIn("abc", view["text"])
        self.assertIn("[REDACTED]", view["text"])

        revision = self.root / "notes" / "private" / "revisions" / f"{created['revision']}.json"
        self.assertNotIn(secret, revision.read_text(encoding="utf-8"))
        self.assertEqual(self.store.search(secret, secrets=(secret,))["entries"], [])

    def test_roots_are_independent(self) -> None:
        other_root = self.work / "other-runtime"
        other_root.mkdir()
        other = NotesStore(other_root)

        self.store.write("same", "first root", None)
        other.write("same", "second root", None)

        self.assertEqual(self.store.read("same")["text"], "first root")
        self.assertEqual(other.read("same")["text"], "second root")
        self.assertNotEqual(self.store.runtime_root, other.runtime_root)

    def test_long_lived_note_reads_only_its_selected_revision(self) -> None:
        from unittest.mock import patch

        first = current = self.store.write("ongoing", "revision 0", None)
        for index in range(1, 66):
            current = self.store.write("ongoing", f"revision {index}", current["sha256"])
        with patch.object(self.store, "_read_regular_bytes", wraps=self.store._read_regular_bytes) as read:
            self.assertEqual(self.store.read("ongoing")["text"], "revision 65")
            self.assertEqual(read.call_count, 2)
        old = self.root / "notes" / "ongoing" / "revisions" / f"{first['revision']}.json"
        old.write_text("{}", encoding="utf-8")
        self.assertEqual(self.store.read("ongoing")["sha256"], current["sha256"])
        with self.assertRaises(NotesError):
            self.store.read("ongoing", revision=first["revision"])

    def test_runtime_ancestor_alias_is_rejected(self) -> None:
        alias = self.work / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        nested = self.root / "nested"
        nested.mkdir()
        with self.assertRaises(NotesError):
            NotesStore(alias / "nested")

    def test_read_validates_revision_and_pointer_bindings(self) -> None:
        created = self.store.write("integrity", "original", None)
        revision = self.root / "notes" / "integrity" / "revisions" / f"{created['revision']}.json"
        revision_value = json.loads(revision.read_text(encoding="utf-8"))
        revision_value["text"] = "tampered"
        revision.write_text(json.dumps(revision_value), encoding="utf-8")
        with self.assertRaises(NotesError):
            self.store.read("integrity")

        bound_root = self.work / "bound-runtime"
        bound_root.mkdir()
        bound = NotesStore(bound_root)
        bound.write("bound", "original", None)
        pointer = bound_root / "notes" / "bound" / "current.json"
        pointer_value = json.loads(pointer.read_text(encoding="utf-8"))
        pointer_value["name"] = "other"
        pointer.write_text(json.dumps(pointer_value), encoding="utf-8")
        with self.assertRaises(NotesError):
            bound.read("bound")


if __name__ == "__main__":
    unittest.main()
