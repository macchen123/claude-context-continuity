"""Best-effort, metadata-only diagnostics; no runtime policy or content capture."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
from pathlib import Path

from . import core

MAX_FILE_BYTES = core.MAX_PACKET
BACKUP_COUNT = 3
MAX_STRING_LENGTH = 128
LOCK_WAIT_SECONDS = 0.25
EVENT_FILE = "events.jsonl"
LOCK_FILE = ".events.lock"

_IDENTITIES = frozenset(("context_id", "session_id", "agent_id", "prompt_id",
                         "message_id", "request_id", "usage_request_id", "model"))
_METRICS = {
    "input_tokens": "input_source",
    "cache_tokens": "cache_source",
    "cache_read_input_tokens": "cache_read_source",
    "cache_creation_input_tokens": "cache_creation_source",
    "output_tokens": "output_source",
    "window_tokens": "window_source",
    "guard_tokens": "guard_source",
    "remaining_tokens": "remaining_source",
}
_LABELS = frozenset(("hook", "hook_event", "source", "usage_source", "decision",
                     "error_origin", *_METRICS.values()))
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", re.ASCII)
_MODEL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:()\[\]/-]*", re.ASCII)
_CREDENTIAL = re.compile(r"(?:sk[-_]|gh[pousr]_|github_pat_|AKIA|ASIA|eyJ|xox[baprs]-)", re.I)


def _token(value: object, *, model: bool = False) -> str | None:
    if (type(value) is str and 0 < len(value) <= MAX_STRING_LENGTH
            and (_MODEL_TOKEN if model else _TOKEN).fullmatch(value)
            and not _CREDENTIAL.search(value)):
        return value
    return None


def _event(event: str, metadata: dict[str, object]) -> bytes | None:
    name = _token(event)
    if name is None:
        return None
    row: dict[str, object] = {
        "event": name, "timestamp_ns": time.time_ns(),
        "request_sent": None, "window_tokens": None,
    }
    for key in _IDENTITIES | _LABELS:
        if key in metadata:
            row[key] = _token(metadata[key], model=key == "model")
    for key in _METRICS:
        if key not in metadata:
            continue
        value = metadata[key]
        minimum = -(2**63) if key == "remaining_tokens" else (1 if key == "window_tokens" else 0)
        row[key] = value if type(value) is int and minimum <= value < 2**63 else None
    sent = metadata.get("request_sent")
    if type(sent) is bool:
        row["request_sent"] = sent
    core.no_secrets(row)
    return (json.dumps(row, ensure_ascii=True, allow_nan=False,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _directory(path: Path) -> int:
    # Walk by directory descriptors: no ancestor symlink or check/open race.
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("diagnostics directory must be private and owned")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _regular(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("diagnostics file must be private, owned and unaliased")


def _open(directory: int, name: str) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        _regular(os.fstat(descriptor))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _slots(directory: int) -> list[str]:
    names = [EVENT_FILE, *(f"{EVENT_FILE}.{index}" for index in range(1, BACKUP_COUNT + 1))]
    for name in names:
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _regular(info)
        if info.st_size > MAX_FILE_BYTES:
            raise ValueError("existing diagnostics exceed the size bound")
    return names


def _append(directory: int, raw: bytes) -> None:
    names = _slots(directory)
    descriptor = _open(directory, EVENT_FILE)
    try:
        size = os.fstat(descriptor).st_size
        if size and (size + len(raw) > MAX_FILE_BYTES or os.pread(descriptor, 1, size - 1) != b"\n"):
            # Rename only fixed, preflighted slots, under the same stable lock.
            for index in range(len(names) - 1, 0, -1):
                try:
                    os.replace(names[index - 1], names[index],
                               src_dir_fd=directory, dst_dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.close(descriptor)
            descriptor = -1
            descriptor = _open(directory, EVENT_FILE)
            size = 0
        try:
            remaining = memoryview(raw)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short diagnostics write")
                remaining = remaining[written:]
            os.fsync(descriptor)
        except OSError:
            os.ftruncate(descriptor, size)
            raise
    finally:
        if descriptor != -1:
            os.close(descriptor)


def record_event(directory: Path, event: str, **metadata: object) -> None:
    """Append one bounded JSONL event in a dedicated private directory, or drop it.

    Only identity fields in _IDENTITIES, labels in _LABELS, integer counts in
    _METRICS and bool/None request_sent are accepted. Labels/IDs are ASCII machine
    tokens, not prose; unknown keys and nested values are never serialized.
    Counts and their sources are independent observations: nothing is inferred.
    Absent/invalid request_sent and window_tokens are explicitly null. Strings
    over 128 characters are rejected, not truncated. Known environment secrets
    and recognizable credentials are refused even in otherwise allowed fields.

    Creates 0700 directories and 0600 files. Refuses symlinks (including path
    ancestors), hardlinks and insecure existing storage. Uses POSIX flock with
    a 250 ms contention budget; retains current plus three 64 KiB files. Faults
    are silent and lossy by design, never a reason to fail a runtime operation.
    """
    directory_fd = lock_fd = -1
    try:
        raw = _event(event, metadata)
        if raw is None or len(raw) > MAX_FILE_BYTES:
            return
        directory_fd = _directory(directory)
        lock_fd = _open(directory_fd, LOCK_FILE)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                time.sleep(min(0.01, remaining))
        current = os.stat(LOCK_FILE, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (os.fstat(lock_fd).st_dev, os.fstat(lock_fd).st_ino):
            return
        _append(directory_fd, raw)
    except Exception:
        # Diagnostic failures must not escape or print potentially sensitive errors.
        pass
    finally:
        for descriptor in (lock_fd, directory_fd):
            if descriptor != -1:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
