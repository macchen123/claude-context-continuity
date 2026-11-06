"""Dependency-aware public projection of the canonical in-memory stream."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from .history import HistoryError, _Record, _blocks, _content, _results, redact


class HistoryProjection:
    def __init__(self, source: Any) -> None:
        self.source = source
        self.stream_revision = -1
        self.revision = 0
        self.identity = str(uuid4())
        self.reset_revision = 0
        self.changed_at: dict[int, int] = {}
        self.position = 0
        self.policy: tuple[str, ...] | None = None
        self.entries: dict[int, dict[str, Any]] = {}
        self.references: dict[str, dict[int, _Record]] = {}
        self.projected_records = 0
        self.records: list[_Record] = []

    @staticmethod
    def _ids(info: _Record) -> set[str]:
        if info.kind == 'assistant':
            return {block['id'] for block in _blocks(_content(info.data))
                    if isinstance(block, dict) and block.get('type') == 'tool_use'
                    and isinstance(block.get('id'), str) and block['id']}
        if info.kind == 'tool_result':
            return {block['tool_use_id'] for block in _results(info.data)}
        return set()

    @staticmethod
    def normalize_policy(secrets: Any) -> tuple[str, ...]:
        try:
            values = () if secrets is None else (secrets,) if isinstance(secrets, str) else tuple(secrets)
        except TypeError as exc:
            raise HistoryError('secrets must be an iterable of strings') from exc
        redact('', values)
        return tuple(sorted({value for value in values if value}))

    @classmethod
    def policy_id(cls, policy: Any) -> str:
        normalized = cls.normalize_policy(policy)
        return hashlib.sha256(json.dumps(normalized, ensure_ascii=True).encode('ascii')).hexdigest()

    def update(self, *, secrets: Any = (), since: int | None = None) -> dict[str, Any]:
        if since is not None and (type(since) is not int or since < 0):
            raise HistoryError('projection revision must be a non-negative integer')
        baseline_exists = since is not None and since <= self.revision
        policy = self.normalize_policy(secrets)
        records = self.source._records(defer_incomplete_tail=True, _copy_records=False)
        stream = self.source._stream
        self.projected_records = 0
        self.records = records
        if self.stream_revision != stream.revision or self.policy != policy:
            reset = (self.policy != policy or self.stream_revision < stream.reset_revision)
            if reset:
                self.position = 0
                self.entries.clear()
                self.references.clear()
                self.changed_at.clear()
                self.reset_revision = self.revision + 1
            added = records[self.position:]
            affected = {info.start: info for info in added}
            pending: list[str] = []
            for info in added:
                for tool_id in self._ids(info):
                    self.references.setdefault(tool_id, {})[info.start] = info
                    pending.append(tool_id)
            visited: set[str] = set()
            while pending:
                tool_id = pending.pop()
                if tool_id in visited:
                    continue
                visited.add(tool_id)
                for start, info in self.references.get(tool_id, {}).items():
                    if start not in affected:
                        affected[start] = info
                        pending.extend(self._ids(info) - visited)
            selected = [affected[start] for start in sorted(affected)]
            # Pairing and text extraction remain exclusively HistorySource logic.
            # Include the entire connected tool component: duplicate asks/results
            # can revoke an earlier verified answer, even across append boundaries.
            pairs = self.source._pairs(selected)
            try:
                entries = self.source._project_records(selected, secrets=policy, pairs=pairs)
            except HistoryError:
                # Do not acknowledge a failed projection. The next call rebuilds
                # this derived state and reports the same canonical error.
                self.stream_revision = -1
                raise
            replacement = {entry['locator']['start_byte']: entry for entry in entries}
            for start in affected:
                previous = self.entries.get(start)
                current = replacement.get(start)
                if previous != current:
                    # Retain one revision per public byte boundary, including
                    # tombstones, rather than copies of old text or raw records.
                    self.changed_at[start] = self.revision + 1
                if current is None:
                    self.entries.pop(start, None)
                else:
                    self.entries[start] = current
            self.projected_records = len(selected)
            self.revision += 1
            self.position = len(records)
            self.stream_revision = stream.revision
            self.policy = policy
        if baseline_exists and since == self.revision:
            result = {'entries': [], 'removed_start_bytes': [], 'reset': False}
        elif baseline_exists and self.reset_revision <= since < self.revision:
            starts = sorted(start for start, revision in self.changed_at.items() if revision > since)
            result = {'entries': [self.entries[start] for start in starts if start in self.entries],
                      'removed_start_bytes': [start for start in starts if start not in self.entries],
                      'reset': False}
        else:
            result = {'entries': [self.entries[start] for start in sorted(self.entries)],
                      'removed_start_bytes': [], 'reset': True}
        from copy import deepcopy
        return {**deepcopy(result), 'revision': self.revision,
                'projection_id': self.identity, 'policy_id': self.policy_id(policy),
                'deferred_incomplete_tail': stream.deferred_tail,
                'projected_records': self.projected_records}
