"""Retained runtime observation over HistorySource's shared canonical stream."""
from __future__ import annotations

from bisect import bisect_left, insort
from typing import Any

from .history import NativeUsagePending
from .history_activity import NativeActivity


class HistoryObservation:
    def __init__(self, source: Any) -> None:
        self.source = source
        self.revision = 0
        self.position = 0
        self.activity = NativeActivity()
        self.usage: dict[str, Any] | None = None
        self.instructions: dict[int, dict[str, Any]] = {}
        self.instruction_starts: list[int] = []

    def update(self, *, secrets: Any = ()) -> dict[str, Any]:
        projection = self.source.index_projection(secrets=secrets, since=self.revision)
        work = self.source.reading_work
        records = self.source._projection.records
        if projection['reset']:
            self.position = 0
            self.activity = NativeActivity()
            self.usage = None
            self.instructions.clear()
            self.instruction_starts.clear()
        added = records[self.position:]
        try:
            usage = self.source._usage_from_records(added)
        except NativeUsagePending:
            usage = self.usage
        self.activity.extend(added)
        self.usage = usage
        for start in projection['removed_start_bytes']:
            self._remove_instruction(start)
        for entry in projection['entries']:
            locator = entry['locator']
            start = locator['start_byte']
            if entry['source_kind'] in {'original_user', 'verified_user_answer'}:
                if start not in self.instructions:
                    insort(self.instruction_starts, start)
                self.instructions[start] = locator
            else:
                self._remove_instruction(start)
        self.position = len(records)
        self.revision = projection['revision']
        from copy import deepcopy
        return {'usage': deepcopy(self.usage), 'activity': self.activity.snapshot(),
                'instruction_bounds': {
                    'first': deepcopy(self.instructions[self.instruction_starts[0]]) if self.instruction_starts else None,
                    'last': deepcopy(self.instructions[self.instruction_starts[-1]]) if self.instruction_starts else None},
                'deferred_incomplete_tail': projection['deferred_incomplete_tail'],
                'revision': self.revision,
                'work': {**work, 'projected_records': projection['projected_records']}}

    def _remove_instruction(self, start: int) -> None:
        if start in self.instructions:
            del self.instructions[start]
            self.instruction_starts.pop(bisect_left(self.instruction_starts, start))
