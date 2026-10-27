"""Metadata privacy, bounded storage and real multi-process diagnostic writes."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import diagnostics  # noqa: E402


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    return tmp_path.resolve() / "diagnostics"


def records(directory: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (directory / diagnostics.EVENT_FILE).read_bytes().splitlines()]


def private_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700)


def private_file(path: Path, data: bytes = b"original\n") -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def test_complete_allowlist_and_sources(directory: Path) -> None:
    metadata = {
        "context_id": "context-1", "session_id": "session-1", "agent_id": "agent-1",
        "prompt_id": "prompt-1", "message_id": "msg_1", "request_id": "req_1",
        "usage_request_id": "req_usage_0",
        "model": "claude-sonnet-4-6", "hook": "PreToolUse", "hook_event": "PostToolUse",
        "source": "native_usage", "usage_source": "native_usage",
        "decision": "observe", "error_origin": "native_synthetic", "request_sent": False,
        "input_tokens": 100, "input_source": "native_usage",
        "cache_tokens": 90, "cache_source": "native_usage",
        "cache_read_input_tokens": 80, "cache_read_source": "native_usage",
        "cache_creation_input_tokens": 10, "cache_creation_source": "native_usage",
        "output_tokens": 20, "output_source": "native_usage",
        "window_tokens": 200000, "window_source": "native_contextWindow",
        "guard_tokens": 1000, "guard_source": "local_budget",
        "remaining_tokens": -10, "remaining_source": "observed_budget",
    }
    assert diagnostics.record_event(directory, "budget_observation", **metadata) is None
    row, = records(directory)
    assert row == {**metadata, "event": "budget_observation", "timestamp_ns": row["timestamp_ns"]}
    assert type(row["timestamp_ns"]) is int


@pytest.mark.parametrize("sent", [None, 0, 1, "true", "false", {}, [], 1.0])
@pytest.mark.parametrize("window", [None, 0, -1, False, True, "200000", 200000.0, {}, 2**63])
def test_unknown_sent_and_window_are_never_inferred(directory: Path, sent: object, window: object) -> None:
    diagnostics.record_event(directory, "observation", request_sent=sent, window_tokens=window,
                             model="claude-opus-4-6", decision="send", error_origin="provider")
    row, = records(directory)
    assert row["request_sent"] is None
    assert row["window_tokens"] is None
    assert "window_source" not in row


def test_absent_fields_stay_unknown_and_explicit_false_is_preserved(directory: Path) -> None:
    diagnostics.record_event(directory, "unknown")
    diagnostics.record_event(directory, "not_sent", request_sent=False)
    diagnostics.record_event(directory, "sent", request_sent=True)
    rows = records(directory)
    assert [row["request_sent"] for row in rows] == [None, False, True]
    assert all(row["window_tokens"] is None for row in rows)
    assert all("input_tokens" not in row for row in rows)


def test_model_id_characters_are_field_specific_and_privacy_checked(
    directory: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for model in ("gpt-6-astra(max)[1M]", "gpt-6.1-sol(high)[1M]",
                  "provider/gpt-6-astra(max)[1M]"):
        diagnostics.record_event(directory, "observation", model=model, context_id=model, source=model)
        row = records(directory)[-1]
        assert row["model"] == model
        assert row["context_id"] is row["source"] is None
    for invalid in ("gpt model", "gpt-6\nsecret", {"model": "gpt-6"},
                    "provider/sk-ant-secret", "gpt-6(" + "a" * 128 + ")"):
        diagnostics.record_event(directory, "observation", model=invalid)
        assert records(directory)[-1]["model"] is None
    secret = "private-model-credential"
    monkeypatch.setenv("DIAGNOSTICS_TEST_API_KEY", secret)
    before = (directory / diagnostics.EVENT_FILE).read_bytes()
    diagnostics.record_event(directory, "observation", model=f"provider/{secret}(high)[1M]")
    assert (directory / diagnostics.EVENT_FILE).read_bytes() == before
    assert secret.encode() not in before


def test_runtime_agent_budget_contract_and_no_carry_forward(directory: Path) -> None:
    diagnostics.record_event(directory, "agent_budget", request_sent=True, window_tokens=200000,
                             input_tokens=100, usage_source="native_usage", request_id="req-1")
    metadata = {
        "context_id": "context-1", "session_id": "session-1", "agent_id": "agent-1",
        "prompt_id": "prompt-2", "message_id": "msg-2", "request_id": None,
        "usage_request_id": "req-1",
        "model": "claude-opus-4-6", "hook_event": "Stop", "decision": "observe",
        "input_tokens": 100, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 10,
        "output_tokens": 20, "window_tokens": None, "window_source": None,
        "usage_source": "prior_success", "guard_tokens": 1000, "remaining_tokens": None,
        "request_sent": None, "error_origin": "native_synthetic",
    }
    diagnostics.record_event(directory, "agent_budget", **metadata)
    row = records(directory)[-1]
    assert row == {**metadata, "event": "agent_budget", "timestamp_ns": row["timestamp_ns"]}
    assert row["request_sent"] is row["window_tokens"] is None


@pytest.mark.parametrize("invalid", [-1, True, "10", 10.5, float("nan"), float("inf"), 2**63, []])
def test_counts_reject_invalid_or_unbounded_values(directory: Path, invalid: object) -> None:
    diagnostics.record_event(directory, "usage", input_tokens=invalid, output_tokens=0,
                             remaining_tokens=-(2**63))
    row, = records(directory)
    assert row["input_tokens"] is None
    assert row["output_tokens"] == 0
    assert row["remaining_tokens"] == -(2**63)


def test_content_keys_and_nested_objects_are_not_serialized(directory: Path) -> None:
    class Unprintable:
        def __str__(self) -> str:
            pytest.fail("metadata objects must never be stringified")

        __repr__ = __str__

    content = "private prompt, tool output and arbitrary error text"
    diagnostics.record_event(
        directory, "safe_event", context_id="context-1", model={"text": content},
        source=[content], decision=Unprintable(), error_origin=RuntimeError(content),
        prompt=content, tool_input=content, tool_output=content, authorization="Bearer secret",
        headers={"Authorization": "Bearer secret"}, password=content, error=content,
        error_message=content, message=content, input={"text": content}, output=content,
        timestamp_ns=content, **{"window_tokens.source": content},
    )
    raw = (directory / diagnostics.EVENT_FILE).read_text()
    assert content not in raw
    assert "Bearer" not in raw
    row, = records(directory)
    assert set(row) == {"event", "timestamp_ns", "request_sent", "window_tokens", "context_id",
                        "model", "source", "decision", "error_origin"}
    assert row["model"] is row["source"] is row["decision"] is row["error_origin"] is None


@pytest.mark.parametrize("value", ["a" * 129, "private content", "line\nbreak", "\x00",
                                   "授权内容", "Authorization:Bearer-secret", "password=secret",
                                   "https://user:password@host", "Bearer secret", "sk-ant-secret",
                                   "github_pat_secret", "ghp_secret", "AKIA0123456789ABCDEF",
                                   "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"])
def test_disallowed_strings_and_recognizable_credentials(directory: Path, value: str) -> None:
    diagnostics.record_event(directory, "observation", context_id=value, error_origin=value)
    path = directory / diagnostics.EVENT_FILE
    if path.exists():
        row, = records(directory)
        assert row["context_id"] is row["error_origin"] is None
        assert value not in path.read_text()
    else:
        # core.no_secrets may reject the whole row, rather than just its value.
        assert not directory.exists()


def test_string_boundary_is_preserved_not_truncated(directory: Path) -> None:
    diagnostics.record_event(directory, "observation", context_id="a" * 128)
    assert records(directory)[0]["context_id"] == "a" * 128


@pytest.mark.parametrize("event", ["private content", "sk-ant-secret", "x" * 129, {}, None])
def test_invalid_event_is_dropped_without_creating_storage(directory: Path, event: object) -> None:
    diagnostics.record_event(directory, event)
    assert not directory.exists()


@pytest.mark.parametrize("field", ["context_id", "source", "event"])
def test_known_environment_secret_prevents_entire_record(directory: Path, monkeypatch: pytest.MonkeyPatch,
                                                         field: str) -> None:
    secret = "opaque-known-credential-1234"
    monkeypatch.setenv("DIAGNOSTICS_TEST_API_KEY", secret)
    metadata = {field: f"prefix-{secret}-suffix"}
    event = metadata.pop("event", "observation")
    diagnostics.record_event(directory, event, **metadata)
    assert not directory.exists()


def test_rollover_retention_and_file_size_bound(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "MAX_FILE_BYTES", 600)
    for index in range(70):
        diagnostics.record_event(directory, "observation", request_id=f"request-{index}")
    names = {path.name for path in directory.iterdir()}
    assert names == {diagnostics.LOCK_FILE, diagnostics.EVENT_FILE,
                     *(f"{diagnostics.EVENT_FILE}.{i}" for i in range(1, 4))}
    retained = []
    for index in range(3, -1, -1):
        path = directory / (f"{diagnostics.EVENT_FILE}.{index}" if index else diagnostics.EVENT_FILE)
        assert 0 < path.stat().st_size <= 600
        retained.extend(json.loads(line)["request_id"] for line in path.read_bytes().splitlines())
    numbers = [int(value.removeprefix("request-")) for value in retained]
    assert numbers == list(range(numbers[0], 70))
    assert numbers[0] > 0


def test_oversized_event_is_dropped_before_storage(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "MAX_FILE_BYTES", 10)
    diagnostics.record_event(directory, "observation")
    assert not directory.exists()


def test_incomplete_tail_rolls_over_before_recovery(directory: Path) -> None:
    private_directory(directory)
    private_file(directory / diagnostics.EVENT_FILE, b'{"interrupted":')
    diagnostics.record_event(directory, "recovered")
    assert records(directory)[0]["event"] == "recovered"
    assert (directory / f"{diagnostics.EVENT_FILE}.1").read_bytes() == b'{"interrupted":'


def test_permissions_even_with_permissive_umask(directory: Path) -> None:
    previous = os.umask(0)
    try:
        diagnostics.record_event(directory / "child", "observation")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "child").stat().st_mode) == 0o700
    for path in (directory / "child").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("ancestor", [False, True])
def test_directory_and_ancestor_symlinks_are_refused(directory: Path, ancestor: bool) -> None:
    target = directory.parent / "target"
    target.mkdir(mode=0o700)
    directory.symlink_to(target, target_is_directory=True)
    diagnostics.record_event(directory / "child" if ancestor else directory, "observation")
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("name", [diagnostics.EVENT_FILE, diagnostics.LOCK_FILE, "events.jsonl.1"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_unsafe_storage_slots_never_touch_external_content(directory: Path, name: str, kind: str) -> None:
    private_directory(directory)
    external = directory.parent / "external"
    private_file(external)
    path = directory / name
    if kind == "symlink":
        path.symlink_to(external)
    elif kind == "hardlink":
        os.link(external, path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.mkdir(mode=0o700)
    before = external.stat()
    slot_before = path.lstat()
    diagnostics.record_event(directory, "observation")
    after = external.stat()
    slot_after = path.lstat()
    assert (slot_before.st_ino, slot_before.st_mode, slot_before.st_size) == (
        slot_after.st_ino, slot_after.st_mode, slot_after.st_size)
    assert external.read_bytes() == b"original\n"
    assert (before.st_mode, before.st_mtime_ns, before.st_size) == (
        after.st_mode, after.st_mtime_ns, after.st_size)
    if name != diagnostics.EVENT_FILE:
        assert not (directory / diagnostics.EVENT_FILE).exists()


def test_dangling_symlink_is_not_followed(directory: Path) -> None:
    private_directory(directory)
    target = directory.parent / "missing"
    (directory / diagnostics.EVENT_FILE).symlink_to(target)
    diagnostics.record_event(directory, "observation")
    assert not target.exists()


def test_insecure_existing_directory_is_untouched(directory: Path) -> None:
    directory.mkdir()
    directory.chmod(0o755)
    diagnostics.record_event(directory, "observation")
    assert list(directory.iterdir()) == []
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


@pytest.mark.parametrize("name", [diagnostics.EVENT_FILE, diagnostics.LOCK_FILE, "events.jsonl.2"])
def test_insecure_existing_file_is_untouched(directory: Path, name: str) -> None:
    private_directory(directory)
    path = directory / name
    private_file(path)
    path.chmod(0o644)
    diagnostics.record_event(directory, "observation")
    assert path.read_bytes() == b"original\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_foreign_directory_is_refused(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private_directory(directory)
    monkeypatch.setattr(diagnostics.os, "getuid", lambda: directory.stat().st_uid + 1)
    diagnostics.record_event(directory, "observation")
    assert list(directory.iterdir()) == []


def test_existing_oversized_storage_is_not_grown(directory: Path) -> None:
    private_directory(directory)
    path = directory / diagnostics.EVENT_FILE
    original = b"x" * (diagnostics.MAX_FILE_BYTES + 1)
    private_file(path, original)
    diagnostics.record_event(directory, "observation")
    assert path.read_bytes() == original


def test_lock_contention_is_bounded_and_next_event_recovers(directory: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    private_directory(directory)
    lock = directory / diagnostics.LOCK_FILE
    private_file(lock, b"")
    assert diagnostics.LOCK_WAIT_SECONDS == 0.25
    elapsed = 0.0

    def advance_clock(delay: float) -> None:
        nonlocal elapsed
        assert 0 < delay <= 0.01
        elapsed += delay

    with lock.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with monkeypatch.context() as patcher:
            patcher.setattr(diagnostics.time, "monotonic", lambda: elapsed)
            patcher.setattr(diagnostics.time, "sleep", advance_clock)
            diagnostics.record_event(directory, "dropped")
        assert elapsed == pytest.approx(0.25)
        assert not (directory / diagnostics.EVENT_FILE).exists()
    diagnostics.record_event(directory, "recovered")
    assert [row["event"] for row in records(directory)] == ["recovered"]


def test_changed_lock_inode_is_refused(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_flock = fcntl.flock

    def replaced_lock(descriptor: int, operation: int) -> None:
        real_flock(descriptor, operation)
        replacement = directory / "replacement"
        private_file(replacement, b"")
        os.replace(replacement, directory / diagnostics.LOCK_FILE)

    monkeypatch.setattr(diagnostics.fcntl, "flock", replaced_lock)
    diagnostics.record_event(directory, "dropped")
    assert not (directory / diagnostics.EVENT_FILE).exists()


def test_partial_writes_complete_one_record(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_write = os.write

    def partial(descriptor: int, data: memoryview) -> int:
        return original_write(descriptor, data[:max(1, len(data) // 2)])

    with monkeypatch.context() as patcher:
        patcher.setattr(diagnostics.os, "write", partial)
        diagnostics.record_event(directory, "observation")
    assert records(directory)[0]["event"] == "observation"


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_io_failure_rolls_back_partial_event_and_recovers(directory: Path, monkeypatch: pytest.MonkeyPatch,
                                                         failure: str) -> None:
    diagnostics.record_event(directory, "before")
    path = directory / diagnostics.EVENT_FILE
    original = path.read_bytes()
    write = os.write
    calls = 0

    def broken_write(descriptor: int, data: memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return write(descriptor, data[:10])
        raise OSError("private error content must not be printed")

    def broken_sync(descriptor: int) -> None:
        raise OSError("private error content must not be printed")

    with monkeypatch.context() as patcher:
        patcher.setattr(diagnostics.os, failure, broken_write if failure == "write" else broken_sync)
        diagnostics.record_event(directory, "dropped")
    assert path.read_bytes() == original
    diagnostics.record_event(directory, "after")
    assert [row["event"] for row in records(directory)] == ["before", "after"]


def test_telemetry_faults_are_silent(directory: Path, monkeypatch: pytest.MonkeyPatch,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("private error detail")

    monkeypatch.setattr(diagnostics.os, "open", broken)
    assert diagnostics.record_event(directory, "observation") is None
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("rotate", [False, True])
def test_actual_concurrent_processes(directory: Path, rotate: bool) -> None:
    code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from claude_context_continuity import diagnostics
diagnostics.LOCK_WAIT_SECONDS = 10.0
if sys.argv[4] == 'True':
    diagnostics.MAX_FILE_BYTES = 1600
    diagnostics.BACKUP_COUNT = 3
for index in range(20):
    diagnostics.record_event(Path(sys.argv[2]), 'observation', request_id=f'{sys.argv[3]}-{index}')
"""
    processes = [subprocess.Popen([sys.executable, "-B", "-c", code, str(ROOT / "src"),
                                   str(directory), str(worker), str(rotate)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for worker in range(4)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr.decode()
            assert stdout == stderr == b""
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    found = []
    for path in directory.glob("events.jsonl*"):
        raw = path.read_bytes()
        assert raw.endswith(b"\n")
        assert len(raw) <= (1600 if rotate else diagnostics.MAX_FILE_BYTES)
        found.extend(json.loads(line)["request_id"] for line in raw.splitlines())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(found) == len(set(found))
    expected = {f"{worker}-{index}" for worker in range(4) for index in range(20)}
    if rotate:
        assert 0 < len(found) < len(expected)
        assert set(found) <= expected
        assert len(list(directory.glob("events.jsonl*"))) == 4
    else:
        assert set(found) == expected
