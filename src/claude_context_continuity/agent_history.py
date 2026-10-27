"""Publish one native child-history stream, without copying or merging live histories."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from . import core

_MAX_LINE = 8 * 1024 * 1024


class AgentHistoryError(ValueError):
    """Public reason only; never include native message content."""


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _identity(value):
    return value.st_dev, value.st_ino


def _snapshot(path, agent, sessions):
    """Validate a stable native stream and bind its bytes, not a rewritten transcript."""
    digest, messages = hashlib.sha256(), 0
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise AgentHistoryError("unsafe_agent_history")
        while raw := stream.readline(_MAX_LINE + 1):
            if len(raw) > _MAX_LINE or not raw.endswith(b"\n"):
                raise AgentHistoryError("incomplete_or_oversized_agent_history_record")
            digest.update(raw)
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise AgentHistoryError("invalid_agent_history_record")
            if row.get("type") in {"assistant", "user"}:
                if (row.get("agentId") != agent or row.get("isSidechain") is not True
                        or row.get("sessionId") not in sessions):
                    raise AgentHistoryError("agent_history_identity_mismatch")
                messages += 1
        after = os.fstat(stream.fileno())
    if _stamp(before) != _stamp(after) or _stamp(after) != _stamp(Path(path).lstat()):
        raise AgentHistoryError("agent_history_changed_during_read")
    if not messages:
        raise AgentHistoryError("empty_agent_history")
    return {"path": str(path), "sha256": digest.hexdigest(), "size": after.st_size,
            "identity": _identity(after), "stamp": _stamp(after)}


def establish(agent, records, target, sessions, directory):
    """Link the original inode exclusively into the current native loader location.

    A native append remains visible to old and new parent sessions. Different
    inodes are never reconciled, even when their bytes currently happen to match.
    The native writer alone owns message content and lifecycle.
    """
    project, target_directory = target
    destination = core.safe_path(project, target_directory / records[0]["relative"].with_name(f"agent-{agent}.jsonl"),
                                 exists=False)
    paths = []
    for record in records:
        path = core.safe_path(record["project"], record["path"].with_name(f"agent-{agent}.jsonl"), exists=False)
        if path.exists():
            paths.append(path)
    if not paths:
        raise AgentHistoryError("original_agent_history_missing")
    identities = set()
    for path in paths:
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode):
            raise AgentHistoryError("unsafe_agent_history")
        identities.add(_identity(current))
    if len(identities) != 1:
        raise AgentHistoryError("divergent_agent_histories")
    if destination.exists():
        actual = destination.lstat()
        if not stat.S_ISREG(actual.st_mode) or _identity(actual) not in identities:
            raise AgentHistoryError("destination_agent_history_conflict")
        # Native/current and already-linked streams are live writer-owned files,
        # not new publication candidates. Their incomplete or large appends must
        # retain the native consumer's semantics; budget/identity checks still run.
        return {"status": "available", "path": str(destination), "source_path": str(paths[0])}
    snapshots = [_snapshot(path, agent, sessions) for path in paths]
    if {snapshot["identity"] for snapshot in snapshots} != identities:
        raise AgentHistoryError("agent_history_changed_before_publish")
    source = snapshots[0]
    receipt = {"agent_id": agent, "source_path": source["path"], "source_sha256": source["sha256"],
               "source_size": source["size"], "destination": str(destination), "method": "native_history_hardlink"}
    receipt_directory = Path(directory) / "agent_history" / core.digest(receipt)
    intent = receipt_directory / "intent.json"
    if intent.exists():
        if core.read_json(intent) != receipt:
            raise AgentHistoryError("agent_history_receipt_conflict")
    else:
        core.atomic(intent, receipt, exclusive=True)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    core.safe_path(project, destination, exists=False)
    current = Path(source["path"]).lstat()
    if _stamp(current) != source["stamp"]:
        raise AgentHistoryError("agent_history_changed_before_publish")
    try:
        os.link(source["path"], destination, follow_symlinks=False)
    except FileExistsError:
        pass  # Never replace a native or concurrent writer's destination.
    except OSError:
        raise AgentHistoryError("native_history_link_unavailable") from None
    actual = destination.lstat()
    if not stat.S_ISREG(actual.st_mode) or _identity(actual) != source["identity"]:
        raise AgentHistoryError("concurrent_agent_history_publication")
    descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    applied = receipt_directory / "applied.json"
    if not applied.exists():
        core.atomic(applied, receipt, exclusive=True)
    return {"status": "linked", "path": str(destination), "source_path": source["path"], "receipt": str(applied)}
