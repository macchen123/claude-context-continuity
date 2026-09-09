"""Fail-closed compatibility repair for native durable ``CronCreate`` records.

The native scheduler remains the only scheduler and task store.  This module only
rebinds the creator session of the exact durable task that the current native
``CronCreate`` call has just written, and records enough private undo metadata to
restore that one field after the owned native process exits.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from . import core

_TASKS_RELATIVE = ".claude/scheduled_tasks.json"
_LOCK_RELATIVE = ".claude/scheduled_tasks.lock"
_RECEIPTS_RELATIVE = "cron_compat"
_RECEIPT_KIND = "cclaude-durable-cron-undo"
_RECEIPT_VERSION = 1
_MAX_ID_LENGTH = 128
_MAX_JSON_BYTES = 4 * 1024 * 1024


class _Fault(Exception):
    """A stable, non-sensitive reason for declining a native-file mutation."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _SnapshotChanged(_Fault):
    def __init__(self):
        super().__init__("changed_snapshot")


def _summary(status: str, reason: str, task_id: str | None = None) -> dict[str, Any]:
    return {"status": status, "task_id": task_id, "reason": reason}


def _positive_pid(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _Fault("invalid_state")
    return value


def _session_id(value: Any) -> str:
    try:
        return core.uuid(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise _Fault("invalid_state") from exc


def _task_id(value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value) > _MAX_ID_LENGTH
            or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)):
        raise _Fault("invalid_task_id")
    return value


def _nonempty_string(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value:
        raise _Fault(reason)
    return value


def _created_at(value: Any, reason: str) -> int | float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))):
        raise _Fault(reason)
    return value


def _runtime_state(state: Any, *, require_enabled: bool) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise _Fault("invalid_state")
    cwd_value = state.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value or not Path(cwd_value).is_absolute():
        raise _Fault("invalid_state")
    try:
        cwd = Path(cwd_value).resolve(strict=True)
    except OSError as exc:
        raise _Fault("invalid_state") from exc
    if not cwd.is_dir():
        raise _Fault("invalid_state")

    owned_pid = _positive_pid(state.get("owned_pid"))
    tmux = state.get("tmux")
    if not isinstance(tmux, dict) or _positive_pid(tmux.get("pane_pid")) != owned_pid:
        raise _Fault("invalid_state")

    compat = state.get("durable_cron_compat")
    if not isinstance(compat, dict) or type(compat.get("enabled")) is not bool:
        raise _Fault("invalid_state")
    scheduler_session_id = _session_id(compat.get("scheduler_session_id"))
    session_id = _session_id(state.get("session_id"))
    if require_enabled and not compat["enabled"]:
        return {
            "cwd": cwd,
            "owned_pid": owned_pid,
            "session_id": session_id,
            "scheduler_session_id": scheduler_session_id,
            "enabled": False,
        }
    return {
        "cwd": cwd,
        "owned_pid": owned_pid,
        "session_id": session_id,
        "scheduler_session_id": scheduler_session_id,
        "enabled": compat["enabled"],
    }


def _runtime_directory(directory: Any) -> Path:
    try:
        path = Path(directory)
    except TypeError as exc:
        raise _Fault("invalid_runtime_directory") from exc
    if path.is_symlink() or not path.is_dir():
        raise _Fault("invalid_runtime_directory")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise _Fault("invalid_runtime_directory") from exc


def _safe_native_paths(cwd: Path) -> tuple[Path, Path]:
    """Resolve all native paths before any file is read or receipt is written."""
    try:
        tasks = core.safe_path(cwd, _TASKS_RELATIVE, exists=False)
        lock = core.safe_path(cwd, _LOCK_RELATIVE, exists=False)
    except (OSError, ValueError, core.ContinuityError) as exc:
        raise _Fault("unsafe_path") from exc
    return tasks, lock


