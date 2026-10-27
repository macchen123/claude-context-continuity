"""Independent child budgets using native hooks; no child reset or task scheduler."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from . import core
from .context_failure import is_context_rejection
from .context_runtime import _event_context_window, observe_budget
from .native_usage import message_usage

_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MAX_LINE = 8 * 1024 * 1024
_LIFECYCLE = {"SubagentStart", "SubagentStop"}


def merge_output(result, extra):
    merged = {**result, **extra}
    specific = {**result.get("hookSpecificOutput", {}), **extra.get("hookSpecificOutput", {})}
    context = "\n".join(filter(None, (result.get("hookSpecificOutput", {}).get("additionalContext"),
                                     extra.get("hookSpecificOutput", {}).get("additionalContext"))))
    if context:
        specific["additionalContext"] = context
    if specific:
        merged["hookSpecificOutput"] = specific
    return merged


def _path(directory, agent):
    if not isinstance(agent, str) or not _AGENT_ID.fullmatch(agent):
        raise ValueError("invalid_agent_id")
    return core.safe_path(directory, Path(directory) / "agent-budgets" / f"{agent}.json", exists=False)


def _load(directory, agent):
    path = _path(directory, agent)
    if not path.exists():
        return {"agent_id": agent, "native_context_window": None, "usage": None, "budget": {},
                "replacement_required": False, "terminal_observed": False}
    value = core.read_json(path)
    if not isinstance(value, dict) or value.get("agent_id") != agent:
        raise ValueError("agent_budget_identity_mismatch")
    return {"native_context_window": None, "usage": None, "budget": {},
            "replacement_required": False, "terminal_observed": False, **value}


def _save(directory, child):
    core.no_secrets(child)
    core.atomic(_path(directory, child["agent_id"]), child, skip_unchanged=True)


def transcript_usage(path, session_id, agent, *, stop_marker=None, session_ids=None):
    """Read only child accounting/lifecycle metadata, never classify it as user authority."""
    path = Path(path)
    if path.name != f"agent-{agent}.jsonl" or path.resolve(strict=False) != path:
        raise ValueError("invalid_agent_transcript_path")
    sessions = set(session_ids or ()) | {session_id}
    latest, rejected, pending, backgrounds = None, False, set(), set()
    stopped, incomplete = False, False
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("invalid_agent_transcript_file")
        while True:
            start = stream.tell()
            raw = stream.readline(_MAX_LINE + 1)
            if not raw or not raw.endswith(b"\n") and len(raw) <= _MAX_LINE:
                break
            if len(raw) > _MAX_LINE:
                while raw and not raw.endswith(b"\n"):
                    raw = stream.readline(_MAX_LINE)
                latest, stopped, incomplete = None, False, True
                continue
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError("invalid_agent_history_record")
            if row.get("type") == "attachment" and row.get("sessionId") in sessions and row.get("agentId") == agent:
                attachment = row.get("attachment", {})
                if (stop_marker and attachment.get("type") == "hook_stopped_continuation"
                        and attachment.get("hookEvent") == "PostToolBatch"
                        and str(attachment.get("message", "")).startswith(stop_marker)):
                    stopped = True
            if row.get("type") not in {"assistant", "user"}:
                continue
            if row.get("sessionId") not in sessions or row.get("agentId") != agent or row.get("isSidechain") is not True:
                raise ValueError("agent_history_identity_mismatch")
            message = row.get("message", {})
            if not isinstance(message, dict):
                raise ValueError("invalid_agent_message")
            content = message.get("content", [])
            blocks = content if isinstance(content, list) else []
            if row["type"] == "assistant":
                text = content if isinstance(content, str) else "\n".join(
                    block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
                if row.get("isApiErrorMessage") and is_context_rejection(row.get("error"), text):
                    rejected = True
                sample = message_usage(message)
                if sample is not None:
                    rejected, stopped = False, False
                    for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                        if field not in message["usage"]:
                            sample["usage"][field] = None
                    if not sample["cache_fields_complete"]:
                        sample["total_input_tokens"] = None
                    sample["output_tokens"] = message["usage"].get("output_tokens")
                    latest = {**sample, "message_id": row.get("uuid"), "request_id": message.get("id"),
                              "source_path": str(path), "session_id": session_id,
                              "start_byte": start, "end_byte": stream.tell()}
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                        pending.add(block["id"])
            else:
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        pending.discard(block.get("tool_use_id"))
                response = row.get("toolUseResult")
                if isinstance(response, dict):
                    handle = response.get("backgroundTaskId")
                    if isinstance(handle, str):
                        backgrounds.add(handle)
    return {"usage": latest, "context_rejected": rejected, "pending_tool_ids": sorted(pending),
            "background_handles": sorted(backgrounds), "stop_marker_observed": stopped,
            "history_incomplete": incomplete}


def _observe(child, path, sid, configured_window, event=None):
    snapshot = transcript_usage(path, sid, child["agent_id"], stop_marker=child.get("stop_marker"),
                                session_ids=child.get("history_session_ids"))
    previous_request = (child.get("usage") or {}).get("request_id")
    sample = snapshot["usage"]
    if sample and sample.get("request_id") != previous_request:
        child["terminal_observed"] = False
    child.update(snapshot)
    child["source_path"], child["session_id"] = str(path), sid
    observed = _event_context_window(event or {})
    # A parent's modelUsage aggregate is not evidence for the child's model window.
    if event and not any(key in event for key in ("contextWindow", "context_window")):
        observed = None
    if observed is not None:
        child["native_context_window"] = observed
        child["window_model"] = sample["actual_model"] if sample else None
    elif sample and child.get("window_model") not in {None, sample["actual_model"]}:
        child["native_context_window"] = None
    native = child.get("native_context_window")
    windows = [value for value in (native, configured_window) if type(value) is int and value > 0]
    child["window_tokens"] = min(windows) if windows else None
    child["window_source"] = ("min_configured_and_child_native" if native is not None and configured_window
                              else "child_native" if native is not None
                              else "configured_estimate" if windows else "unknown")
    child["remaining_tokens"] = None
    child["decision"] = "usage_unknown"
    if sample and sample["cache_fields_complete"] and sample["output_tokens"] is not None and windows:
        actual = native is not None and (not configured_window or native <= configured_window)
        decision = observe_budget(child.get("budget"), sample,
                                  {"tokens": min(windows), "actual": actual}, None)
        child["budget"] = decision["observations"]
        child["remaining_tokens"] = decision["remaining_tokens"]
        child["decision"] = "handoff_required" if decision["near_limit"] else "continue"
        if decision["near_limit"]:
            child["replacement_required"] = True
    elif sample and not windows:
        child["decision"] = "window_unknown"
    if snapshot["context_rejected"]:
        child["replacement_required"] = True
        child["decision"] = "context_rejected"
    return child


def _diagnose(state, directory, event, child, decision=None):
    from .diagnostics import record_event
    sample = child.get("usage") or {}
    values = sample.get("usage") or {}
    record_event(directory, "agent_budget", context_id=state["conversation_id"],
                 session_id=child.get("session_id"), agent_id=child["agent_id"],
                 prompt_id=event.get("prompt_id"), message_id=sample.get("message_id"),
                 request_id=None if child.get("context_rejected") else sample.get("request_id"),
                 usage_request_id=sample.get("request_id"), model=sample.get("actual_model"),
                 hook_event=event.get("hook_event_name"), decision=decision or child.get("decision"),
                 input_tokens=values.get("input_tokens"),
                 cache_creation_input_tokens=(values.get("cache_creation_input_tokens") if sample.get("cache_fields_complete") else None),
                 cache_read_input_tokens=(values.get("cache_read_input_tokens") if sample.get("cache_fields_complete") else None),
                 output_tokens=sample.get("output_tokens"), window_tokens=child.get("window_tokens"),
                 window_source=child.get("window_source"), usage_source="child_native_transcript",
                 guard_tokens=child.get("budget", {}).get("guard_tokens"), remaining_tokens=child.get("remaining_tokens"),
                 request_sent=None, error_origin="native_unspecified" if child.get("context_rejected") else None)


def _handoff(state, directory, child):
    child["handoff"] = {
        "kind": "child_context_handoff", "authority": "runtime_metadata_not_user_authorization",
        "agent_id": child["agent_id"], "agent_type": child.get("agent_type"),
        "actual_model": (child.get("usage") or {}).get("actual_model"),
        "metadata_path": str(Path(child["source_path"]).with_suffix(".meta.json")) if child.get("source_path") else None,
        "background_handle_status": "observed_not_confirmed_active",
        "source_path": child.get("source_path"), "original_tool_use_id": child.get("original_tool_use_id"),
        "pending_tool_ids": child.get("pending_tool_ids", []), "background_handles": child.get("background_handles", []),
        "instruction": "Check original delegation, latest user changes, artifacts and receipts. Continue only unfinished authorized work "
                       "after verifying the old execution is terminal. Preserve its role, model slot, permissions and leaf boundary. "
                       "Do not resume this oversized history, replay tools, restart background work or revive cancelled tasks. "
                       "If complete, deliver existing results without starting another agent.",
    }
    state.setdefault("agent_handoffs", {})[child["agent_id"]] = str(_path(directory, child["agent_id"]))
    _save(directory, child)


def _source_binding(state, event, child):
    sid, agent = event.get("session_id"), child["agent_id"]
    binding = state.get("child_handle_sources", {}).get(agent)
    candidates = [(state["session_id"], state.get("source_path")),
                  (child.get("session_id"), child.get("parent_source_path"))]
    if isinstance(binding, list) and len(binding) == 2:
        candidates.append(tuple(binding))
    if sid not in {item[0] for item in candidates}:
        return None
    declared = event.get("agent_transcript_path")
    transcript = event.get("transcript_path")
    if not declared and isinstance(transcript, str) and Path(transcript).name == f"agent-{agent}.jsonl":
        declared = transcript
    for source_sid, parent in candidates:
        if not source_sid or not parent:
            continue
        parent = Path(parent)
        base = core.safe_path(parent.parent, f"{source_sid}/subagents", exists=False)
        candidate = declared or (child.get("source_path") if source_sid == child.get("session_id") else None)
        try:
            path = core.safe_path(base, candidate or base / f"agent-{agent}.jsonl", exists=False)
        except (OSError, ValueError, core.ContinuityError):
            continue
        if declared or path.is_file():
            return path, source_sid, parent
    return None


def on_hook(state, event, directory):
    agent, name = event.get("agent_id"), event.get("hook_event_name")
    child = _load(directory, agent)
    try:
        binding = _source_binding(state, event, child)
    except (OSError, ValueError, TypeError, core.ContinuityError):
        binding = None
    if binding is None and event.get("session_id") not in {
            state["session_id"], child.get("session_id"),
            (state.get("child_handle_sources", {}).get(agent) or [None])[0]}:
        return {}
    if name == "SubagentStart":
        child["terminal_observed"] = False
        child.pop("stop_marker_observed", None)
        child.pop("stop_marker", None)
        role = event.get("agent_type")
        if isinstance(role, str):
            child["agent_type"] = role
    if name == "SubagentStop":
        child["terminal_observed"] = True
    try:
        if binding is None:
            raise ValueError("agent_transcript_unavailable")
        path, sid, parent = binding
        child["parent_source_path"] = str(parent)
        _observe(child, path, sid, state["configuration"].get("configured_window"), event)
    except (OSError, ValueError, TypeError):
        child["decision"] = "usage_unavailable"
    if name == "SubagentStop":
        child["terminal_observed"] = True
    if name == "StopFailure" and is_context_rejection(event.get("error"), event.get("last_assistant_message")):
        child["replacement_required"] = True
        child["context_rejected"] = True
    result = {}
    if child["replacement_required"]:
        if name == "PostToolBatch":
            # This marker binds native completion to this stopped execution, not an old finished sidecar.
            marker = child.setdefault("stop_marker", f"cclaude-child-budget:{uuid4()}")
            result = {"continue": False, "stopReason":
                      f"{marker} Child context budget reached; old execution must settle before continuation. "
                      f"Parent handoff: {_path(directory, agent)}. Do not SendMessage this oversized history."}
        elif name == "PreToolUse":
            result = {"hookSpecificOutput": {"hookEventName": name, "additionalContext":
                      "Child context budget reached. Finish a concise artifact-based handoff and return; "
                      "do not call parent context-request or compact. The host will stop at the settled tool boundary."}}
        _handoff(state, directory, child)
    elif name not in _LIFECYCLE and name not in {"Stop", "StopFailure"}:
        sample = child.get("usage")
        if sample and sample["cache_fields_complete"] and sample["output_tokens"] is not None:
            used = sample["total_input_tokens"] + sample["output_tokens"]
            context = (f"Child-only runtime budget, not authorization: input+cache={sample['total_input_tokens']}; "
                       f"output={sample['output_tokens']}; window={child['window_tokens']} ({child['window_source']}); "
                       f"remaining={child['remaining_tokens']}. Never use the parent's context-request to reset this child.")
            if child["window_tokens"] and used >= child["window_tokens"] * .65:
                context += " Reserve remaining space for a concise handoff with artifact paths and unfinished work, then return."
            result = {"hookSpecificOutput": {"hookEventName": name, "additionalContext": context}}
        else:
            result = {"hookSpecificOutput": {"hookEventName": name, "additionalContext":
                      "Child runtime budget is unverified (usage or window unavailable); this is not zero usage or spare capacity. "
                      "Keep a concise artifact handoff and return before taking on more work. Do not renew the parent or compact."}}
    _save(directory, child)
    _diagnose(state, directory, event, child, "stop_at_batch" if result.get("continue") is False else None)
    return result


def guard_resume(state, event, directory, outcome):
    for identity in outcome.get("agents", []):
        if identity.get("status") == "blocked":
            continue
        try:
            child = _load(directory, identity["agent_id"])
        except (OSError, ValueError, TypeError, core.ContinuityError):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": "cclaude 子代理预算状态不可读，无法确认旧历史能否安全恢复；请先核对原始记录，不要绕过。"}}
        child["agent_type"] = identity.get("agent_type")
        child["original_tool_use_id"] = identity.get("tool_use_id")
        child["history_session_ids"] = identity.get("history_session_ids", [])
        unavailable = False
        for source in identity.get("transcript_sources", []):
            try:
                _observe(child, Path(source["source_path"]), source["session_id"],
                         state["configuration"].get("configured_window"))
            except (OSError, ValueError, TypeError) as error:
                if isinstance(error, ValueError) and str(error) == "agent_history_identity_mismatch":
                    from .agent_metadata import failure_hook
                    return failure_hook(event, "agent_history_identity_mismatch")
                unavailable = True
        if child["replacement_required"]:
            _handoff(state, directory, child)
            _diagnose(state, directory, event, child, "deny_oversized_resume")
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"cclaude 已阻止恢复超限子代理 {child['agent_id']}。交接：{_path(directory, child['agent_id'])}。"
                        "核对原任务、最新取消/限制、产物及旧执行终态；仅对未完成且仍获授权的工作，"
                        "以原角色和模型槽创建新上下文，保留权限与叶子边界。不要改用别名、resume 或 compact 绕过。"}}
        if unavailable:
            _diagnose(state, directory, event, child, "resume_usage_unavailable")
        _save(directory, child)
    return {}


def _terminal_metadata(child):
    from .agent_metadata import _snapshot
    path = child.get("source_path")
    if not path:
        return False
    try:
        metadata = _snapshot(Path(path).with_suffix(".meta.json"))["value"]
    except (OSError, ValueError, TypeError):
        return False
    if child.get("agent_type") and metadata["agentType"] != child["agent_type"]:
        return False
    return (metadata.get("stoppedByUser") is True or
            metadata.get("finished") is True and child.get("stop_marker_observed") is True)


def parent_notice(state, event, directory, *, delivery_allowed=True):
    if not delivery_allowed or event.get("hook_event_name") not in {"PreToolUse", "PostToolUse", "PostToolBatch", "UserPromptSubmit"}:
        return {}
    notices = []
    for agent, path in list(state.get("agent_handoffs", {}).items()):
        try:
            child = _load(directory, agent)
        except (OSError, ValueError, TypeError, core.ContinuityError):
            continue
        if child.get("notified_session_id") == state["session_id"]:
            continue
        try:
            _observe(child, Path(child["source_path"]), child["session_id"],
                     state["configuration"].get("configured_window"))
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if _terminal_metadata(child):
            child["terminal_observed"] = True
            _handoff(state, directory, child)
        if not child.get("terminal_observed") and agent in state.get("active_child_handles", []):
            continue
        notices.append(f"Child {agent} needs context handoff: {path}.")
        child["notified_session_id"] = state["session_id"]
        _save(directory, child)
        if len(notices) == 4:
            break
    if not notices:
        return {}
    return {"hookSpecificOutput": {"hookEventName": event["hook_event_name"], "additionalContext":
            "Runtime state, not new authorization. " + " ".join(notices) +
            " Verify terminal state, original delegation, latest user instructions and artifact receipts. "
            "Continue only unfinished authorized work in a fresh agent of the same role/model/permissions. "
            "Do not SendMessage the oversized history, replay completed tools or revive paused/cancelled work."}}
