"""Process-local canonical stream reuse; no native record is persisted here."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from .history import HistoryError, _Record, _MESSAGE_TYPES, _STATE_ONLY, _message_id, _task_notice


@dataclass(frozen=True)
class StreamWork:
    read_bytes: int = 0
    validated_bytes: int = 0
    parsed_bytes: int = 0
    parsed_records: int = 0


class HistoryStream:
    """Reuse complete records, validating old bytes before accepting an append.

    File metadata can identify an unchanged source, but growth does not prove an
    append: a writer can edit the prefix and append in the same operation. Keep
    the complete prefix only in memory and compare it byte-for-byte on changes.
    This intentionally costs prefix I/O, not repeated JSON decoding or hashes.
    A new process must parse a changed source again; this is not a disk cursor.
    """

    def __init__(self, source: Any) -> None:
        self.source = source
        self.signature: tuple[int, ...] | None = None
        self.prefix = bytearray()
        self.complete: list[_Record] = []
        self.deferred_tail = False
        self.revision = 0
        self.reset_revision = 0
        self.work = StreamWork()
        self._views: dict[tuple[bool, bool], tuple[int, list[_Record], set[str]]] = {}

    @staticmethod
    def _signature(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @staticmethod
    def _compare_prefix(handle, parts) -> tuple[bool, int]:
        checked = 0
        for expected in parts:
            position = 0
            while position < len(expected):
                block = os.pread(handle.fileno(), min(64 * 1024, len(expected) - position), checked)
                checked += len(block)
                if not block or block != expected[position:position + len(block)]:
                    return False, checked
                position += len(block)
        return True, checked

    def _snapshot_exists(self, handle, before: tuple[int, ...]) -> bool:
        actual = (self._signature(os.fstat(handle.fileno())),
                  self._signature(self.source.path.stat()))
        return all(value[:2] == before[:2] and value[2] >= before[2] for value in actual)

    def refresh(self) -> None:
        read = validated = parsed = count = 0
        try:
            before = self._signature(self.source.path.stat())
            self.work = StreamWork()
            if before == self.signature:
                return
            with self.source.path.open('rb') as handle:
                opened = self._signature(os.fstat(handle.fileno()))
                if opened[:2] != before[:2]:
                    raise HistoryError('history source changed before it could be read')
                before = opened
                reuse = (self.signature is not None and before[:2] == self.signature[:2]
                         and before[2] >= len(self.prefix))
                if reuse:
                    reuse, checked = self._compare_prefix(handle, (self.prefix,))
                    read += checked
                    validated += checked
                offset = len(self.prefix) if reuse else 0
                handle.seek(offset)
                added: list[_Record] = []
                raw_prefix = bytearray()
                tail = b''
                while handle.tell() < before[2]:
                    start = handle.tell()
                    raw = handle.readline(min(8 * 1024 * 1024 + 1, before[2] - start))
                    read += len(raw)
                    if not raw:
                        break
                    if len(raw) > 8 * 1024 * 1024:
                        raise HistoryError('single history record exceeds the bounded reader limit')
                    if not raw.endswith(b'\n'):
                        tail = raw
                        break
                    parsed += len(raw)
                    count += 1
                    record = self.source._parse_record(raw, start)
                    if record is not None:
                        added.append(record)
                    raw_prefix.extend(raw)
                if (self._signature(os.fstat(handle.fileno())) != before
                        or self._signature(self.source.path.stat()) != before):
                    if not self._snapshot_exists(handle, before):
                        raise HistoryError('history source changed while it was being read')
                    # A live writer may append beyond this snapshot. Validate its
                    # captured bytes, not the changing size of the entire file.
                    matches, checked = self._compare_prefix(
                        handle, (self.prefix if reuse else b'', raw_prefix, tail))
                    read += checked
                    validated += checked
                    if not matches or not self._snapshot_exists(handle, before):
                        raise HistoryError('history source changed while it was being read')
            if not reuse:
                self.complete = []
                self.prefix = bytearray()
                self._views.clear()
                self.reset_revision = self.revision + 1
            self.complete.extend(added)
            self.prefix.extend(raw_prefix)
            self.deferred_tail = bool(tail)
            # Keep the captured signature so later appends are observed next time.
            self.signature = before
            self.revision += 1
        except OSError as exc:
            raise HistoryError('cannot read explicit source') from exc
        finally:
            self.work = StreamWork(read, validated, parsed, count)

    def records(self, *, include_task_notifications: bool, include_attachments: bool,
                defer_incomplete_tail: bool, report_deferred_tail: bool, copy_records: bool = True):
        self.refresh()
        if self.deferred_tail and not defer_incomplete_tail:
            raise HistoryError(f'truncated JSONL record at byte {len(self.prefix)}')
        key = (include_task_notifications, include_attachments)
        position, records, seen = self._views.get(key, (0, [], set()))
        added: list[_Record] = []
        added_ids: set[str] = set()
        for info in self.complete[position:]:
            data, kind = info.data, info.kind
            if (kind == 'meta'
                    and (data['type'] in _STATE_ONLY or (data['type'] not in _MESSAGE_TYPES and 'uuid' not in data))
                    and not (include_task_notifications and _task_notice(data))
                    and not (include_attachments and data['type'] == 'attachment')):
                continue
            message_id = _message_id(data)
            if message_id in seen or message_id in added_ids:
                raise HistoryError(f'duplicate message identifier {message_id!r}')
            added_ids.add(message_id)
            added.append(info)
        records.extend(added)
        seen.update(added_ids)
        self._views[key] = (len(self.complete), records, seen)
        # Keep the public/private legacy list contract without exposing the
        # reusable list to callers which filter or extend their snapshot.
        result = list(records) if copy_records else records
        return (result, self.deferred_tail) if report_deferred_tail else result
