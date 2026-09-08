"""Private, reconstructible APSW/FTS5 index over explicit native histories.

The cache stores only HistorySource's redacted public projection.  Native JSONL
records remain the authority for exact reads; this module never writes them and
never persists tool inputs, thinking blocks, image data, or other raw records.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Iterator
from uuid import UUID

import apsw

from . import core
from .history import HistoryError, HistorySource


class HistoryIndexError(HistoryError):
    """The private cross-window history index request is invalid or unsafe."""


class HistoryIndexCacheError(HistoryIndexError):
    """The APSW cache cannot be safely opened, read, or updated."""


class HistoryIndexParserError(HistoryIndexError):
    """A selected canonical HistorySource could not produce its public projection."""


class HistoryIndexStaleError(HistoryIndexError):
    """A source or pagination cursor changed and the caller must restart."""


SCHEMA_VERSION = 1
MAX_PAGE_ITEMS = 20
MAX_FILTER_VALUES = 256
MAX_QUERY_CHARS = 8_000
MAX_QUERY_BYTES = 32 * 1024
MAX_TOOL_CHARS = 512
MAX_CURSOR_BYTES = 4 * 1024

# A private namespace marker for a reconstructible cache, not task-state data.
_APPLICATION_ID = 0x43434958
_REDACTION_POLICY_VERSION = 1
_CURSOR_VERSION = 1
_MAX_SQLITE_INT = (1 << 63) - 1
_SOURCE_KINDS = frozenset({
    "assistant",
    "local_command",
    "meta",
    "original_user",
    "sidechain",
    "summary",
    "tool_result",
    "verified_user_answer",
})


@dataclass(frozen=True)
class _Signature:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def values(self) -> tuple[int, int, int, int, int]:
        return (self.dev, self.ino, self.size, self.mtime_ns, self.ctime_ns)


@dataclass(frozen=True)
class _State:
    signature: _Signature
    deferred_tail: bool


@dataclass(frozen=True)
class _SourceSpec:
    source_path: str
    session_id: str
    generation: int


@dataclass(frozen=True)
class _BoundSource:
    spec: _SourceSpec
    source: HistorySource

    @property
    def key(self) -> tuple[str, str]:
        return (str(self.source.path), self.spec.session_id)

    @property
    def generation(self) -> int:
        return self.spec.generation


@dataclass(frozen=True)
class _ProjectedRecord:
    source_path: str
    session_id: str
    message_id: str
    start_byte: int
    end_byte: int
    sha256: str
    source_kind: str
    timestamp: str | None
    text: str
    text_folded: str
    total_chars: int
    tools: tuple[tuple[str, str | None], ...]
    projection_hash: str


def _serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        # 刷新、查询和脱敏清理必须属于同一次缓存访问。
        with self._cache_lock():
            try:
                return method(self, *args, **kwargs)
            except apsw.Error as exc:
                raise HistoryIndexCacheError("private history index operation failed") from exc
    return guarded


class HistoryIndex:
    """A bounded APSW cache scoped to one caller-supplied private path.

    ``sources`` is always explicit.  It is not a filesystem discovery API and
    never scans a parent directory for histories.  Callers pass the complete
    context catalogue on every request; optional ``windows`` and ``sessions``
    filters select result bindings without evicting other cached windows.
    """

    def __init__(self, path: Path) -> None:
        self._path, existed = self._prepare_cache_path(path)
        self._db: apsw.Connection | None = None
        self._closed = False
        try:
            with self._cache_lock():
                # 等待锁期间另一进程可能已经创建缓存。
                existed = self._path.exists()
                self._db = apsw.Connection(str(self._path))
                self._db.setbusytimeout(1_000)
                self._connection().execute("PRAGMA foreign_keys=ON")
                self._connection().execute("PRAGMA trusted_schema=OFF")
                if existed:
                    self._validate_schema()
                else:
                    self._create_schema()
                self._configure_owned_cache()
                self._verify_cache_target()
                self._sqlite_version = str(self._scalar("SELECT sqlite_version()"))
        except HistoryIndexError:
            self._close_quietly()
            raise
        except (apsw.Error, OSError) as exc:
            self._close_quietly()
            raise HistoryIndexCacheError("cannot initialize the private history index cache") from exc

    @property
    def path(self) -> Path:
        """Return the exact private SQLite cache path."""
        return self._path

    def __enter__(self) -> "HistoryIndex":
        self._connection()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the APSW connection; calling close more than once is harmless."""
        if self._closed:
            return
        connection, self._db, self._closed = self._db, None, True
        if connection is None:
            return
        try:
            connection.close()
        except apsw.Error as exc:
            raise HistoryIndexCacheError("cannot close the private history index cache") from exc

    @contextmanager
    def _cache_lock(self):
        self._ensure_private_parent(self._path.parent)
        try:
            with core.lock(self._path.with_name(self._path.name + ".lock"), wait_seconds=1):
                yield
        except (core.ContinuityError, OSError) as exc:
            raise HistoryIndexCacheError("private history index cache is busy or unavailable") from exc

    @_serialized
    def search(
        self,
        sources: list[dict[str, Any]],
        query: str | None,
        *,
        source_kinds: Iterable[str] = (),
        tool: str | None = None,
        recent_first: bool = False,
        limit: int = 10,
        cursor: str | None = None,
        secrets: Any = (),
        windows: Iterable[int] = (),
        sessions: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Search or browse redacted public projections in deterministic order.

        ``query=None`` browses the selected projection.  Any string query is a
        Python-casefolded literal substring; it is never interpreted as FTS,
        wildcard, regular-expression, or boolean syntax.
        """
        self._connection()
        specs = self._source_specs(sources)
        needle = self._query(query)
        kinds = self._source_kinds(source_kinds)
        tool_value = self._tool(tool)
        recent = self._recent_first(recent_first)
        page_limit = self._limit(limit)
        selected_windows = self._windows(windows)
        selected_sessions = self._sessions(sessions)
        secret_values, redaction_policy = self._redaction_policy(secrets)
        request_digest = self._request_digest(
            needle,
            kinds,
            tool_value,
            recent,
            selected_windows,
            selected_sessions,
        )

        selected_specs = tuple(
            spec for spec in specs
            if (not selected_windows or spec.generation in selected_windows)
            and (not selected_sessions or spec.session_id in selected_sessions)
        )
        if not selected_specs:
            if cursor is not None:
                self._decode_cursor(cursor)
                raise HistoryIndexStaleError("selected history sources changed; restart the search")
            refresh = self._refresh_without_selected_sources(redaction_policy)
            return self._result([], None, refresh)

        try:
            # 只打开实际选择的窗口；其他已登记但失效的文件不能阻断过滤后的查询。
            selected = self._bound_sources(selected_specs)
            current_keys = {(str(Path(spec.source_path).expanduser().resolve(strict=False)), spec.session_id)
                            for spec in specs}
            refresh = self._refresh(current_keys, selected, secret_values, redaction_policy)
            states = self._validate_selected_snapshot(selected)
            snapshot = self._snapshot_digest(selected, states, redaction_policy)
            position = self._cursor_position(cursor, request_digest, snapshot)
            rows = self._rows_for_page(
                selected,
                needle,
                kinds,
                tool_value,
                recent,
                position,
                page_limit + 1,
            )
            page = rows[:page_limit]
            next_cursor = (
                self._encode_cursor(request_digest, snapshot, position + len(page))
                if len(rows) > page_limit else None
            )
            return self._result(self._entries(page, needle), next_cursor, refresh)
        except HistoryIndexError:
            raise
        except apsw.Error as exc:
            raise HistoryIndexCacheError("private history index query failed") from exc
        except OSError as exc:
            raise HistoryIndexCacheError("private history index filesystem check failed") from exc

    # -- input validation -------------------------------------------------

    @staticmethod
    def _utf8(value: str, maximum: int, label: str) -> None:
        if not isinstance(value, str):
            raise HistoryIndexError(f"{label} must be text")
        if len(value) > maximum:
            raise HistoryIndexError(f"{label} exceeds its bounded length")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HistoryIndexError(f"{label} must be valid UTF-8") from exc
        if label == "search query" and len(encoded) > MAX_QUERY_BYTES:
            raise HistoryIndexError(f"{label} exceeds its bounded UTF-8 size")

    @classmethod
    def _source_specs(cls, sources: list[dict[str, Any]]) -> tuple[_SourceSpec, ...]:
        if not isinstance(sources, list):
            raise HistoryIndexError("sources must be a list of explicit source bindings")
        specs: list[_SourceSpec] = []
        expected = {"source_path", "session_id", "generation"}
        for value in sources:
            if not isinstance(value, dict) or set(value) != expected:
                raise HistoryIndexError("every source binding must contain only source_path, session_id, and generation")
            source_path, session_id, generation = value["source_path"], value["session_id"], value["generation"]
            if not isinstance(source_path, str) or not source_path:
                raise HistoryIndexError("source_path must be a non-empty absolute path")
            cls._utf8(source_path, MAX_QUERY_BYTES, "source_path")
            canonical_session = cls._canonical_session(session_id, "source session_id")
            if type(generation) is not int or generation < 0 or generation > _MAX_SQLITE_INT:
                raise HistoryIndexError("source generation must be a non-negative SQLite integer")
            try:
                candidate = Path(source_path).expanduser()
            except (TypeError, ValueError, RuntimeError) as exc:
                raise HistoryIndexError("source_path is invalid") from exc
            if not candidate.is_absolute() or candidate.name != f"{canonical_session}.jsonl":
                raise HistoryIndexError("source_path must exactly name its <session_id>.jsonl file")
            specs.append(_SourceSpec(source_path, canonical_session, generation))
        return tuple(specs)

    @staticmethod
    def _canonical_session(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise HistoryIndexError(f"{label} must be a canonical lowercase UUID")
        try:
            parsed = str(UUID(value))
        except (ValueError, AttributeError) as exc:
            raise HistoryIndexError(f"{label} must be a canonical lowercase UUID") from exc
        if parsed != value:
            raise HistoryIndexError(f"{label} must be a canonical lowercase UUID")
        return value

    @classmethod
    def _query(cls, query: str | None) -> str | None:
        if query is None:
            return None
        if not isinstance(query, str) or not query:
            raise HistoryIndexError("query must be a non-empty string or None for browse")
        cls._utf8(query, MAX_QUERY_CHARS, "search query")
        try:
            folded = query.casefold()
            if len(folded.encode("utf-8")) > MAX_QUERY_BYTES:
                raise HistoryIndexError("search query exceeds its bounded UTF-8 size")
            return folded
        except UnicodeEncodeError as exc:
            raise HistoryIndexError("search query must be valid UTF-8") from exc

    @staticmethod
    def _iterable(value: Any, label: str) -> tuple[Any, ...]:
        if isinstance(value, (str, bytes)):
            raise HistoryIndexError(f"{label} must be an iterable, not a string")
        try:
            values = tuple(value)
        except TypeError as exc:
            raise HistoryIndexError(f"{label} must be an iterable") from exc
        if len(values) > MAX_FILTER_VALUES:
            raise HistoryIndexError(f"{label} must contain at most {MAX_FILTER_VALUES} values")
        return values

    @classmethod
    def _source_kinds(cls, value: Iterable[str]) -> tuple[str, ...]:
        kinds = cls._iterable(value, "source_kinds")
        if any(not isinstance(kind, str) or kind not in _SOURCE_KINDS for kind in kinds):
            raise HistoryIndexError("source_kinds contains an unknown canonical source kind")
        return tuple(sorted(set(kinds)))

    @classmethod
    def _windows(cls, value: Iterable[int]) -> tuple[int, ...]:
        windows = cls._iterable(value, "windows")
        if any(type(window) is not int or window < 0 or window > _MAX_SQLITE_INT for window in windows):
            raise HistoryIndexError("windows must contain non-negative SQLite integers")
        return tuple(sorted(set(windows)))

    @classmethod
    def _sessions(cls, value: Iterable[str]) -> tuple[str, ...]:
        sessions = tuple(cls._canonical_session(item, "sessions item") for item in cls._iterable(value, "sessions"))
        return tuple(sorted(set(sessions)))

    @classmethod
    def _tool(cls, tool: str | None) -> str | None:
        if tool is None:
            return None
        if not isinstance(tool, str) or not tool:
            raise HistoryIndexError("tool must be a non-empty string or None")
        cls._utf8(tool, MAX_TOOL_CHARS, "tool")
        return tool.casefold()

    @staticmethod
    def _recent_first(value: bool) -> bool:
        if type(value) is not bool:
            raise HistoryIndexError("recent_first must be a boolean")
        return value

    @staticmethod
    def _limit(value: int) -> int:
        if type(value) is not int or not 1 <= value <= MAX_PAGE_ITEMS:
            raise HistoryIndexError(f"limit must be between 1 and {MAX_PAGE_ITEMS}")
        return value

    @classmethod
    def _redaction_policy(cls, secrets: Any) -> tuple[tuple[str, ...], str]:
        if secrets is None:
            values: tuple[str, ...] = ()
        elif isinstance(secrets, str):
            values = (secrets,)
        else:
            try:
                values = tuple(secrets)
            except TypeError as exc:
                raise HistoryIndexError("secrets must be an iterable of strings") from exc
        if len(values) > MAX_FILTER_VALUES or any(not isinstance(value, str) for value in values):
            raise HistoryIndexError("secrets must be an iterable of bounded strings")
        normalized: list[str] = []
        for value in values:
            cls._utf8(value, MAX_QUERY_CHARS, "secret")
            if value and value not in normalized:
                normalized.append(value)
        # Only a digest of the semantic policy is persisted.  Secret values and
        # individual value hashes never appear in cache rows, cursors, or results.
        value_hashes = sorted(hashlib.sha256(value.encode("utf-8")).hexdigest() for value in normalized)
        policy = cls._digest({"version": _REDACTION_POLICY_VERSION, "secrets": value_hashes})
        return tuple(normalized), policy

    @staticmethod
    def _digest(value: Any) -> str:
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise HistoryIndexError("history index binding cannot be encoded safely") from exc
        return hashlib.sha256(encoded).hexdigest()

    # -- source normalization and canonical projection -------------------

    @classmethod
    def _bound_sources(cls, specs: tuple[_SourceSpec, ...]) -> tuple[_BoundSource, ...]:
        bound: list[_BoundSource] = []
        seen: set[tuple[str, str]] = set()
        for spec in specs:
            try:
                source = HistorySource(Path(spec.source_path), spec.session_id)
            except (HistoryError, OSError, TypeError, ValueError) as exc:
                raise HistoryIndexParserError("an explicit history source is unavailable or invalid") from exc
            item = _BoundSource(spec, source)
            if item.key in seen:
                raise HistoryIndexError("two source bindings resolve to the same session history")
            seen.add(item.key)
            bound.append(item)
        return tuple(sorted(bound, key=lambda item: (item.generation, item.key[0], item.key[1])))

    @staticmethod
    def _signature(path: Path) -> _Signature:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise HistoryIndexParserError("cannot inspect an explicit history source") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise HistoryIndexParserError("explicit history source is no longer a regular nonsymlink file")
        return _Signature(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _project_source(
        self,
        source: _BoundSource,
        secrets: tuple[str, ...],
        signature: _Signature,
    ) -> tuple[list[_ProjectedRecord], bool]:
        try:
            projection = source.source.index_projection(secrets=secrets)
        except HistoryError as exc:
            raise HistoryIndexParserError("canonical history projection failed") from exc
        after = self._signature(source.source.path)
        if after != signature:
            raise HistoryIndexStaleError("history source changed while its projection was being indexed; restart")
        if not isinstance(projection, dict) or set(projection) != {"entries", "deferred_incomplete_tail"}:
            raise HistoryIndexParserError("canonical history projection has an invalid shape")
        deferred = projection["deferred_incomplete_tail"]
        if type(deferred) is not bool or not isinstance(projection["entries"], list):
            raise HistoryIndexParserError("canonical history projection has invalid values")
        return self._prepare_projection(source, projection["entries"]), deferred

    def _prepare_projection(self, source: _BoundSource, entries: list[Any]) -> list[_ProjectedRecord]:
        prepared: list[_ProjectedRecord] = []
        starts: set[int] = set()
        for value in entries:
            if not isinstance(value, dict):
                raise HistoryIndexParserError("canonical history projection entry is invalid")
            locator = value.get("locator")
            if not isinstance(locator, dict):
                raise HistoryIndexParserError("canonical history projection locator is invalid")
            required = {"source_path", "session_id", "message_id", "start_byte", "end_byte", "sha256", "source_kind"}
            if set(locator) != required:
                raise HistoryIndexParserError("canonical history projection locator is incomplete")
            source_path, session_id = locator["source_path"], locator["session_id"]
            message_id = locator["message_id"]
            start, end, sha = locator["start_byte"], locator["end_byte"], locator["sha256"]
            source_kind = value.get("source_kind")
            text, timestamp, total = value.get("text"), value.get("timestamp"), value.get("total_chars")
            if (
                source_path != source.key[0]
                or session_id != source.key[1]
                or not isinstance(message_id, str)
                or not message_id
                or type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or not isinstance(sha, str)
                or len(sha) != 64
                or any(character not in "0123456789abcdef" for character in sha)
                or source_kind not in _SOURCE_KINDS
                or locator["source_kind"] != source_kind
                or not isinstance(text, str)
                or not text
                or timestamp is not None and not isinstance(timestamp, str)
                or type(total) is not int
                or total != len(text)
            ):
                raise HistoryIndexParserError("canonical history projection entry has invalid public fields")
            self._utf8(text, 8 * 1024 * 1024, "projected history text")
            if timestamp is not None:
                self._utf8(timestamp, MAX_QUERY_BYTES, "projected timestamp")
            if start in starts:
                raise HistoryIndexParserError("canonical history projection repeats a byte boundary")
            starts.add(start)
            tools = self._tools_from_projection(value)
            try:
                folded = text.casefold()
                folded.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise HistoryIndexParserError("canonical history text cannot be casefolded safely") from exc
            fingerprint = self._digest({
                "message_id": message_id,
                "start_byte": start,
                "end_byte": end,
                "sha256": sha,
                "source_kind": source_kind,
                "timestamp": timestamp,
                "text": text,
                "total_chars": total,
                "tools": tools,
            })
            prepared.append(_ProjectedRecord(
                source_path,
                session_id,
                message_id,
                start,
                end,
                sha,
                source_kind,
                timestamp,
                text,
                folded,
                total,
                tools,
                fingerprint,
            ))
        return sorted(prepared, key=lambda item: item.start_byte)

    @classmethod
    def _tools_from_projection(cls, value: dict[str, Any]) -> tuple[tuple[str, str | None], ...]:
        raw_tools = value.get("tool_uses", [])
        if not isinstance(raw_tools, list):
            raise HistoryIndexParserError("canonical tool metadata is invalid")
        tools: list[tuple[str, str | None]] = []
        for raw in raw_tools:
            if not isinstance(raw, dict) or set(raw) != {"tool_use_id", "tool_name"}:
                raise HistoryIndexParserError("canonical tool metadata is invalid")
            tool_use_id, tool_name = raw["tool_use_id"], raw["tool_name"]
            if not isinstance(tool_use_id, str) or not tool_use_id or tool_name is not None and not isinstance(tool_name, str):
                raise HistoryIndexParserError("canonical tool metadata is invalid")
            cls._utf8(tool_use_id, MAX_TOOL_CHARS, "tool_use_id")
            if tool_name is not None:
                if not tool_name:
                    raise HistoryIndexParserError("canonical tool metadata is invalid")
                cls._utf8(tool_name, MAX_TOOL_CHARS, "tool_name")
            tools.append((tool_use_id, tool_name))
        declared_ids = value.get("tool_use_ids", ())
        declared_names = value.get("tool_names", ())
        expected_ids: list[str] = []
        for tool_use_id, _ in tools:
            if tool_use_id not in expected_ids:
                expected_ids.append(tool_use_id)
        if declared_ids not in ((), expected_ids):
            raise HistoryIndexParserError("canonical tool identifiers do not match their associations")
        expected_names = list(dict.fromkeys(name for _, name in tools if name is not None))
        if declared_names not in ((), expected_names):
            raise HistoryIndexParserError("canonical tool names do not match their associations")
        return tuple(tools)

    # -- cache refresh -----------------------------------------------------

    @staticmethod
    def _new_refresh() -> dict[str, int]:
        return {
            "parsed_sources": 0,
            "refreshed_sources": 0,
            "unchanged_sources": 0,
            "rebuilt_sources": 0,
            "added_records": 0,
            "changed_records": 0,
            "removed_records": 0,
            "removed_sources": 0,
            "redaction_evicted_sources": 0,
            "deferred_sources": 0,
        }

    def _refresh_without_selected_sources(self, redaction_policy: str) -> dict[str, int]:
        """Evict stale text on a policy change without reading any source file."""
        refresh = self._new_refresh()
        previous = self._stored_policy()
        if previous == redaction_policy:
            return refresh
        states = self._states()
        had_cached_sources = bool(states)
        with self._transaction():
            for key in sorted(states):
                self._delete_source(key)
                refresh["redaction_evicted_sources"] += 1
            self._set_policy(redaction_policy)
        if previous is not None or had_cached_sources:
            self._vacuum_after_redaction_change()
        return refresh

    def _refresh(
        self,
        current_keys: set[tuple[str, str]],
        selected: tuple[_BoundSource, ...],
        secrets: tuple[str, ...],
        redaction_policy: str,
    ) -> dict[str, int]:
        """Refresh only selected source files and atomically apply their diffs."""
        refresh = self._new_refresh()
        states = self._states()
        had_cached_sources = bool(states)
        previous_policy = self._stored_policy()
        policy_changed = previous_policy != redaction_policy
        selected_keys = {source.key for source in selected}
        removed_keys = set(states) - current_keys
        evicted_keys = {
            key for key in states
            if policy_changed and key not in selected_keys and key in current_keys
        }
        updates: dict[tuple[str, str], tuple[_Signature, list[_ProjectedRecord], bool, _State | None]] = {}

        for source in selected:
            signature = self._signature(source.source.path)
            prior = states.get(source.key)
            if policy_changed or prior is None or prior.signature != signature:
                records, deferred = self._project_source(source, secrets, signature)
                updates[source.key] = (signature, records, deferred, prior)
                refresh["parsed_sources"] += 1
                refresh["refreshed_sources"] += 1
                refresh["deferred_sources"] += int(deferred)
            else:
                refresh["unchanged_sources"] += 1
                refresh["deferred_sources"] += int(prior.deferred_tail)

        with self._transaction():
            for key in sorted(removed_keys):
                self._delete_source(key)
                refresh["removed_sources"] += 1
            for key in sorted(evicted_keys):
                self._delete_source(key)
                refresh["redaction_evicted_sources"] += 1
            for source in selected:
                update = updates.get(source.key)
                if update is None:
                    continue
                signature, records, deferred, prior = update
                rebuild = prior is not None and (
                    signature.dev != prior.signature.dev
                    or signature.ino != prior.signature.ino
                    or signature.size < prior.signature.size
                )
                # The source binding is the records table's foreign-key parent;
                # create/update it before adding any projected child rows.
                self._upsert_state(source.key, signature, deferred)
                self._merge_source(source.key, records, rebuild, refresh)
            if policy_changed:
                self._set_policy(redaction_policy)

        # secure_delete overwrites removed cells; VACUUM then rebuilds the cache
        # file so an old known-secret cannot remain in FTS/free pages.
        if policy_changed and (previous_policy is not None or had_cached_sources):
            self._vacuum_after_redaction_change()
        return refresh

    def _merge_source(
        self,
        key: tuple[str, str],
        records: list[_ProjectedRecord],
        rebuild: bool,
        refresh: dict[str, int],
    ) -> None:
        old = self._source_records(key)
        if rebuild:
            refresh["rebuilt_sources"] += 1
            refresh["removed_records"] += len(old)
            self._delete_records(old.values())
            for record in records:
                self._insert_record(record)
            refresh["added_records"] += len(records)
            return

        new = {record.start_byte: record for record in records}
        if len(new) != len(records):
            raise HistoryIndexParserError("canonical history projection repeats a byte boundary")
        removed = set(old) - set(new)
        changed = {
            start for start in set(old) & set(new)
            if old[start][1].projection_hash != new[start].projection_hash
        }
        added = set(new) - set(old)
        self._delete_records(old[start] for start in sorted(removed | changed))
        for start in sorted(added | changed):
            self._insert_record(new[start])
        refresh["removed_records"] += len(removed)
        refresh["changed_records"] += len(changed)
        refresh["added_records"] += len(added)

    def _source_records(self, key: tuple[str, str]) -> dict[int, tuple[int, _ProjectedRecord]]:
        rows = self._rows(
            """
            SELECT id, source_path, session_id, message_id, start_byte, end_byte, sha256,
                   source_kind, timestamp, text, text_folded, total_chars, projection_hash
              FROM records
             WHERE source_path=? AND session_id=?
            """,
            key,
        )
        result: dict[int, tuple[int, _ProjectedRecord]] = {}
        for row in rows:
            (
                record_id,
                source_path,
                session_id,
                message_id,
                start,
                end,
                sha,
                source_kind,
                timestamp,
                text,
                folded,
                total,
                fingerprint,
            ) = row
            if (
                type(record_id) is not int
                or not isinstance(source_path, str)
                or not isinstance(session_id, str)
                or not isinstance(message_id, str)
                or type(start) is not int
                or type(end) is not int
                or not isinstance(sha, str)
                or source_kind not in _SOURCE_KINDS
                or timestamp is not None and not isinstance(timestamp, str)
                or not isinstance(text, str)
                or not isinstance(folded, str)
                or type(total) is not int
                or not isinstance(fingerprint, str)
            ):
                raise HistoryIndexCacheError("private history index record has an invalid shape")
            tools = self._record_tools(record_id)
            record = _ProjectedRecord(
                source_path,
                session_id,
                message_id,
                start,
                end,
                sha,
                source_kind,
                timestamp,
                text,
                folded,
                total,
                tools,
                fingerprint,
            )
            if start in result:
                raise HistoryIndexCacheError("private history index repeats a byte boundary")
            result[start] = (record_id, record)
        return result

    def _insert_record(self, record: _ProjectedRecord) -> None:
        self._connection().execute(
            """
            INSERT INTO records(
                source_path, session_id, message_id, start_byte, end_byte, sha256,
                source_kind, timestamp, text, text_folded, total_chars, projection_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.source_path,
                record.session_id,
                record.message_id,
                record.start_byte,
                record.end_byte,
                record.sha256,
                record.source_kind,
                record.timestamp,
                record.text,
                record.text_folded,
                record.total_chars,
                record.projection_hash,
            ),
        )
        row = next(self._connection().execute("SELECT last_insert_rowid()"), None)
        if row is None or type(row[0]) is not int:
            raise HistoryIndexCacheError("cannot bind an inserted history record")
        record_id = row[0]
        self._connection().execute("INSERT INTO history_fts(rowid, text_folded) VALUES (?, ?)", (record_id, record.text_folded))
        for ordinal, (tool_use_id, tool_name) in enumerate(record.tools):
            self._connection().execute(
                """
                INSERT INTO record_tools(record_id, ordinal, tool_use_id, tool_use_id_folded, tool_name, tool_name_folded)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    ordinal,
                    tool_use_id,
                    tool_use_id.casefold(),
                    tool_name,
                    tool_name.casefold() if tool_name is not None else None,
                ),
            )

    def _delete_records(self, records: Iterable[tuple[int, _ProjectedRecord]]) -> None:
        for record_id, _ in records:
            self._connection().execute("DELETE FROM history_fts WHERE rowid=?", (record_id,))
            self._connection().execute("DELETE FROM records WHERE id=?", (record_id,))

    def _delete_source(self, key: tuple[str, str]) -> None:
        self._delete_records(self._source_records(key).values())
        self._connection().execute("DELETE FROM source_state WHERE source_path=? AND session_id=?", key)

    def _upsert_state(self, key: tuple[str, str], signature: _Signature, deferred_tail: bool) -> None:
        self._connection().execute(
            """
            INSERT INTO source_state(
                source_path, session_id, dev, ino, size, mtime_ns, ctime_ns, deferred_tail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_path, session_id) DO UPDATE SET
                dev=excluded.dev,
                ino=excluded.ino,
                size=excluded.size,
                mtime_ns=excluded.mtime_ns,
                ctime_ns=excluded.ctime_ns,
                deferred_tail=excluded.deferred_tail
            """,
            (*key, *signature.values(), int(deferred_tail)),
        )

    # -- deterministic query and cursor ----------------------------------

    def _validate_selected_snapshot(self, selected: tuple[_BoundSource, ...]) -> dict[tuple[str, str], _State]:
        states = self._states()
        for source in selected:
            state = states.get(source.key)
            if state is None:
                raise HistoryIndexCacheError("selected source has no cache state after refresh")
            if self._signature(source.source.path) != state.signature:
                raise HistoryIndexStaleError("history source changed during refresh; restart the search")
        return states

    def _snapshot_digest(
        self,
        selected: tuple[_BoundSource, ...],
        states: dict[tuple[str, str], _State],
        redaction_policy: str,
    ) -> str:
        snapshot = []
        for source in selected:
            state = states.get(source.key)
            if state is None:
                raise HistoryIndexCacheError("selected source cache state is missing")
            snapshot.append({
                "source_path": source.key[0],
                "session_id": source.key[1],
                "generation": source.generation,
                "signature": state.signature.values(),
            })
        return self._digest({"policy": redaction_policy, "sources": snapshot})

    @classmethod
    def _request_digest(
        cls,
        needle: str | None,
        kinds: tuple[str, ...],
        tool: str | None,
        recent_first: bool,
        windows: tuple[int, ...],
        sessions: tuple[str, ...],
    ) -> str:
        return cls._digest({
            "query": None if needle is None else hashlib.sha256(needle.encode("utf-8")).hexdigest(),
            "source_kinds": kinds,
            "tool": None if tool is None else hashlib.sha256(tool.encode("utf-8")).hexdigest(),
            "recent_first": recent_first,
            "windows": windows,
            "sessions": sessions,
        })

    def _cursor_position(self, cursor: str | None, request_digest: str, snapshot: str) -> int:
        if cursor is None:
            return 0
        value = self._decode_cursor(cursor)
        if value["request"] != request_digest:
            raise HistoryIndexStaleError("cursor does not match this search request; restart the search")
        if value["snapshot"] != snapshot:
            raise HistoryIndexStaleError("history source snapshot changed; restart the search")
        return value["position"]

    @staticmethod
    def _encode_cursor(request_digest: str, snapshot: str, position: int) -> str:
        payload = {
            "version": _CURSOR_VERSION,
            "request": request_digest,
            "snapshot": snapshot,
            "position": position,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> dict[str, Any]:
        if not isinstance(cursor, str) or not cursor:
            raise HistoryIndexError("cursor must be a non-empty opaque string")
        try:
            raw_cursor = cursor.encode("ascii")
        except UnicodeEncodeError as exc:
            raise HistoryIndexError("cursor must be an ASCII opaque string") from exc
        if len(raw_cursor) > MAX_CURSOR_BYTES:
            raise HistoryIndexError("cursor exceeds its bounded size")
        try:
            padded = raw_cursor + b"=" * (-len(raw_cursor) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise HistoryIndexError("cursor is invalid") from exc
        expected = {"version", "request", "snapshot", "position"}
        if not isinstance(value, dict) or set(value) != expected:
            raise HistoryIndexError("cursor is invalid")
        if (
            value["version"] != _CURSOR_VERSION
            or not isinstance(value["request"], str)
            or not isinstance(value["snapshot"], str)
            or len(value["request"]) != 64
            or len(value["snapshot"]) != 64
            or any(character not in "0123456789abcdef" for character in value["request"] + value["snapshot"])
            or type(value["position"]) is not int
            or value["position"] < 0
            or value["position"] > _MAX_SQLITE_INT
        ):
            raise HistoryIndexError("cursor is invalid")
        return value

    def _rows_for_page(
        self,
        selected: tuple[_BoundSource, ...],
        needle: str | None,
        kinds: tuple[str, ...],
        tool: str | None,
        recent_first: bool,
        position: int,
        count: int,
    ) -> list[tuple[Any, ...]]:
        use_fts = needle is not None and len(needle) >= 3 and "\x00" not in needle
        if use_fts:
            try:
                return self._query_rows(selected, needle, kinds, tool, recent_first, position, count, use_fts=True)
            except apsw.SQLError as exc:
                # The generated phrase is quoted, but a SQLite build can still
                # reject an edge Unicode token.  Content scan retains exact
                # literal semantics without rereading native JSONL.
                message = str(exc).lower()
                if not any(marker in message for marker in ("syntax", "malformed", "not supported", "unsupported")):
                    raise HistoryIndexCacheError("private history FTS query failed") from exc
        return self._query_rows(selected, needle, kinds, tool, recent_first, position, count, use_fts=False)

    def _query_rows(
        self,
        selected: tuple[_BoundSource, ...],
        needle: str | None,
        kinds: tuple[str, ...],
        tool: str | None,
        recent_first: bool,
        position: int,
        count: int,
        *,
        use_fts: bool,
    ) -> list[tuple[Any, ...]]:
        # 临时绑定表避免窗口数量增长触发 SQL 参数数量上限。
        connection = self._connection()
        connection.execute("CREATE TEMP TABLE IF NOT EXISTS selected_sources("
                           "source_path TEXT, session_id TEXT, generation INTEGER)")
        connection.execute("DELETE FROM selected_sources")
        connection.executemany("INSERT INTO selected_sources VALUES (?, ?, ?)",
                               ((*source.key, source.generation) for source in selected))
        params: list[Any] = []
        joins = [
            "FROM records AS r",
            "JOIN selected_sources AS s ON s.source_path=r.source_path AND s.session_id=r.session_id",
        ]
        if use_fts:
            joins.append("JOIN history_fts ON history_fts.rowid=r.id")
        clauses: list[str] = []
        if needle is not None:
            if use_fts:
                clauses.append("history_fts MATCH ?")
                params.append('"' + needle.replace('"', '""') + '"')
            clauses.append("instr(r.text_folded, ?) > 0")
            params.append(needle)
        if kinds:
            clauses.append("r.source_kind IN (" + ", ".join("?" for _ in kinds) + ")")
            params.extend(kinds)
        if tool is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM record_tools AS t WHERE t.record_id=r.id "
                "AND (t.tool_name_folded=? OR t.tool_use_id_folded=?))"
            )
            params.extend((tool, tool))
        direction = "DESC" if recent_first else "ASC"
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""
            SELECT r.id, r.source_path, r.session_id, r.message_id, r.start_byte, r.end_byte,
                   r.sha256, r.source_kind, r.timestamp, r.text, r.total_chars, s.generation
              {' '.join(joins)}
              {where}
             ORDER BY s.generation {direction}, s.source_path {direction}, s.session_id {direction},
                      r.start_byte {direction}, r.id {direction}
             LIMIT ? OFFSET ?
        """
        params.extend((count, position))
        return list(self._connection().execute(sql, tuple(params)))

    def _entries(self, rows: list[tuple[Any, ...]], needle: str | None) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for row in rows:
            (
                record_id,
                source_path,
                session_id,
                message_id,
                start,
                end,
                sha,
                source_kind,
                timestamp,
                text,
                total_chars,
                generation,
            ) = row
            if (
                type(record_id) is not int
                or not isinstance(source_path, str)
                or not isinstance(session_id, str)
                or not isinstance(message_id, str)
                or type(start) is not int
                or type(end) is not int
                or not isinstance(sha, str)
                or source_kind not in _SOURCE_KINDS
                or timestamp is not None and not isinstance(timestamp, str)
                or not isinstance(text, str)
                or type(total_chars) is not int
                or type(generation) is not int
            ):
                raise HistoryIndexCacheError("private history index query returned an invalid record")
            entry: dict[str, Any] = {
                "locator": {
                    "source_path": source_path,
                    "session_id": session_id,
                    "message_id": message_id,
                    "start_byte": start,
                    "end_byte": end,
                    "sha256": sha,
                    "source_kind": source_kind,
                },
                "source_kind": source_kind,
                "timestamp": timestamp,
                "generation": generation,
                "snippet": self._snippet(text, needle),
                "total_chars": total_chars,
            }
            tools = self._record_tools(record_id)
            if tools:
                tool_use_ids: list[str] = []
                tool_names: list[str] = []
                for tool_use_id, tool_name in tools:
                    if tool_use_id not in tool_use_ids:
                        tool_use_ids.append(tool_use_id)
                    if tool_name is not None and tool_name not in tool_names:
                        tool_names.append(tool_name)
                if tool_use_ids:
                    entry["tool_use_ids"] = tool_use_ids
                if tool_names:
                    entry["tool_names"] = tool_names
            entries.append(entry)
        return entries

    @staticmethod
    def _snippet(text: str, needle: str | None) -> str:
        if needle is None:
            start = 0
        else:
            folded = text.casefold()
            position = folded.find(needle)
            if position < 0:
                raise HistoryIndexCacheError("private history index literal match is inconsistent")
            start = HistoryIndex._original_offset(text, position)
        left = max(0, start - 56)
        right = min(len(text), start + 136)
        fragment = " ".join(text[left:right].split())
        prefix = "…" if left else ""
        return prefix + fragment[: max(0, 160 - len(prefix))]

    @staticmethod
    def _original_offset(text: str, folded_offset: int) -> int:
        if folded_offset <= 0:
            return 0
        used = 0
        for offset, character in enumerate(text):
            next_used = used + len(character.casefold())
            if folded_offset < next_used:
                return offset
            if folded_offset == next_used:
                return offset + 1
            used = next_used
        return len(text)

    # -- APSW schema and cache safety -------------------------------------

    @staticmethod
    def _lstat(path: Path) -> os.stat_result | None:
        try:
            return os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HistoryIndexCacheError("cannot inspect private history index storage") from exc

    @classmethod
    def _prepare_cache_path(cls, value: Path) -> tuple[Path, bool]:
        try:
            original = Path(value).expanduser()
        except (TypeError, ValueError, RuntimeError) as exc:
            raise HistoryIndexCacheError("history index cache path is invalid") from exc
        if not original.is_absolute() or ".." in original.parts or not original.name:
            raise HistoryIndexCacheError("history index cache path must be an absolute non-aliased file path")
        path = Path(os.path.abspath(os.fspath(original)))
        cls._ensure_private_parent(path.parent)
        cls._check_sidecars(path)
        info = cls._lstat(path)
        if info is None:
            return path, False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HistoryIndexCacheError("history index cache target must be one regular nonsymlink file")
        cls._require_current_owner(info, "history index cache target")
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise HistoryIndexCacheError("cannot restrict history index cache permissions") from exc
        return path, True

    @classmethod
    def _ensure_private_parent(cls, parent: Path) -> None:
        anchor = Path(parent.anchor)
        current = anchor
        for component in parent.parts[1:]:
            current /= component
            info = cls._lstat(current)
            if info is None:
                try:
                    os.mkdir(current, 0o700)
                except OSError as exc:
                    raise HistoryIndexCacheError("cannot create private history index directory") from exc
                info = cls._lstat(current)
            if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise HistoryIndexCacheError("history index directory must not contain a symbolic link")
        info = cls._lstat(parent)
        if info is None:
            raise HistoryIndexCacheError("private history index directory is unavailable")
        cls._require_current_owner(info, "private history index directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise HistoryIndexCacheError("history index directory must have private permissions")

    @classmethod
    def _check_sidecars(cls, path: Path) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            info = cls._lstat(sidecar)
            if info is None:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise HistoryIndexCacheError("history index SQLite sidecar must not be a symbolic link")
            cls._require_current_owner(info, "history index SQLite sidecar")
            try:
                os.chmod(sidecar, 0o600)
            except OSError as exc:
                raise HistoryIndexCacheError("cannot restrict history index sidecar permissions") from exc

    @staticmethod
    def _require_current_owner(info: os.stat_result, label: str) -> None:
        uid = getattr(os, "getuid", lambda: None)()
        if uid is not None and info.st_uid != uid:
            raise HistoryIndexCacheError(f"{label} must be owned by the current user")

    def _verify_cache_target(self) -> None:
        self._ensure_private_parent(self._path.parent)
        self._check_sidecars(self._path)
        info = self._lstat(self._path)
        if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HistoryIndexCacheError("history index cache target changed or is unsafe")
        self._require_current_owner(info, "history index cache target")
        try:
            os.chmod(self._path, 0o600)
        except OSError as exc:
            raise HistoryIndexCacheError("cannot restrict history index cache permissions") from exc
        try:
            resolved = self._path.resolve(strict=True)
        except OSError as exc:
            raise HistoryIndexCacheError("cannot resolve history index cache target") from exc
        if resolved != self._path:
            raise HistoryIndexCacheError("history index cache target must not resolve through a symbolic link")

    def _connection(self) -> apsw.Connection:
        if self._closed or self._db is None:
            raise HistoryIndexCacheError("history index is closed")
        return self._db

    def _close_quietly(self) -> None:
        connection, self._db, self._closed = self._db, None, True
        if connection is not None:
            try:
                connection.close()
            except apsw.Error:
                pass

    def _configure_owned_cache(self) -> None:
        self._connection().execute("PRAGMA journal_mode=DELETE")
        self._connection().execute("PRAGMA synchronous=FULL")
        self._connection().execute("PRAGMA secure_delete=ON")
        self._connection().execute("INSERT INTO history_fts(history_fts, rank) VALUES('secure-delete', 1)")
        self._connection().execute("PRAGMA foreign_keys=ON")

    def _create_schema(self) -> None:
        statements = (
            """
            CREATE TABLE metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT
            """,
            """
            CREATE TABLE source_state(
                source_path TEXT NOT NULL,
                session_id TEXT NOT NULL,
                dev INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                size INTEGER NOT NULL CHECK(size >= 0),
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                deferred_tail INTEGER NOT NULL CHECK(deferred_tail IN (0, 1)),
                PRIMARY KEY(source_path, session_id)
            ) STRICT
            """,
            """
            CREATE TABLE records(
                id INTEGER PRIMARY KEY,
                source_path TEXT NOT NULL,
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                start_byte INTEGER NOT NULL CHECK(start_byte >= 0),
                end_byte INTEGER NOT NULL CHECK(end_byte > start_byte),
                sha256 TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                timestamp TEXT,
                text TEXT NOT NULL,
                text_folded TEXT NOT NULL,
                total_chars INTEGER NOT NULL CHECK(total_chars >= 0),
                projection_hash TEXT NOT NULL,
                UNIQUE(source_path, session_id, start_byte),
                FOREIGN KEY(source_path, session_id)
                    REFERENCES source_state(source_path, session_id) ON DELETE CASCADE
            ) STRICT
            """,
            """
            CREATE TABLE record_tools(
                record_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                tool_use_id TEXT NOT NULL,
                tool_use_id_folded TEXT NOT NULL,
                tool_name TEXT,
                tool_name_folded TEXT,
                PRIMARY KEY(record_id, ordinal),
                FOREIGN KEY(record_id) REFERENCES records(id) ON DELETE CASCADE
            ) STRICT
            """,
            "CREATE INDEX records_source_order ON records(source_path, session_id, start_byte)",
            "CREATE INDEX record_tools_name ON record_tools(tool_name_folded)",
            "CREATE INDEX record_tools_id ON record_tools(tool_use_id_folded)",
            """
            CREATE VIRTUAL TABLE history_fts USING fts5(
                text_folded,
                tokenize='trigram case_sensitive 1 remove_diacritics 0'
            )
            """,
        )
        with self._transaction():
            for statement in statements:
                self._connection().execute(statement)
            self._connection().execute(f"PRAGMA application_id={_APPLICATION_ID}")
            self._connection().execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _validate_schema(self) -> None:
        application_id = self._scalar("PRAGMA application_id")
        version = self._scalar("PRAGMA user_version")
        if application_id != _APPLICATION_ID or version != SCHEMA_VERSION:
            raise HistoryIndexCacheError("history index cache is not this schema version; choose a new cache path")
        objects = {row[0]: row[1] for row in self._rows("SELECT name, type FROM sqlite_schema")}
        required = {"metadata", "source_state", "records", "record_tools", "history_fts"}
        if any(objects.get(name) != "table" for name in required):
            raise HistoryIndexCacheError("history index cache schema is incomplete")
        fts_sql = self._scalar("SELECT sql FROM sqlite_schema WHERE name='history_fts'")
        if not isinstance(fts_sql, str) or "fts5" not in fts_sql.lower() or "trigram" not in fts_sql.lower():
            raise HistoryIndexCacheError("history index cache FTS schema is invalid")
        if self._rows("SELECT name FROM sqlite_schema WHERE type='trigger'"):
            raise HistoryIndexCacheError("history index cache must not contain triggers")
        if self._rows("PRAGMA foreign_key_check"):
            raise HistoryIndexCacheError("history index cache foreign-key bindings are invalid")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            self._connection().execute("BEGIN IMMEDIATE")
        except apsw.Error as exc:
            raise HistoryIndexCacheError("private history index cache is busy or cannot begin an update") from exc
        try:
            yield
        except BaseException:
            try:
                self._connection().execute("ROLLBACK")
            except apsw.Error as exc:
                raise HistoryIndexCacheError("private history index rollback failed") from exc
            raise
        else:
            try:
                self._connection().execute("COMMIT")
            except apsw.Error as exc:
                try:
                    self._connection().execute("ROLLBACK")
                except apsw.Error:
                    pass
                raise HistoryIndexCacheError("private history index commit failed") from exc

    def _rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        try:
            return list(self._connection().execute(sql, parameters))
        except apsw.Error as exc:
            raise HistoryIndexCacheError("private history index cache query failed") from exc

    def _scalar(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
        rows = self._rows(sql, parameters)
        if len(rows) != 1 or len(rows[0]) != 1:
            raise HistoryIndexCacheError("private history index cache returned an invalid scalar")
        return rows[0][0]

    def _states(self) -> dict[tuple[str, str], _State]:
        rows = self._rows(
            "SELECT source_path, session_id, dev, ino, size, mtime_ns, ctime_ns, deferred_tail FROM source_state"
        )
        states: dict[tuple[str, str], _State] = {}
        for row in rows:
            source_path, session_id, dev, ino, size, mtime_ns, ctime_ns, deferred = row
            values = (dev, ino, size, mtime_ns, ctime_ns)
            if (
                not isinstance(source_path, str)
                or not isinstance(session_id, str)
                or any(type(value) is not int for value in values)
                or size < 0
                or deferred not in (0, 1)
            ):
                raise HistoryIndexCacheError("private history index source state is invalid")
            key = (source_path, session_id)
            if key in states:
                raise HistoryIndexCacheError("private history index repeats a source state")
            states[key] = _State(_Signature(*values), bool(deferred))
        return states

    def _stored_policy(self) -> str | None:
        rows = self._rows("SELECT value FROM metadata WHERE key='redaction_policy'")
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], str):
            raise HistoryIndexCacheError("private history index redaction policy is invalid")
        value = rows[0][0]
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise HistoryIndexCacheError("private history index redaction policy is invalid")
        return value

    def _set_policy(self, value: str) -> None:
        self._connection().execute(
            """
            INSERT INTO metadata(key, value) VALUES ('redaction_policy', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (value,),
        )

    def _record_tools(self, record_id: int) -> tuple[tuple[str, str | None], ...]:
        rows = self._rows(
            "SELECT tool_use_id, tool_name FROM record_tools WHERE record_id=? ORDER BY ordinal ASC",
            (record_id,),
        )
        tools: list[tuple[str, str | None]] = []
        for tool_use_id, tool_name in rows:
            if not isinstance(tool_use_id, str) or not tool_use_id or tool_name is not None and not isinstance(tool_name, str):
                raise HistoryIndexCacheError("private history index tool metadata is invalid")
            tools.append((tool_use_id, tool_name))
        return tuple(tools)

    def _vacuum_after_redaction_change(self) -> None:
        try:
            # VACUUM 不会清除 FTS 尚未合并的逻辑删除词项，先重建倒排段。
            self._connection().execute("INSERT INTO history_fts(history_fts) VALUES('rebuild')")
            self._connection().execute("VACUUM")
        except apsw.Error as exc:
            raise HistoryIndexCacheError("cannot securely rebuild the private history index cache") from exc
        self._verify_cache_target()
        self._validate_schema()

    def _result(self, entries: list[dict[str, Any]], next_cursor: str | None, refresh: dict[str, int]) -> dict[str, Any]:
        return {
            "entries": entries,
            "next_cursor": next_cursor,
            "sqlite_version": self._sqlite_version,
            "refresh": refresh,
        }


__all__ = [
    "HistoryIndex",
    "HistoryIndexCacheError",
    "HistoryIndexError",
    "HistoryIndexParserError",
    "HistoryIndexStaleError",
    "MAX_PAGE_ITEMS",
    "SCHEMA_VERSION",
]
