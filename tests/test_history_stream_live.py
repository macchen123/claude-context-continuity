import json
from pathlib import Path

import pytest

from claude_context_continuity import history
from claude_context_continuity.history import HistoryError, HistorySource


SESSION_ID = "123e4567-e89b-12d3-a456-426614174000"


def record(message_id: str, content: str | None = None) -> bytes:
    return (json.dumps({
        "type": "user", "uuid": message_id, "sessionId": SESSION_ID,
        "message": {"role": "user", "content": content or message_id},
    }) + "\n").encode()


def once_during_decode(monkeypatch, action):
    original = history._decode
    fired = False

    def decode(raw, start):
        nonlocal fired
        result = original(raw, start)
        if not fired:
            fired = True
            action()
        return result

    monkeypatch.setattr(history, "_decode", decode)


def append(path: Path, raw: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(raw)


def ids(projection):
    return [entry["locator"]["message_id"] for entry in projection["entries"]]


@pytest.mark.parametrize("warm", [False, True])
def test_live_append_preserves_bounded_snapshot_and_is_consumed_next_time(tmp_path, monkeypatch, warm):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    path.write_bytes(record("first"))
    source = HistorySource(path, SESSION_ID)
    if warm:
        source.index_projection()
        append(path, record("second"))
    expected = ["first", "second"] if warm else ["first"]
    next_id = "third" if warm else "second"
    once_during_decode(monkeypatch, lambda: append(path, record(next_id)))

    snapshot = source.index_projection()

    assert ids(snapshot) == expected
    assert source.reading_work["parsed_records"] == 1
    assert ids(source.index_projection()) == [*expected, next_id]
    assert source.reading_work["parsed_records"] == 1
    assert ids(source.index_projection()) == [*expected, next_id]
    assert source.reading_work["read_bytes"] == 0


def test_live_tail_completion_is_deferred_to_the_next_snapshot(tmp_path, monkeypatch):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    second = record("second")
    path.write_bytes(record("first") + second[:-8])
    source = HistorySource(path, SESSION_ID)
    once_during_decode(monkeypatch, lambda: append(path, second[-8:]))

    snapshot = source.index_projection()

    assert ids(snapshot) == ["first"]
    assert snapshot["deferred_incomplete_tail"] is True
    completed = source.index_projection()
    assert ids(completed) == ["first", "second"]
    assert completed["deferred_incomplete_tail"] is False


@pytest.mark.parametrize("change", ["mutate_and_append", "truncate", "replace"])
def test_live_snapshot_rejects_changed_prefix_truncation_and_replacement(tmp_path, monkeypatch, change):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    first = record("first", "original")
    path.write_bytes(first)
    source = HistorySource(path, SESSION_ID)

    def change_source():
        if change == "mutate_and_append":
            path.write_bytes(record("first", "modified") + record("second"))
        elif change == "truncate":
            path.write_bytes(b"")
        else:
            replacement = tmp_path / "replacement.jsonl"
            replacement.write_bytes(first + record("second"))
            replacement.replace(path)

    once_during_decode(monkeypatch, change_source)
    with pytest.raises(HistoryError, match="history source changed"):
        source.index_projection()
    assert source._stream.complete == []
    assert source._stream.signature is None
