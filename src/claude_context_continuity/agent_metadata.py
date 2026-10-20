"""保留原生子代理恢复 sidecar；只恢复已登记会话中的原始身份，不另造代理。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import core, native_addressing
from .history import HistorySource

_METADATA_NAME = re.compile(r"agent-([A-Za-z0-9_-]{1,128})\.meta\.json\Z")
_NATIVE_ID = re.compile(r"a[0-9a-f]{7,64}\Z")
_MUTABLE = frozenset({"stoppedByUser", "worktreeCleanlyRemoved", "isObserver", "observerStopped",
                      "observerTaskId", "armingPermissionMode"})
_FALLBACK_FIELDS = frozenset({"agentType", "spawnDepth", "requestShape", "requestNonInteractive", "stoppedByUser"})
_UNSPECIFIED_SESSION = object()


class MetadataError(ValueError):
    """可公开的稳定原因，不把 metadata 内容或凭据带入 hook 输出。"""


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _snapshot(path):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > core.MAX_PACKET:
            raise MetadataError("unsafe_or_oversized_metadata")
        raw = stream.read(core.MAX_PACKET + 1)
        after = os.fstat(stream.fileno())
    stamp = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if len(raw) > core.MAX_PACKET or stamp(before) != stamp(after) or stamp(after) != stamp(path.lstat()):
        raise MetadataError("metadata_changed_during_read")
    value = json.loads(raw)
    if (not isinstance(value, dict) or not isinstance(value.get("agentType"), str)
            or not value["agentType"] or len(value["agentType"]) > 256):
        raise MetadataError("invalid_agent_identity")
    core.no_secrets(value)
    return {"path": path, "value": value, "sha256": _digest(raw)}


def _source(entry):
    sid = core.uuid(entry["session_id"])
    path = Path(entry["source_path"])
    if not path.is_absolute() or path.name != f"{sid}.jsonl" or path.resolve(strict=False) != path:
        raise MetadataError("invalid_native_source")
    return path.parent, core.safe_path(path.parent, f"{sid}/subagents", exists=False)


def _source_directories(entries):
    sources, seen = [], set()
    for entry in entries:
        key = entry["session_id"], entry["source_path"]
        if key in seen:
            continue
        seen.add(key)
        sources.append(_source(entry))
    return sources


def _sources(state, entries):
    current = state.get("source_path")
    if not current or state.get("native_session_uncertain") or state.get("pending_session_start"):
        raise MetadataError("native_session_identity_pending")
    target = {"session_id": state["session_id"], "source_path": current}
    return _source_directories([*entries, target]), _source(target)


def _recipient_inventory(state, entries):
    """Read candidate local names without authorizing any identity restoration.

    A pending resume's exact source must also be searched for local collisions.
    _sources remains mandatory before publishing or verifying a local sidecar.
    """
    if not state.get("source_path"):
        raise MetadataError("native_session_identity_pending")
    candidates = [*entries, {"session_id": state["session_id"], "source_path": state["source_path"]}]
    pending = state.get("pending_session_start")
    if isinstance(pending, dict) and pending.get("source") == "resume":
        candidates.append({"session_id": pending["session_id"], "source_path": pending["transcript_path"]})
    records, faults = _inventory(_source_directories(candidates))
    return records, faults, candidates


def _name_matches(name, bare):
    if not isinstance(name, str):
        return False
    normalized = native_addressing.normalized_name(name)
    return normalized == bare or (len(bare) >= 3 and normalized.startswith(bare))


def _launch_name_matches(entries, records, bare):
    """Use canonical native launch records, not a second agent-name registry."""
    calls, seen = set(), set()
    try:
        for entry in entries:
            key = entry["session_id"], entry["source_path"]
            if key in seen:
                continue
            seen.add(key)
            source = HistorySource(Path(entry["source_path"]), entry["session_id"])
            for call, names in source.agent_launch_names().items():
                if any(_name_matches(name, bare) for name in names):
                    calls.add(call)
    except (ValueError, OSError, TypeError):
        raise MetadataError("agent_name_registry_unreadable") from None
    bound_calls = {call for rows in records.values() for row in rows
                   if isinstance(call := row["value"].get("toolUseId"), str) and call in calls}
    if calls - bound_calls:
        raise MetadataError("original_agent_metadata_missing")
    return {agent for agent, rows in records.items()
            if any(isinstance(call := row["value"].get("toolUseId"), str) and call in calls for row in rows)}


def _metadata_paths(project, directory):
    # pathlib.rglob silently skips unreadable directories. They are uncertainty,
    # not evidence that a same-named local agent is absent.
    def unreadable(_):
        raise MetadataError("agent_name_registry_unreadable")
    paths = []
    for current, directories, files in os.walk(directory, onerror=unreadable):
        for name in directories:
            core.safe_path(project, Path(current) / name, exists=True)
        paths.extend(Path(current) / name for name in files if _METADATA_NAME.fullmatch(name))
    return sorted(paths)


def _inventory(sources):
    records, faults = {}, {}
    for project, directory in sources:
        if not directory.exists():
            continue
        for path in _metadata_paths(project, directory):
            match = _METADATA_NAME.fullmatch(path.name)
            if match is None:
                continue
            agent = match[1]
            try:
                safe = core.safe_path(project, path, exists=True)
                record = _snapshot(safe)
                record["relative"] = path.relative_to(directory)
                records.setdefault(agent, []).append(record)
            except (ValueError, OSError, TypeError) as error:
                faults[agent] = type(error).__name__
    return records, faults


def _fallback(value):
    return value["agentType"] == "general-purpose" and set(value) <= _FALLBACK_FIELDS


def _profile(value):
    # 未知原生字段也必须保持；只允许原生生命周期字段独立演进。
    return {key: item for key, item in value.items() if key not in _MUTABLE}


def _origin(records):
    trusted = [record for record in records if not _fallback(record["value"])]
    if not trusted:
        raise MetadataError("original_agent_identity_missing")
    profile = _profile(trusted[0]["value"])
    if any(_profile(record["value"]) != profile for record in trusted[1:]):
        raise MetadataError("conflicting_agent_identity")
    if len({str(record["relative"]) for record in records}) != 1:
        raise MetadataError("conflicting_agent_directory")
    origin = trusted[-1]
    value = dict(origin["value"])
    for key in ("stoppedByUser", "worktreeCleanlyRemoved", "observerStopped"):
        if any(record["value"].get(key) is True for record in records):
            value[key] = True
    return origin, value


def _same_snapshot(record):
    if _snapshot(record["path"])["sha256"] != record["sha256"]:
        raise MetadataError("metadata_changed_before_publish")


def _restore(agent, records, target, state, directory):
    origin, value = _origin(records)
    project, target_directory = target
    path = core.safe_path(project, target_directory / origin["relative"], exists=False)
    try:
        previous = _snapshot(path)
    except FileNotFoundError:
        previous = None
    if previous is not None and _profile(previous["value"]) == _profile(value):
        if value.get("stoppedByUser") and not previous["value"].get("stoppedByUser"):
            return {"status": "blocked", "agent_id": agent, "reason": "agent_stopped_by_user"}
        return {"status": "unchanged", "agent_id": agent, "agent_type": value["agentType"],
                "stopped_by_user": bool(value.get("stoppedByUser")), "path": str(path)}
    if previous is not None:
        # 只修复已证实的原生缺 sidecar 兜底形状；其它冲突绝不覆盖。
        old = previous["value"]
        if (not _fallback(old) or not value.get("toolUseId")
                or any(old[key] != value.get(key) for key in old if key not in {"agentType", "stoppedByUser"})):
            raise MetadataError("destination_identity_conflict")
        if agent in state.get("active_child_handles", []):
            raise MetadataError("degraded_agent_still_running")
    _same_snapshot(origin)
    identity = {"agent_id": agent, "source": {"path": str(origin["path"]), "sha256": origin["sha256"]},
                "destination": str(path), "previous_sha256": previous["sha256"] if previous else None,
                "value_hash": core.digest(value)}
    receipt_directory = Path(directory) / "agent_metadata" / core.digest(identity)
    intent = {**identity, "previous_value": previous["value"] if previous else None}
    intent_path = receipt_directory / "intent.json"
    if intent_path.exists():
        if core.read_json(intent_path) != intent:
            raise MetadataError("repair_receipt_conflict")
    else:
        core.atomic(intent_path, intent, exclusive=True)
    if previous is not None:
        _same_snapshot(previous)
    core.safe_path(project, path, exists=False)
    try:
        core.atomic(path, value, exclusive=previous is None)
    except FileExistsError:
        # 原生进程先发布时仅验证其身份，不覆盖并发 writer。
        actual = _snapshot(path)
        if actual["value"] != value:
            raise MetadataError("concurrent_metadata_publication") from None
    actual = _snapshot(path)
    if actual["value"] != value:
        raise MetadataError("published_metadata_changed")
    receipt = {**identity, "destination_sha256": actual["sha256"],
               "status": "repaired_fallback" if previous else "restored"}
    applied = receipt_directory / "applied.json"
    if applied.exists():
        if core.read_json(applied) != receipt:
            raise MetadataError("repair_receipt_conflict")
    else:
        core.atomic(applied, receipt, exclusive=True)
    return {"status": receipt["status"], "agent_id": agent, "agent_type": value["agentType"],
            "stopped_by_user": bool(value.get("stoppedByUser")), "receipt": str(applied), "path": str(path)}


def recipient(event):
    if event.get("hook_event_name") != "PreToolUse":
        return None
    name, value = event.get("tool_name"), event.get("tool_input")
    if not isinstance(value, dict):
        return None
    if name == "SendMessage":
        to = value.get("to")
        if (not isinstance(to, str) or not to.strip() or native_addressing.explicit_peer(to)
                or native_addressing.normalized_name(native_addressing.bare_recipient(to)) == "main"):
            return None  # 原生 peer 地址不可能恢复本地子代理，仍由原生校验和投递。
        return to.strip()
    return None


def failure_hook(event, reason="agent_metadata_unavailable"):
    if recipient(event) is None:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "cclaude 无法保持原子代理身份，已阻止本次恢复，避免退回 general-purpose。"
                                    f"原因：{reason}。不要重发或改角色绕过；先核对原生 metadata。"}}


def stopped_handles(state, entries):
    """Read explicit native stop markers without resuming agents or writing sidecars."""
    sources, _ = _sources(state, entries)
    records, faults = _inventory(sources)
    stopped = {}
    for agent in state.get("active_child_handles", []):
        if agent in faults or agent not in records:
            continue
        try:
            _, value = _origin(records[agent])
            if value.get("stoppedByUser") is not True:
                continue
            proof = next(row for row in reversed(records[agent]) if row["value"].get("stoppedByUser") is True)
            _same_snapshot(proof)
        except (ValueError, OSError, TypeError):
            continue
        stopped[agent] = {"path": str(proof["path"]), "sha256": proof["sha256"], "reason": "stopped_by_user"}
    return stopped


def synchronize(state, entries, directory, *, to=None, session_id=_UNSPECIFIED_SESSION):
    """SessionStart主动继承；只对可能恢复本地代理的工具执行身份门禁。"""
    if to is None:
        sources, target = _sources(state, entries)
        records, faults = _inventory(sources)
        candidates = set(records) | set(faults)
    else:
        if (native_addressing.explicit_peer(to)
                or native_addressing.normalized_name(native_addressing.bare_recipient(to)) == "main"):
            return {"status": "not_local_agent", "restored": 0, "blocked": []}
        records, faults, source_entries = _recipient_inventory(state, entries)
        bare = native_addressing.normalized_name(native_addressing.bare_recipient(to))
        candidates = set(records) | set(faults)
        matched = {agent for agent in candidates if agent == bare or
                   (_NATIVE_ID.fullmatch(bare) and agent.startswith(bare)) or
                   any(_name_matches(row["value"].get("name"), bare) for row in records.get(agent, []))}
        if not _NATIVE_ID.fullmatch(bare):
            if faults:
                raise MetadataError("agent_name_registry_unreadable")
            matched |= _launch_name_matches(source_entries, records, bare)
        mismatch = session_id is not _UNSPECIFIED_SESSION and session_id != state["session_id"]
        if not matched:
            if faults:
                raise MetadataError("agent_name_registry_unreadable")
            if _NATIVE_ID.fullmatch(bare):
                raise MetadataError("original_agent_metadata_missing")
            # Settled unknown names retain native name resolution. An unsettled
            # sender needs positive native peer discovery, not a guessed bypass.
            if (not mismatch and not state.get("pending_session_start")
                    and not state.get("native_session_uncertain")) or native_addressing.named_peer(to):
                return {"status": "not_local_agent", "restored": 0, "blocked": []}
        if mismatch:
            raise MetadataError("native_session_identity_mismatch")
        _, target = _sources(state, entries)  # 必须在本地恢复前通过原有完整身份门禁。
        candidates = matched
    outcomes = []
    for agent in sorted(candidates):
        try:
            if agent in faults:
                raise MetadataError("unreadable_agent_metadata")
            outcome = _restore(agent, records[agent], target, state, directory)
            if to is not None and outcome.get("stopped_by_user"):
                outcome = {"status": "blocked", "agent_id": agent, "reason": "agent_stopped_by_user"}
        except (ValueError, OSError, TypeError) as error:
            reason = str(error) if isinstance(error, MetadataError) else type(error).__name__
            outcome = {"status": "blocked", "agent_id": agent, "reason": reason}
        outcomes.append(outcome)
    blocked = [row for row in outcomes if row["status"] == "blocked"]
    return {"status": "blocked" if blocked else "verified",
            "restored": sum(row["status"] in {"restored", "repaired_fallback"} for row in outcomes),
            "blocked": blocked, "agents": outcomes}
