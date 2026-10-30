"""Publish native child history, retaining original evidence for continuation fragments."""
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


def _snapshot(path, agent, sessions, *, fragments=False):
    """Validate a stable native stream and bind its bytes, not a rewritten transcript."""
    digest, messages = hashlib.sha256(), 0
    rows, chunks = [], []
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
            if fragments:
                rows.append(row)
                chunks.append(raw)
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
            "identity": _identity(after), "stamp": _stamp(after),
            **({"rows": rows, "raw": b"".join(chunks)} if fragments else {})}


def _continuation(snapshots):
    # A previously reconstructed full stream may coexist with its retained inputs.
    maximal = [item for item in snapshots if not any(
        item["raw"] != other["raw"] and item["raw"] in other["raw"] for other in snapshots)]
    owners, identifiers = {}, []
    for index, item in enumerate(maximal):
        ids = set()
        for row in item["rows"]:
            uid = row.get("uuid")
            if not isinstance(uid, str):
                continue
            if uid in owners:
                raise AgentHistoryError("divergent_agent_histories")
            owners[uid] = index
            ids.add(uid)
        if not ids:
            raise AgentHistoryError("missing_agent_history_predecessor")
        identifiers.append(ids)
    predecessors, successors = {}, {}
    for index, item in enumerate(maximal):
        external = {row["parentUuid"] for row in item["rows"]
                    if row.get("parentUuid") and row["parentUuid"] not in identifiers[index]}
        if not external:
            continue
        if len(external) != 1:
            raise AgentHistoryError("divergent_agent_histories")
        bridge = next(iter(external))
        if bridge not in owners:
            raise AgentHistoryError("missing_agent_history_predecessor")
        previous = owners[bridge]
        ancestry = [row for row in maximal[previous]["rows"] if isinstance(row.get("uuid"), str)]
        if (not ancestry or ancestry[-1]["uuid"] != bridge or previous in successors
                or any(row.get("parentUuid") == bridge for row in maximal[previous]["rows"])):
            raise AgentHistoryError("divergent_agent_histories")
        predecessors[index], successors[previous] = previous, index
    roots = set(range(len(maximal))) - predecessors.keys()
    if len(roots) != 1:
        raise AgentHistoryError("divergent_agent_histories")
    order, current = [], roots.pop()
    while current not in order:
        order.append(current)
        if current not in successors:
            break
        current = successors[current]
    if len(order) != len(maximal):
        raise AgentHistoryError("divergent_agent_histories")
    return [maximal[index] for index in order]


def _fragment_receipt(directory, agent, destination):
    return Path(directory) / "agent_history" / core.digest({
        "agent_id": agent, "destination": str(destination), "method": "native_history_continuation"})


def _established_fragments(receipt_directory, paths, destination):
    applied = receipt_directory / "applied.json"
    if not applied.exists() or not destination.exists():
        return None
    receipt = core.read_json(applied)
    identity = tuple(receipt["destination_identity"])
    if _identity(destination.lstat()) != identity:
        return None
    sources = {tuple(row["stamp"][:2]): row for row in receipt["sources"]}
    for path in paths:
        current = path.lstat()
        if _identity(current) == identity:
            continue  # Native appends, including incomplete tails, remain writer-owned.
        source = sources.get(_identity(current))
        if source is None or _stamp(current) != tuple(source["stamp"]):
            return None
    return {"status": "available", "path": str(destination), "source_path": str(destination), "receipt": str(applied)}


def _publish_fragments(agent, paths, destination, sessions, project, directory):
    receipt_directory = _fragment_receipt(directory, agent, destination)
    established = _established_fragments(receipt_directory, paths, destination)
    if established:
        return established
    unique = {_identity(path.lstat()): path for path in paths}
    snapshots = [_snapshot(path, agent, sessions, fragments=True) for path in unique.values()]
    if {item["identity"] for item in snapshots} != set(unique):
        raise AgentHistoryError("agent_history_changed_before_publish")
    ordered = _continuation(snapshots)
    destination_stamp = _stamp(destination.lstat()) if destination.exists() else None
    if destination_stamp is not None and destination_stamp[:2] not in unique:
        raise AgentHistoryError("destination_agent_history_conflict")
    receipt_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    sources = []
    for snapshot in snapshots:
        if _stamp(Path(snapshot["path"]).lstat()) != snapshot["stamp"]:
            raise AgentHistoryError("agent_history_changed_before_publish")
        preserved = receipt_directory / f"source-{snapshot['identity'][0]}-{snapshot['identity'][1]}.jsonl"
        if not preserved.exists():
            os.link(snapshot["path"], preserved, follow_symlinks=False)
        if _identity(preserved.lstat()) != snapshot["identity"]:
            raise AgentHistoryError("agent_history_receipt_conflict")
        # Creating the evidence link changes ctime, but never the original bytes.
        linked_stamp = _stamp(Path(snapshot["path"]).lstat())
        if linked_stamp[:4] != snapshot["stamp"][:4]:
            raise AgentHistoryError("agent_history_changed_before_publish")
        snapshot["stamp"] = linked_stamp
        sources.append({key: snapshot[key] for key in ("path", "sha256", "size", "stamp")} | {"preserved_path": str(preserved)})
    receipt = {"agent_id": agent, "destination": str(destination), "method": "native_history_continuation", "sources": sources}
    core.atomic(receipt_directory / "intent.json", receipt)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    core.safe_path(project, destination, exists=False)
    temporary = receipt_directory / "publication.jsonl"
    try:
        if len(ordered) == 1:
            os.link(ordered[0]["path"], temporary, follow_symlinks=False)
            # This link also changes ctime of the selected source.
            linked_stamp = _stamp(Path(ordered[0]["path"]).lstat())
            if linked_stamp[:4] != ordered[0]["stamp"][:4]:
                raise AgentHistoryError("agent_history_changed_before_publish")
            ordered[0]["stamp"] = linked_stamp
        else:
            with temporary.open("xb") as stream:
                for snapshot in ordered:
                    stream.write(snapshot["raw"])
                stream.flush()
                os.fsync(stream.fileno())
        for snapshot in snapshots:
            if _stamp(Path(snapshot["path"]).lstat()) != snapshot["stamp"]:
                raise AgentHistoryError("agent_history_changed_before_publish")
        if destination_stamp is None:
            os.link(temporary, destination, follow_symlinks=False)
        else:
            # The destination is a registered, verified fragment, not an unrelated file.
            expected = next(item["stamp"] for item in snapshots if item["identity"] == destination_stamp[:2])
            if _stamp(destination.lstat()) != expected:
                raise AgentHistoryError("agent_history_changed_before_publish")
            os.replace(temporary, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    for source in sources:
        current = _stamp(Path(source["preserved_path"]).lstat())
        if current[:4] != source["stamp"][:4]:
            raise AgentHistoryError("agent_history_changed_before_publish")
        source["stamp"] = current
    receipt["destination_identity"] = _identity(destination.lstat())
    applied = receipt_directory / "applied.json"
    core.atomic(applied, receipt)
    return {"status": "reconstructed" if len(ordered) > 1 else "linked", "path": str(destination),
            "source_path": str(destination), "receipt": str(applied)}


def establish(agent, records, target, sessions, directory):
    """Expose one complete native stream through the existing loader location.

    Single-inode streams stay live via hard links. Registered continuation
    fragments are ordered by native ancestry, retaining their exact input bytes.
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
        return _publish_fragments(agent, paths, destination, sessions, project, directory)
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