def _raw_snapshot(path: Path) -> tuple[bytes, int]:
    """Read one bounded regular file and reject aliases or an unstable read."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _Fault("io_error") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _Fault("unsafe_path")
    if before.st_size > _MAX_JSON_BYTES:
        raise _Fault("oversized_json")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _Fault("unsafe_path" if getattr(exc, "errno", None) == errno.ELOOP else "io_error") from exc
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino or opened.st_size > _MAX_JSON_BYTES):
            raise _SnapshotChanged()
        blocks: list[bytes] = []
        remaining = _MAX_JSON_BYTES + 1
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                break
            blocks.append(block)
            remaining -= len(block)
        raw = b"".join(blocks)
        if len(raw) > _MAX_JSON_BYTES:
            raise _Fault("oversized_json")
    finally:
        os.close(descriptor)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise _SnapshotChanged() from exc
    if (after.st_dev != before.st_dev or after.st_ino != before.st_ino
            or after.st_size != before.st_size or stat.S_ISLNK(after.st_mode)):
        raise _SnapshotChanged()
    return raw, stat.S_IMODE(before.st_mode)


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonstandard_number(_: str) -> Any:
    raise ValueError("nonstandard JSON number")


def _json_document(raw: bytes, *, receipt: bool = False) -> Any:
    try:
        text = raw.decode("utf-8")
        return json.loads(text, object_pairs_hook=_without_duplicate_keys,
                          parse_constant=_reject_nonstandard_number)
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _Fault("malformed_receipt" if receipt else "malformed_json") from exc


def _tasks_document(raw: bytes) -> dict[str, Any]:
    document = _json_document(raw)
    if not isinstance(document, dict) or set(document) != {"tasks"} or not isinstance(document["tasks"], list):
        raise _Fault("malformed_task_file")
    seen: set[str] = set()
    for task in document["tasks"]:
        if not isinstance(task, dict):
            raise _Fault("malformed_task_file")
        task_id = _task_id(task.get("id"))
        if task_id in seen:
            raise _Fault("duplicate_task_id")
        seen.add(task_id)
    return document


def _lock_identity(raw: bytes) -> tuple[int, str]:
    lock = _json_document(raw)
    if not isinstance(lock, dict):
        raise _Fault("malformed_lock")
    # The lock session is intentionally not compared to the captured startup ID.
    _nonempty_string(lock.get("sessionId"), "malformed_lock")
    return _positive_pid(lock.get("pid")), _nonempty_string(lock.get("procStart"), "malformed_lock")


def _task_identity(task: dict[str, Any]) -> tuple[str, int, str, int | float]:
    return (
        _nonempty_string(task.get("createdBySessionId"), "malformed_task_record"),
        _positive_pid(task.get("createdByPid")),
        _nonempty_string(task.get("createdByProcStart"), "malformed_task_record"),
        _created_at(task.get("createdAt"), "malformed_task_record"),
    )


def _same_value(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _input_matches(task: dict[str, Any], tool_input: dict[str, Any]) -> bool:
    if not isinstance(tool_input.get("prompt"), str) or not isinstance(tool_input.get("cron"), str):
        return False
    recurring_input = tool_input.get("recurring", True)
    if type(recurring_input) is not bool:
        return False
    if not _same_value(task.get("prompt"), tool_input["prompt"]):
        return False
    if not _same_value(task.get("cron"), tool_input["cron"]):
        return False
    # Native persistence omits a false recurring flag, so omitted means exactly false.
    recurring = task.get("recurring", False)
    return type(recurring) is bool and recurring is recurring_input


def _response_payload(response: Any) -> tuple[str | None, str | None]:
    """Return only an explicit direct or ``data`` response ID; never search text."""
    if not isinstance(response, dict):
        return None, "missing_response_id"
    direct = "id" in response
    nested = response.get("data")
    nested_id = isinstance(nested, dict) and "id" in nested
    if direct and nested_id:
        return None, "ambiguous_response"
    if direct:
        payload = response
    elif nested_id:
        payload = nested
    else:
        return None, "missing_response_id"
    try:
        task_id = _task_id(payload.get("id"))
    except _Fault:
        return None, "invalid_response_id"
    for candidate in (payload, response if payload is not response else None):
        if isinstance(candidate, dict) and "durable" in candidate and candidate["durable"] is not True:
            return None, "response_not_durable"
    return task_id, None


def _event_task_id(event: Any, runtime: dict[str, Any]) -> tuple[str | None, str | None]:
    if not isinstance(event, dict):
        return None, "invalid_event"
    if event.get("hook_event_name") != "PostToolUse":
        return None, "not_post_tool_use"
    if event.get("tool_name") != "CronCreate":
        return None, "not_cron_create"
    if event.get("agent_id") or event.get("agentId"):
        return None, "agent_event"
    if event.get("session_id") != runtime["session_id"]:
        return None, "session_mismatch"
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None, "malformed_tool_input"
    if tool_input.get("durable") is not True:
        return None, "session_only_task"
    if (not isinstance(tool_input.get("prompt"), str) or not isinstance(tool_input.get("cron"), str)
            or type(tool_input.get("recurring", True)) is not bool):
        return None, "malformed_tool_input"
    return _response_payload(event.get("tool_response"))


def _task_fingerprint(task: dict[str, Any]) -> str:
    if not isinstance(task.get("prompt"), str):
        raise _Fault("malformed_task_record")
    protected: dict[str, Any] = {}
    for key, value in task.items():
        if key in {"createdBySessionId", "lastFiredAt"}:
            continue
        protected[key] = ({"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
                          if key == "prompt" else value)
    try:
        encoded = json.dumps(protected, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _Fault("malformed_task_record") from exc
    return hashlib.sha256(encoded).hexdigest()


def _receipt_for(task: dict[str, Any], *, original_session_id: str,
                 bound_session_id: str, pid: int, proc_start: str,
                 created_at: int | float) -> dict[str, Any]:
    receipt = {
        "kind": _RECEIPT_KIND,
        "version": _RECEIPT_VERSION,
        "task_id": _task_id(task.get("id")),
        "original_session_id": original_session_id,
        "bound_session_id": bound_session_id,
        "pid": pid,
        "proc_start": proc_start,
        "created_at": created_at,
        "fingerprint": _task_fingerprint(task),
    }
    try:
        core.no_secrets(receipt)
    except core.ContinuityError as exc:
        raise _Fault("unsafe_receipt") from exc
    return receipt


def _validate_receipt(value: Any, *, filename: str | None = None) -> dict[str, Any]:
    expected = {
        "kind", "version", "task_id", "original_session_id", "bound_session_id",
        "pid", "proc_start", "created_at", "fingerprint",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise _Fault("malformed_receipt")
    if value.get("kind") != _RECEIPT_KIND or value.get("version") != _RECEIPT_VERSION:
        raise _Fault("malformed_receipt")
    task_id = _task_id(value.get("task_id"))
    original = _session_id(value.get("original_session_id"))
    bound = _session_id(value.get("bound_session_id"))
    pid = _positive_pid(value.get("pid"))
    proc_start = _nonempty_string(value.get("proc_start"), "malformed_receipt")
    created_at = _created_at(value.get("created_at"), "malformed_receipt")
    fingerprint = value.get("fingerprint")
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)):
        raise _Fault("malformed_receipt")
    if filename is not None and filename != _receipt_filename(task_id):
        raise _Fault("malformed_receipt")
    return {
        "kind": _RECEIPT_KIND,
        "version": _RECEIPT_VERSION,
        "task_id": task_id,
        "original_session_id": original,
        "bound_session_id": bound,
        "pid": pid,
        "proc_start": proc_start,
        "created_at": created_at,
        "fingerprint": fingerprint,
    }


def _receipt_filename(task_id: str) -> str:
    return "undo-" + hashlib.sha256(task_id.encode("utf-8")).hexdigest() + ".json"


def _receipt_path(directory: Path, task_id: str, *, create_parent: bool) -> Path | None:
    try:
        parent = core.safe_path(directory, _RECEIPTS_RELATIVE, exists=False)
    except (OSError, ValueError, core.ContinuityError) as exc:
        raise _Fault("unsafe_path") from exc
    if parent.exists():
        if not parent.is_dir():
            raise _Fault("unsafe_path")
    elif not create_parent:
        return None
    else:
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise _Fault("changed_snapshot") from exc
        except OSError as exc:
            raise _Fault("io_error") from exc
        try:
            parent = core.safe_path(directory, _RECEIPTS_RELATIVE, exists=True)
        except (OSError, ValueError, core.ContinuityError) as exc:
            raise _Fault("unsafe_path") from exc
        if not parent.is_dir():
            raise _Fault("unsafe_path")
    try:
        return core.safe_path(directory, f"{_RECEIPTS_RELATIVE}/{_receipt_filename(task_id)}", exists=False)
    except (OSError, ValueError, core.ContinuityError) as exc:
        raise _Fault("unsafe_path") from exc


def _serialized(value: Any) -> bytes:
    try:
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                          allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _Fault("malformed_task_record") from exc
    if len(raw) > _MAX_JSON_BYTES:
        raise _Fault("oversized_json")
    return raw


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _replace_if_unchanged(path: Path, snapshot: bytes | None, value: Any, mode: int) -> None:
    """Write a fsynced temp file, compare raw bytes once, then make one atomic swap."""
    raw = _serialized(value)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".durable-cron-", dir=path.parent)
    temporary = Path(temporary_name)
    linked = False
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if snapshot is None:
            if path.exists() or path.is_symlink():
                raise _SnapshotChanged()
            try:
                os.link(temporary, path)
                linked = True
            except FileExistsError as exc:
                raise _SnapshotChanged() from exc
        else:
            current, _ = _raw_snapshot(path)
            if current != snapshot:
                raise _SnapshotChanged()
            os.replace(temporary, path)
            linked = True
        _fsync_directory(path.parent)
    finally:
        if not linked:
            temporary.unlink(missing_ok=True)
        elif snapshot is None:
            temporary.unlink(missing_ok=True)


def _load_receipt(path: Path) -> dict[str, Any]:
    raw, mode = _raw_snapshot(path)
    if mode & 0o077:
        raise _Fault("unsafe_receipt")
    return _validate_receipt(_json_document(raw, receipt=True), filename=path.name)


def _ensure_receipt(directory: Path, receipt: dict[str, Any]) -> None:
    path = _receipt_path(directory, receipt["task_id"], create_parent=True)
    assert path is not None
    if path.exists():
        if _load_receipt(path) != receipt:
            raise _Fault("receipt_conflict")
        return
    _replace_if_unchanged(path, None, receipt, 0o600)


def _native_durable_eligible(task: dict[str, Any], scheduler_session_id: str) -> bool:
    """Local, deliberately narrow reproduction of the durable session-ID filter."""
    return task.get("createdBySessionId") == scheduler_session_id


def repair_created_task(state: dict, event: dict, directory: Path) -> dict:
    """Bind one freshly-created durable native task to the scheduler startup session.

    The caller must invoke this synchronously from a successful parent
    ``PostToolUse`` event.  All non-exact or unsafe situations leave native files
    untouched and return a compact reason instead.
    """
    task_id: str | None = None
    try:
        runtime = _runtime_state(state, require_enabled=True)
        runtime_directory = _runtime_directory(directory)
        if not runtime["enabled"]:
            return _summary("skipped", "disabled")
        task_id, event_reason = _event_task_id(event, runtime)
        if event_reason is not None:
            return _summary("skipped", event_reason)
        assert task_id is not None
        tool_input = event["tool_input"]

        tasks_path, lock_path = _safe_native_paths(runtime["cwd"])
        if not tasks_path.exists():
            return _summary("skipped", "task_file_missing", task_id)
        if not lock_path.exists():
            return _summary("skipped", "scheduler_lock_missing", task_id)
        task_snapshot, task_mode = _raw_snapshot(tasks_path)
        lock_snapshot, _ = _raw_snapshot(lock_path)
        document = _tasks_document(task_snapshot)
        lock_pid, lock_proc_start = _lock_identity(lock_snapshot)
        task = next((row for row in document["tasks"] if row["id"] == task_id), None)
        if task is None:
            return _summary("skipped", "task_not_found", task_id)
        if not _input_matches(task, tool_input):
            return _summary("skipped", "task_input_mismatch", task_id)
        creator_session_id, creator_pid, creator_proc_start, created_at = _task_identity(task)
        if (creator_pid != runtime["owned_pid"] or lock_pid != runtime["owned_pid"]
                or lock_proc_start != creator_proc_start):
            return _summary("skipped", "process_mismatch", task_id)

        if _native_durable_eligible(task, runtime["scheduler_session_id"]):
            return _summary("not_needed", "already_startup_bound", task_id)
        if creator_session_id != runtime["session_id"]:
            return _summary("skipped", "creator_session_mismatch", task_id)

        receipt = _receipt_for(task, original_session_id=creator_session_id,
                               bound_session_id=runtime["scheduler_session_id"],
                               pid=creator_pid, proc_start=creator_proc_start,
                               created_at=created_at)
        # This is intentionally before the task update.  A crash here is safe to resume.
        _ensure_receipt(runtime_directory, receipt)
        current_lock, _ = _raw_snapshot(lock_path)
        if current_lock != lock_snapshot:
            raise _SnapshotChanged()
        task["createdBySessionId"] = runtime["scheduler_session_id"]
        _replace_if_unchanged(tasks_path, task_snapshot, document, task_mode)
        return _summary("applied", "bound_startup_session", task_id)
    except _Fault as exc:
        return _summary("error", exc.reason, task_id)
    except (OSError, TypeError, ValueError):
        return _summary("error", "io_error", task_id)


def _owner_exited(pid: int) -> tuple[bool, str | None]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True, None
    except PermissionError:
        return False, "owned_process_status_unknown"
    except OSError:
        return False, "owned_process_status_unknown"
    return False, "owned_process_alive"


def _receipt_files(directory: Path) -> list[Path]:
    try:
        parent = core.safe_path(directory, _RECEIPTS_RELATIVE, exists=False)
    except (OSError, ValueError, core.ContinuityError) as exc:
        raise _Fault("unsafe_path") from exc
    if not parent.exists():
        return []
    if not parent.is_dir():
        raise _Fault("unsafe_path")
    result: list[Path] = []
    try:
        children = sorted(parent.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise _Fault("io_error") from exc
    for child in children:
        if child.name.startswith("undo-") and child.name.endswith(".json"):
            try:
                safe_child = core.safe_path(directory, f"{_RECEIPTS_RELATIVE}/{child.name}", exists=True)
            except (OSError, ValueError, core.ContinuityError) as exc:
                raise _Fault("unsafe_path") from exc
            result.append(safe_child)
    return result


def _offline_lock_snapshot(path: Path) -> bytes | None:
    """撤销时同目录的新原生调度器也必须已退出，不能只检查旧 owner。"""
    if not path.exists():
        return None
    raw, _ = _raw_snapshot(path)
    pid, _ = _lock_identity(raw)
    exited, reason = _owner_exited(pid)
    if not exited:
        raise _Fault("native_scheduler_alive" if reason == "owned_process_alive" else "native_scheduler_status_unknown")
    return raw


def restore_bindings(state: dict, directory: Path) -> dict:
    """Restore exact private receipts only after the recorded owner is gone.

    Receipts are retained as an audit trail.  Missing/cancelled tasks are never
    recreated, and an externally changed task is left untouched.
    """
    try:
        runtime = _runtime_state(state, require_enabled=False)
        runtime_directory = _runtime_directory(directory)
        exited, reason = _owner_exited(runtime["owned_pid"])
        if not exited:
            return _summary("error", reason or "owned_process_status_unknown")

        receipt_paths = _receipt_files(runtime_directory)
        if not receipt_paths:
            return _summary("not_needed", "no_receipts")
        receipts = [_load_receipt(path) for path in receipt_paths]
        for receipt in receipts:
            if (receipt["bound_session_id"] != runtime["scheduler_session_id"]
                    or receipt["pid"] != runtime["owned_pid"]):
                return _summary("skipped", "foreign_runtime_receipt", receipt["task_id"])

        tasks_path, lock_path = _safe_native_paths(runtime["cwd"])
        lock_snapshot = _offline_lock_snapshot(lock_path)
        if not tasks_path.exists():
            return _summary("skipped", "task_file_missing")
        task_snapshot, task_mode = _raw_snapshot(tasks_path)
        document = _tasks_document(task_snapshot)
        by_id = {task["id"]: task for task in document["tasks"]}
        applied: list[str] = []
        skipped: list[tuple[str, str]] = []
        unchanged: list[str] = []
        for receipt in receipts:
            task = by_id.get(receipt["task_id"])
            if task is None:
                skipped.append((receipt["task_id"], "cancelled"))
                continue
            try:
                creator_session_id, creator_pid, creator_proc_start, created_at = _task_identity(task)
                matches = (creator_pid == receipt["pid"] and creator_proc_start == receipt["proc_start"]
                           and _same_value(created_at, receipt["created_at"])
                           and _task_fingerprint(task) == receipt["fingerprint"])
            except _Fault:
                matches = False
            if not matches:
                skipped.append((receipt["task_id"], "external_change"))
                continue
            if creator_session_id == receipt["original_session_id"]:
                unchanged.append(receipt["task_id"])
                continue
            if creator_session_id != receipt["bound_session_id"]:
                skipped.append((receipt["task_id"], "external_change"))
                continue
            task["createdBySessionId"] = receipt["original_session_id"]
            applied.append(receipt["task_id"])

        if applied:
            if _offline_lock_snapshot(lock_path) != lock_snapshot:
                raise _SnapshotChanged()
            _replace_if_unchanged(tasks_path, task_snapshot, document, task_mode)
            return _summary("applied", "restored" if not skipped else "restored_with_skips",
                            applied[0] if len(applied) == 1 else None)
        if skipped:
            return _summary("skipped", skipped[0][1], skipped[0][0] if len(skipped) == 1 else None)
        return _summary("not_needed", "already_restored",
                        unchanged[0] if len(unchanged) == 1 else None)
    except _Fault as exc:
        return _summary("error", exc.reason)
    except (OSError, TypeError, ValueError):
        return _summary("error", "io_error")
