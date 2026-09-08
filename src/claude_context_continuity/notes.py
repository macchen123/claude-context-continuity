"""会话连续性笔记，保存在调用方核验的运行目录中。

笔记只保存工作状态和索引，不是用户授权或研究事实的权威来源。
分页与单次文本大小有界，但不按累计笔记数或修改次数停止长期接续。
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterator

from . import core
from .history import HistoryError, redact


class NotesError(ValueError):
    """The bounded notes store is invalid, incomplete, or unsafe to use."""


class NotesConflictError(NotesError):
    """A write did not name the current content SHA256 exactly."""


class NotesNotFoundError(NotesError):
    """The requested note or immutable revision does not exist."""


AUTHORITY_NOTICE = (
    "Notes are non-authoritative convenience text; never use them to establish "
    "user authorization or research facts."
)
MAX_NOTE_BYTES = 8_000
MAX_PAGE_CHARS = 8_000
MAX_PAGE_ITEMS = 32
MAX_SEARCH_SCAN = 64
MAX_QUERY_BYTES = 512
MAX_SNIPPET_CHARS = 240

_NOTES_DIRECTORY = "notes"
_LOCK_FILE = ".lock"
_POINTER_FILE = "current.json"
_REVISIONS_DIRECTORY = "revisions"
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,63})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_POINTER_KEYS = frozenset({"name", "revision", "sha256"})
_REVISION_KEYS = _POINTER_KEYS | {"text"}


class NotesStore:
    """A small immutable-revision note store rooted at one exact runtime path.

    The caller supplies an already-existing, absolute runtime directory. This
    class never derives a root, scans a project, reads settings or environment
    secrets, or treats note text as authority. Every mutation uses an explicit
    content SHA256 precondition and leaves earlier revision files untouched.
    """

    authority_notice = AUTHORITY_NOTICE

    def __init__(self, runtime_root: str | Path) -> None:
        try:
            root = Path(runtime_root)
        except TypeError as exc:
            raise NotesError("runtime root must be an absolute path") from exc
        if not root.is_absolute():
            raise NotesError("runtime root must be an absolute path")
        self._require_directory(root, "runtime root")
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise NotesError("runtime root cannot be resolved") from exc
        if resolved != root:
            raise NotesError("runtime root must not contain a symlink or path alias")
        self._require_directory(resolved, "runtime root")
        self._root = resolved

    @property
    def runtime_root(self) -> Path:
        """Return the canonical caller-supplied runtime directory."""
        return self._root

    def list(self, offset: int = 0, limit: int = MAX_PAGE_ITEMS) -> dict[str, Any]:
        """Return one bounded page of current note identities."""
        self._offset(offset)
        self._item_limit(limit)
        with self._locked(create=False) as notes_root:
            if notes_root is None:
                return {
                    "entries": [],
                    "next_offset": None,
                    "total": 0,
                    "non_authoritative": True,
                }
            names = self._note_names(notes_root)
            end = min(len(names), offset + limit)
            entries = [self._note_state(notes_root, name)[2] for name in names[offset:end]]
            return {
                "entries": entries,
                "next_offset": end if end < len(names) else None,
                "total": len(names),
                "non_authoritative": True,
            }

    def read(
        self,
        name: str,
        start: int = 0,
        limit: int = MAX_PAGE_CHARS,
        revision: str | None = None,
        *,
        secrets: Any = (),
    ) -> dict[str, Any]:
        """Read a bounded, redacted page of one current or immutable revision."""
        name = self._name(name)
        if revision is not None:
            revision = self._sha256(revision, "revision")
        self._offset(start, label="start")
        self._char_limit(limit)
        secret_values = self._secrets(secrets)
        with self._locked(create=False) as notes_root:
            if notes_root is None:
                raise NotesNotFoundError("note does not exist")
            _, revisions_dir, current, revisions = self._note_state(notes_root, name)
            identity = current if revision is None else self._identity(name, revision)
            if identity["revision"] not in revisions:
                revisions[identity["revision"]] = self._read_revision(revisions_dir, name, identity["revision"])
            text = self._redact(revisions[identity["revision"]], secret_values)
            page, next_start, total = self._text_page(text, start, limit)
            return {
                **identity,
                "text": page,
                "next_start": next_start,
                "total_chars": total,
                "non_authoritative": True,
            }

    def search(
        self,
        query: str,
        offset: int = 0,
        limit: int = MAX_PAGE_ITEMS,
        *,
        scan_limit: int = MAX_SEARCH_SCAN,
        secrets: Any = (),
    ) -> dict[str, Any]:
        """Search redacted current revisions with bounded scan and result pages.

        ``offset`` is the sorted note-name index from which to resume, not a
        result index. ``next_offset`` is therefore always safe to pass back to
        a later call without an unbounded rescan.
        """
        if not isinstance(query, str) or not query:
            raise NotesError("search query must be a non-empty string")
        self._utf8(query, MAX_QUERY_BYTES, "search query")
        self._offset(offset)
        self._item_limit(limit)
        self._scan_limit(scan_limit)
        secret_values = self._secrets(secrets)
        needle = query.casefold()
        with self._locked(create=False) as notes_root:
            if notes_root is None:
                return {
                    "entries": [],
                    "next_offset": None,
                    "scanned_notes": 0,
                    "total": 0,
                    "non_authoritative": True,
                }
            names = self._note_names(notes_root)
            end = min(len(names), offset + scan_limit)
            entries: list[dict[str, Any]] = []
            index = offset
            while index < end:
                _, _, identity, revisions = self._note_state(notes_root, names[index])
                text = self._redact(revisions[identity["revision"]], secret_values)
                index += 1
                if needle not in text.casefold():
                    continue
                entries.append({**identity, "snippet": self._snippet(text)})
                if len(entries) == limit:
                    break
            return {
                "entries": entries,
                "next_offset": index if index < len(names) else None,
                "scanned_notes": index - offset,
                "total": len(names),
                "non_authoritative": True,
            }

    def write(
        self,
        name: str,
        text: str,
        expected_sha256: str | None,
        *,
        secrets: Any = (),
    ) -> dict[str, Any]:
        """Create or replace a current revision under an exact SHA256 condition."""
        return self._commit(name, text, expected_sha256=expected_sha256, append=False, secrets=secrets)

    def append(
        self,
        name: str,
        text: str,
        expected_sha256: str | None,
        *,
        secrets: Any = (),
    ) -> dict[str, Any]:
        """Create or append text under an exact SHA256 condition without mutation."""
        return self._commit(name, text, expected_sha256=expected_sha256, append=True, secrets=secrets)

    def _commit(
        self,
        name: str,
        text: str,
        *,
        expected_sha256: str | None,
        append: bool,
        secrets: Any,
    ) -> dict[str, Any]:
        name = self._name(name)
        expected = self._expected_sha256(expected_sha256)
        secret_values = self._secrets(secrets)
        added = self._redact(self._text(text), secret_values)
        with self._locked(create=True) as notes_root:
            if notes_root is None:  # Defensive: create=True always supplies a root.
                raise NotesError("notes storage was not initialized")
            note_dir = notes_root / name
            if self._lstat(note_dir) is None:
                if expected is not None:
                    raise NotesConflictError("creation requires an empty expected SHA256")
                note_dir, revisions_dir = self._create_note(notes_root, name)
                current: dict[str, str] | None = None
                revisions: dict[str, str] = {}
                created = True
                prior = ""
            else:
                note_dir, revisions_dir, current, revisions = self._note_state(notes_root, name)
                if expected is None:
                    raise NotesConflictError("updating a note requires its current expected SHA256")
                if not hmac.compare_digest(expected, current["sha256"]):
                    raise NotesConflictError("expected SHA256 is stale")
                created = False
                prior = revisions[current["revision"]]

            combined = self._redact(prior, secret_values) + added if append else added
            raw = self._utf8(combined, MAX_NOTE_BYTES, "note text")
            digest = hashlib.sha256(raw).hexdigest()
            if current is not None and current["sha256"] == digest and prior == combined:
                return {**current, "created": False, "non_authoritative": True}

            self._store_revision(revisions_dir, name, digest, raw, revisions)
            identity = self._identity(name, digest)
            self._write_pointer(note_dir, identity)
            _, _, verified, verified_revisions = self._note_state(notes_root, name)
            if verified != identity or verified_revisions[digest] != combined:
                raise NotesError("stored note binding could not be validated")
            return {**identity, "created": created, "non_authoritative": True}

    @staticmethod
    def _identity(name: str, digest: str) -> dict[str, str]:
        return {"name": name, "revision": digest, "sha256": digest}

    @staticmethod
    def _lstat(path: Path) -> os.stat_result | None:
        try:
            return os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise NotesError("cannot inspect notes storage") from exc

    @classmethod
    def _require_directory(cls, path: Path, label: str) -> os.stat_result:
        info = cls._lstat(path)
        if info is None:
            raise NotesError(f"{label} does not exist")
        if stat.S_ISLNK(info.st_mode):
            raise NotesError("symbolic links are not allowed in notes storage")
        if not stat.S_ISDIR(info.st_mode):
            raise NotesError(f"{label} must be a directory")
        return info

    @classmethod
    def _require_regular(cls, path: Path, label: str) -> os.stat_result:
        info = cls._lstat(path)
        if info is None:
            raise NotesError(f"{label} does not exist")
        if stat.S_ISLNK(info.st_mode):
            raise NotesError("symbolic links are not allowed in notes storage")
        if not stat.S_ISREG(info.st_mode):
            raise NotesError(f"{label} must be a regular file")
        return info

    @staticmethod
    def _name(name: str) -> str:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise NotesError("note name must be one lowercase safe component")
        return name

    @staticmethod
    def _sha256(value: str, label: str) -> str:
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise NotesError(f"{label} must be a lowercase SHA256 hex digest")
        return value

    def _expected_sha256(self, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return self._sha256(value, "expected SHA256")

    @staticmethod
    def _offset(value: int, *, label: str = "offset") -> None:
        if type(value) is not int or value < 0:
            raise NotesError(f"{label} must be a non-negative integer")

    @staticmethod
    def _item_limit(value: int) -> None:
        if type(value) is not int or not 1 <= value <= MAX_PAGE_ITEMS:
            raise NotesError(f"limit must be between 1 and {MAX_PAGE_ITEMS}")

    @staticmethod
    def _char_limit(value: int) -> None:
        if type(value) is not int or not 1 <= value <= MAX_PAGE_CHARS:
            raise NotesError(f"limit must be between 1 and {MAX_PAGE_CHARS}")

    @staticmethod
    def _scan_limit(value: int) -> None:
        if type(value) is not int or not 1 <= value <= MAX_SEARCH_SCAN:
            raise NotesError(f"scan_limit must be between 1 and {MAX_SEARCH_SCAN}")

    @staticmethod
    def _utf8(text: str, maximum: int, label: str) -> bytes:
        if not isinstance(text, str):
            raise NotesError(f"{label} must be text")
        try:
            raw = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise NotesError(f"{label} must be valid UTF-8 text") from exc
        if len(raw) > maximum:
            raise NotesError(f"{label} exceeds its {maximum}-byte bound")
        return raw

    def _text(self, text: str) -> str:
        self._utf8(text, MAX_NOTE_BYTES, "note text")
        return text

    @staticmethod
    def _secrets(secrets: Any) -> tuple[str, ...]:
        if secrets is None:
            return ()
        if isinstance(secrets, str):
            return (secrets,)
        try:
            values = tuple(secrets)
        except TypeError as exc:
            raise NotesError("secrets must be an iterable of strings") from exc
        if any(not isinstance(value, str) for value in values):
            raise NotesError("secrets must be an iterable of strings")
        return values

    def _redact(self, text: str, secrets: tuple[str, ...]) -> str:
        try:
            redacted = redact(text, secrets)
        except HistoryError as exc:
            raise NotesError("secrets must be an iterable of strings") from exc
        self._utf8(redacted, MAX_NOTE_BYTES, "redacted note text")
        return redacted

    @staticmethod
    def _text_page(text: str, start: int, limit: int) -> tuple[str, int | None, int]:
        total = len(text)
        if start >= total:
            return "", None, total
        end = min(total, start + limit)
        return text[start:end], end if end < total else None, total

    @staticmethod
    def _snippet(text: str) -> str:
        return text[:MAX_SNIPPET_CHARS]

    def _notes_root(self, *, create: bool) -> tuple[Path | None, bool]:
        self._require_directory(self._root, "runtime root")
        notes_root = self._root / _NOTES_DIRECTORY
        info = self._lstat(notes_root)
        if info is None:
            if not create:
                return None, False
            try:
                os.mkdir(notes_root, 0o700)
            except FileExistsError as exc:
                raise NotesError("notes storage appeared during initialization") from exc
            except OSError as exc:
                raise NotesError("cannot create notes storage") from exc
            self._require_directory(notes_root, "notes storage")
            return notes_root, True
        if stat.S_ISLNK(info.st_mode):
            raise NotesError("symbolic links are not allowed in notes storage")
        if not stat.S_ISDIR(info.st_mode):
            raise NotesError("notes storage must be a directory")
        return notes_root, False

    @contextmanager
    def _locked(self, *, create: bool) -> Iterator[Path | None]:
        """Use the continuity core's non-retrying OS lock for every store access."""
        notes_root, created = self._notes_root(create=create)
        if notes_root is None:
            yield None
            return
        lock_path = notes_root / _LOCK_FILE
        if created:
            if self._lstat(lock_path) is not None:
                raise NotesError("notes lock state is ambiguous")
        else:
            self._require_regular(lock_path, "notes lock")
        try:
            with core.lock(lock_path, wait_seconds=0):
                self._require_regular(lock_path, "notes lock")
                yield notes_root
        except core.ContinuityError as exc:
            raise NotesError("notes storage is busy or unsafe") from exc

    def _note_names(self, notes_root: Path) -> list[str]:
        names: list[str] = []
        try:
            with os.scandir(notes_root) as entries:
                for entry in entries:
                    path = notes_root / entry.name
                    if entry.name == _LOCK_FILE:
                        self._require_regular(path, "notes lock")
                        continue
                    self._name(entry.name)
                    self._require_directory(path, "note directory")
                    names.append(entry.name)
        except OSError as exc:
            raise NotesError("cannot enumerate notes storage") from exc
        return sorted(names)

    def _note_state(self, notes_root: Path, name: str) -> tuple[Path, Path, dict[str, str], dict[str, str]]:
        note_dir = notes_root / name
        self._require_directory(note_dir, "note directory")
        revisions_dir: Path | None = None
        pointer: Path | None = None
        try:
            with os.scandir(note_dir) as entries:
                for entry in entries:
                    path = note_dir / entry.name
                    if entry.name == _REVISIONS_DIRECTORY:
                        self._require_directory(path, "revisions directory")
                        revisions_dir = path
                    elif entry.name == _POINTER_FILE:
                        self._require_regular(path, "current note pointer")
                        pointer = path
                    else:
                        raise NotesError("note state contains an unexpected entry")
        except OSError as exc:
            raise NotesError("cannot inspect note state") from exc
        if revisions_dir is None or pointer is None:
            raise NotesError("note state is incomplete")
        identity = self._read_pointer(pointer, name)
        revisions = {identity["revision"]: self._read_revision(revisions_dir, name, identity["revision"])}
        return note_dir, revisions_dir, identity, revisions

    def _read_pointer(self, path: Path, name: str) -> dict[str, str]:
        value = self._json(self._read_regular_bytes(path, MAX_QUERY_BYTES), "current note pointer")
        if not isinstance(value, dict) or set(value) != _POINTER_KEYS:
            raise NotesError("current note pointer has an invalid binding")
        pointer_name = value["name"]
        revision = value["revision"]
        digest = value["sha256"]
        if not isinstance(pointer_name, str) or pointer_name != name:
            raise NotesError("current note pointer name binding is invalid")
        self._name(pointer_name)
        self._sha256(revision, "pointer revision")
        self._sha256(digest, "pointer SHA256")
        if revision != digest:
            raise NotesError("current note pointer SHA256 binding is invalid")
        return self._identity(name, revision)

    @staticmethod
    def _json(raw: bytes, label: str) -> Any:
        try:
            return json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                object_pairs_hook=NotesStore._unique_object,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise NotesError(f"{label} is invalid") from exc

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def _read_revision(self, revisions_dir: Path, name: str, revision: str) -> str:
        """只核验被读取的版本；旧版本损坏不使无依赖的当前版本失效。"""
        self._sha256(revision, "revision identity")
        path = revisions_dir / f"{revision}.json"
        if self._lstat(path) is None:
            raise NotesNotFoundError("note revision does not exist")
        value = self._json(self._read_regular_bytes(path, core.MAX_PACKET), "revision file")
        if not isinstance(value, dict) or set(value) != _REVISION_KEYS:
            raise NotesError("revision file has an invalid binding")
        if {key: value[key] for key in _POINTER_KEYS} != self._identity(name, revision):
            raise NotesError("revision name or SHA256 binding is invalid")
        raw = self._utf8(value["text"], MAX_NOTE_BYTES, "revision text")
        if hashlib.sha256(raw).hexdigest() != revision:
            raise NotesError("revision content SHA256 does not match its identity")
        return value["text"]

    def _read_regular_bytes(self, path: Path, maximum: int) -> bytes:
        self._require_regular(path, "notes storage file")
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise NotesError("safe notes storage requires O_NOFOLLOW")
        try:
            fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            raise NotesError("cannot safely open notes storage") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise NotesError("notes storage path must be a regular file")
            if os.fstat(fd).st_size > maximum:
                raise NotesError("notes storage file exceeds its bound")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        except OSError as exc:
            raise NotesError("cannot read notes storage") from exc
        finally:
            os.close(fd)
        if len(raw) > maximum:
            raise NotesError("notes storage file exceeds its bound")
        return raw

    def _create_note(self, notes_root: Path, name: str) -> tuple[Path, Path]:
        note_dir = notes_root / name
        if self._lstat(note_dir) is not None:
            raise NotesError("note state appeared during creation")
        try:
            os.mkdir(note_dir, 0o700)
            revisions_dir = note_dir / _REVISIONS_DIRECTORY
            os.mkdir(revisions_dir, 0o700)
        except OSError as exc:
            raise NotesError("cannot create note storage") from exc
        self._require_directory(note_dir, "note directory")
        self._require_directory(revisions_dir, "revisions directory")
        return note_dir, revisions_dir

    def _store_revision(
        self,
        revisions_dir: Path,
        name: str,
        digest: str,
        raw: bytes,
        revisions: dict[str, str],
    ) -> None:
        path = revisions_dir / f"{digest}.json"
        if digest in revisions:
            if self._utf8(revisions[digest], MAX_NOTE_BYTES, "stored revision") != raw:
                raise NotesError("existing revision does not match its content identity")
            return
        if self._lstat(path) is not None:
            if self._read_revision(revisions_dir, name, digest).encode("utf-8") != raw:
                raise NotesError("existing revision does not match its content identity")
            return
        value = {**self._identity(name, digest), "text": raw.decode("utf-8")}
        try:
            core.atomic(path, value, exclusive=True)
        except FileExistsError as exc:
            raise NotesError("revision state appeared during write") from exc
        except (core.ContinuityError, OSError, TypeError, ValueError) as exc:
            raise NotesError("cannot persist immutable note revision") from exc

    def _write_pointer(self, note_dir: Path, identity: dict[str, str]) -> None:
        pointer = note_dir / _POINTER_FILE
        if self._lstat(pointer) is not None:
            self._require_regular(pointer, "current note pointer")
        try:
            core.atomic(pointer, identity)
        except (core.ContinuityError, OSError, TypeError, ValueError) as exc:
            raise NotesError("cannot update current note pointer") from exc


__all__ = [
    "AUTHORITY_NOTICE",
    "MAX_NOTE_BYTES",
    "MAX_PAGE_CHARS",
    "MAX_PAGE_ITEMS",
    "MAX_SEARCH_SCAN",
    "NotesConflictError",
    "NotesError",
    "NotesNotFoundError",
    "NotesStore",
]
