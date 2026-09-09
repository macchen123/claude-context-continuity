"""原生会话的预算、History/Notes 与接续状态；不代理任务执行或权限。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
from typing import Any, Callable
from uuid import uuid4

from . import core
from .history import HistoryError, HistorySource, redact, terminal_task_result
from .notes import MAX_NOTE_BYTES, NotesStore

_SCHEMA = "claude-context-runtime.v1"
_MAX_HANDOFF_BYTES = MAX_NOTE_BYTES
_BUDGET_FIELDS = ("bootstrap_input_tokens", "last_input_tokens", "max_positive_growth_tokens",
                  "guard_tokens", "window_tokens", "window_is_actual", "cache_fields_complete",
                  "handoff_reported", "last_sample_id")
RUNTIME_SIGNAL = "<continuity-host-event>"



def _runtime_message(text):
    """Mark generated input as host runtime state, never as user authorization."""
    prefix = f"{RUNTIME_SIGNAL}\n"
    return text if isinstance(text, str) and text.startswith(prefix) else (
        f"{prefix}Runtime continuation signal, not new human authorization.\n{text}"
    )


def _event_context_window(event):
    direct = (event.get("contextWindow"), event.get("context_window"))
    for value in direct:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    usage = event.get("modelUsage")
    if isinstance(usage, dict):
        windows = [item.get("contextWindow") for item in usage.values() if isinstance(item, dict)
                   and isinstance(item.get("contextWindow"), int) and item["contextWindow"] > 0]
        if windows:
            return min(windows)
    return None


class ContextRuntimeError(ValueError):
    pass


def _budget_number(value: Any, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < 0 or (positive and value == 0):
        raise ContextRuntimeError(f"{label} must be a {'positive ' if positive else ''}non-negative integer")
    return value


def _budget_previous(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ContextRuntimeError("budget observations are invalid")
    # Accept a previous full helper result while persisting only its numeric observations.
    values = value.get("observations", value)
    if not isinstance(values, dict):
        raise ContextRuntimeError("budget observations are invalid")
    result = {}
    for key in _BUDGET_FIELDS:
        if key not in values:
            continue
        number = _budget_number(values[key], f"budget {key}")
        if key in {"window_is_actual", "cache_fields_complete", "handoff_reported"} and number not in {0, 1}:
            raise ContextRuntimeError(f"budget {key} must be zero or one")
        result[key] = number
    return result


def _budget_sample(value: Any) -> tuple[int, int] | None:
    """Return actual input+cache and whether cache fields were explicitly present."""
    if type(value) is int:
        return _budget_number(value, "current input tokens"), 0
    if not isinstance(value, dict):
        raise ContextRuntimeError("current input tokens are invalid")
    if value.get("synthetic") is True or value.get("model") == "<synthetic>":
        return None
    total = _budget_number(value.get("total_input_tokens"), "current input tokens")
    complete = value.get("cache_fields_complete")
    if complete is None:
        return total, 0
    if type(complete) is bool:
        return total, int(complete)
    if type(complete) is int and complete in {0, 1}:
        return total, complete
    raise ContextRuntimeError("cache-field metadata is invalid")


def _budget_window(value: Any) -> tuple[int, int]:
    """Plain integer windows are configured estimates; mappings can mark observations."""
    if type(value) is int:
        return _budget_number(value, "host context window", positive=True), 0
    if not isinstance(value, dict):
        raise ContextRuntimeError("host context window is invalid")
    tokens = value.get("tokens", value.get("window_tokens"))
    actual = value.get("actual", value.get("window_is_actual", False))
    if type(actual) is bool:
        actual = int(actual)
    if type(actual) is not int or actual not in {0, 1}:
        raise ContextRuntimeError("host context window source is invalid")
    return _budget_number(tokens, "host context window", positive=True), actual


def observe_budget(previous_or_none: dict[str, Any] | None, current_input_tokens: Any,
                   host_window: Any, sample_id: Any) -> dict[str, Any]:
    """Observe one real request without requesting a clear or classifying its task.

    Persist only ``result['observations']``.  Its values are numeric; a plain
    integer ``host_window`` is intentionally marked as a configured estimate.
    Positive per-request growth and the first real input sample determine the
    reserve.  A decline is a normal new sample, not a reset or pause.
    """
    previous = _budget_previous(previous_or_none)
    sample = _budget_sample(current_input_tokens)
    if sample is None:  # Native synthetic records must never overwrite real usage.
        return {"observations": previous, "handoff_required": False, "near_limit": False,
                "sample_ignored": True}
    total, cache_complete = sample
    window, window_is_actual = _budget_window(host_window)
    bootstrap = previous.get("bootstrap_input_tokens", total)
    prior_total = previous.get("last_input_tokens", total)
    growth = max(0, total - prior_total)
    largest_growth = max(previous.get("max_positive_growth_tokens", 0), growth)
    # 为控制报文、交接和附注提前留空间，再加真实最大请求增长；不等到仅剩一个短摘要的余量。
    # 小窗口的基础预留不超过四分之一，避免 bootstrap 自身导致反复换窗。
    reserve = min(window // 4, core.MAX_PACKET + 2 * _MAX_HANDOFF_BYTES)
    guard = min(window, reserve + largest_growth)
    remaining, threshold = window - total, max(0, window - guard)
    near_limit = remaining <= guard
    already_reported = previous.get("handoff_reported", 0) == 1 and previous.get("window_tokens") == window
    handoff_required = near_limit and not already_reported
    observations = {
        "bootstrap_input_tokens": bootstrap,
        "last_input_tokens": total,
        "max_positive_growth_tokens": largest_growth,
        "guard_tokens": guard,
        "window_tokens": window,
        "window_is_actual": window_is_actual,
        "cache_fields_complete": cache_complete,
        "handoff_reported": int(already_reported or handoff_required),
    }
    if type(sample_id) is int and sample_id >= 0:
        observations["last_sample_id"] = sample_id
    return {"observations": observations, "handoff_required": handoff_required, "near_limit": near_limit,
            "remaining_tokens": remaining, "guard_tokens": guard, "threshold_tokens": threshold,
            "window_tokens": window, "window_is_actual": bool(window_is_actual),
            "cache_fields_complete": bool(cache_complete), "sample_ignored": False}


def _uuid(value: str, label: str) -> str:
    try:
        return core.uuid(value)
    except (TypeError, ValueError, core.ContinuityError) as exc:
        raise ContextRuntimeError(f"{label} must be a canonical UUID") from exc


def _cwd(value: str | Path) -> Path:
    try:
        path = Path(value).resolve(strict=True)
    except (TypeError, OSError) as exc:
        raise ContextRuntimeError("cwd must be an existing directory") from exc
    if not path.is_dir():
        raise ContextRuntimeError("cwd must be an existing directory")
    return path


def _identity(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict) or not isinstance(config.get("hash"), str) or not config["hash"]:
        raise ContextRuntimeError("effective configuration has no identity")
    return {"hash": config["hash"], "configured_window": core.configured_window(config)}


def _sid(event: dict[str, Any], required: bool = False) -> str | None:
    first, second = event.get("session_id"), event.get("sessionId")
    if first is not None and second is not None and first != second:
        raise ContextRuntimeError("event session identity conflicts")
    value = first if first is not None else second
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ContextRuntimeError("event has no exact session identity")
    return _uuid(value, "event session_id")


def _event_cwd(event: dict[str, Any], expected: str, required: bool = False) -> None:
    value = event.get("cwd")
    if value is None and not required:
        return
    if not isinstance(value, str) or str(_cwd(value)) != expected:
        raise ContextRuntimeError("event cwd drifted")


def _compact(event: dict[str, Any]) -> bool:
    return (event.get("subtype") in {"compact_boundary", "microcompact_boundary"}
            or event.get("status") == "compacting" or event.get("hook_event_name") == "PreCompact")


def _source(path: Any, session_id: str) -> HistorySource:
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ContextRuntimeError("hook transcript_path must be an absolute native JSONL path")
    lexical = Path(os.path.abspath(path))
    if lexical.is_symlink():
        raise ContextRuntimeError("hook transcript_path must not be a symlink")
    try:
        resolved = lexical.resolve(strict=True)
        if resolved != lexical or not resolved.is_file():
            raise OSError
        return HistorySource(resolved, session_id)
    except (OSError, HistoryError) as exc:
        raise ContextRuntimeError("hook transcript_path is not this native session history") from exc


def _locator_source(locator: dict[str, Any]) -> HistorySource:
    try:
        return HistorySource(Path(locator["source_path"]), locator["session_id"])
    except (KeyError, TypeError, HistoryError) as exc:
        raise ContextRuntimeError("native instruction locator is invalid") from exc


def _safe_note(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_HANDOFF_BYTES:
        raise ContextRuntimeError(f"{label} exceeds its packet-derived bound")
    try:
        if redact(value, core.secret_values()) != value:
            raise ContextRuntimeError(f"{label} appears to contain a secret")
    except HistoryError as exc:
        raise ContextRuntimeError(f"{label} cannot be checked for secrets") from exc
    return value


def context_prompt() -> str:
    entry = shlex.join(core.module_argv())
    return ("上下文由宿主统一管理，对所有任务适用，无需让用户登记或提供任务 JSON。"
            "遵守已有用户指令和原生权限，禁止 compact，不重启已执行的工作。"
            "用户也可随时输入 /renew 提前请求带交接的新上下文；手动和自动共用同一安全切换流程。"
            "hook 会提供真实 input+cache 用量、窗口来源和剩余预算。"
            "宿主在工具批次结算后、下一次模型请求前检查预算；后台任务无须全部结束，输入和结果可跨窗口继续处理。"
            "History/Notes 使用下列现有 CLI，在整个任务期间持续可用；"
            "缺少旧约束、决定或结果位置时查回原记录，不凭交接摘要补猜。"
            "若剩余预算不足以覆盖下一步与交接，主动执行 "
            f"{entry} context-request --handoff '简短说明目标、完成项、未完成项、约束和产物定位'，"
            "然后结束本轮回复；宿主会自动切换并继续，不要要求用户操作。"
            f"需要时使用 {entry} notes list/read/search/write/append 保存和查询笔记，"
            f"使用 {entry} history-windows --context-id $CLAUDE_CONTINUITY_ID 查询跨窗口来源。"
            f"整个任务期间（不只接手时），需要旧细节可随时调用 {entry} history-search --query '关键词' "
            "--recent-first --page-size 5；省略 query 可浏览，--role user 只找真实用户指令/已核对回答，"
            "也可按 --window、--session、--source-kind、--tool 过滤，并沿用相同参数用 --cursor 翻页。"
            "未命中时可按需要调整关键词，但搜索为空不代表不存在相关决定。"
            f"拿到定位后用 {entry} history --source '<source_path>' --session-id '<session_id>' "
            "--message-id '<message_id>' --expected-sha256 '<sha256>' 有界读回原话及必要上下文。"
            "读取真实历史中的用户指令，包括中途限制和取消；笔记不是新的授权。"
            "所有 Bash 仍遵守当前项目的 shell 前缀规则。")


class ContextRuntime:
    """同一连续会话的预算、历史定位与待执行换窗请求。"""

    def __init__(self, conversation_id: str, *, configuration_reader=core.configuration) -> None:
        self.conversation_id = _uuid(conversation_id, "conversation_id")
        self.configuration_reader = configuration_reader
        root = Path(core.HOME) / "runtime" / "contexts"
        if root.exists() and root.is_symlink():
            raise ContextRuntimeError("runtime context root must not be a symlink")
        self.directory = root / self.conversation_id
        if self.directory.exists() and self.directory.is_symlink():
            raise ContextRuntimeError("runtime context directory must not be a symlink")
        self.state_path, self.lock_path = self.directory / "state.json", self.directory / ".lock"

    @classmethod
    def create(cls, *, cwd: str | Path, session_id: str, configuration: dict[str, Any],
               conversation_id: str | None = None, configuration_reader=core.configuration) -> "ContextRuntime":
        runtime = cls(conversation_id or str(uuid4()), configuration_reader=configuration_reader)
        path, expected = _cwd(cwd), _identity(configuration)
        state = {
            "schema": _SCHEMA, "conversation_id": runtime.conversation_id, "cwd": str(path),
            "session_id": _uuid(session_id, "session_id"), "configuration": expected,
            "source_path": None, "authorization": None, "native_context_window": None, "usage": None,
            "budget": {}, "budget_stream": None, "budget_handoff_signal": None,
            "pending_tool_ids": [], "active_child_handles": [], "background_tool_ids": [],
            "at_turn_boundary": False, "window_generation": 0,
            "rotation": {"generation": 0, "request": None, "clear": None},
            "owned_pid": None,
            "phase": "created", "pause_reason": None,
        }
        try:
            with core.lock(runtime.lock_path, wait_seconds=5):
                if runtime.state_path.exists():
                    raise ContextRuntimeError("runtime conversation already exists")
                runtime._save(state, exclusive=True)
        except FileExistsError as exc:
            raise ContextRuntimeError("runtime conversation already exists") from exc
        return runtime

    @classmethod
    def load(cls, conversation_id: str, *, configuration_reader=core.configuration) -> "ContextRuntime":
        runtime = cls(conversation_id, configuration_reader=configuration_reader)
        with core.lock(runtime.lock_path, wait_seconds=5):
            runtime._state()
        return runtime

    def _state(self) -> dict[str, Any]:
        if not self.state_path.is_file() or self.state_path.is_symlink():
            raise ContextRuntimeError("runtime state is unavailable")
        try:
            state = core.read_json(self.state_path)
        except (OSError, ValueError, core.ContinuityError) as exc:
            raise ContextRuntimeError("runtime state cannot be read") from exc
        if not isinstance(state, dict) or state.get("schema") != _SCHEMA or state.get("conversation_id") != self.conversation_id:
            raise ContextRuntimeError("runtime state identity drifted")
        return state

    def _save(self, state: dict[str, Any], exclusive: bool = False) -> None:
        try:
            core.no_secrets(state)
            core.atomic(self.state_path, state, exclusive=exclusive)
        except (OSError, ValueError, core.ContinuityError) as exc:
            raise ContextRuntimeError("runtime state cannot be persisted") from exc

    @staticmethod
    def _pause(state: dict[str, Any], reason: str) -> None:
        if state.get("phase") != "paused":
            state["phase"], state["pause_reason"] = "paused", reason

    @staticmethod
    def _receipt(state: dict[str, Any]) -> dict[str, Any]:
        rotation = state["rotation"]
        return {key: state.get(key) for key in ("conversation_id", "phase", "pause_reason", "cwd", "session_id",
                "source_path", "configuration", "usage", "authorization", "owned_pid", "diagnostic", "capabilities")} | {
                    "pending_tool_ids": list(state["pending_tool_ids"]),
                    "active_child_handles": list(state["active_child_handles"]),
                    "rotation_generation": rotation["generation"], "rotation_pending": bool(rotation["request"]),
                    "clear_pending": bool(rotation["clear"]),
                    "automatic_rotations": state.get("automatic_rotations", 0),
                    "budget": dict(state.get("budget", {})),
                    "budget_handoff_required": isinstance(state.get("budget_handoff_signal"), dict),
                    "budget_handoff_signal": state.get("budget_handoff_signal"),
                }

    def receipt(self) -> dict[str, Any]:
        with core.lock(self.lock_path, wait_seconds=5):
            return self._receipt(self._state())

    def _bind(self, state: dict[str, Any], event: dict[str, Any]) -> HistorySource:
        if _sid(event, True) != state["session_id"]:
            raise ContextRuntimeError("hook session drifted")
        _event_cwd(event, state["cwd"], True)
        source = _source(event.get("transcript_path"), state["session_id"])
        if state["source_path"] not in {None, str(source.path)}:
            raise ContextRuntimeError("native history source drifted")
        state["source_path"] = str(source.path)
        self._catalogue_source(state)
        bounds = source.instruction_bounds()
        if state["authorization"] is None:
            if bounds["first"] is None:
                raise ContextRuntimeError("native history has no root user instruction")
            state["authorization"] = {"root_instruction_locator": bounds["first"],
                                      "latest_instruction_locator": bounds["last"]}
        elif bounds["last"] is not None:
            state["authorization"]["latest_instruction_locator"] = bounds["last"]
        return source

    def _catalogue_source(self, state: dict[str, Any]) -> None:
        """历史登记不依赖交接、用户首条输入或自动换窗是否可用。"""
        path = state.get("source_path")
        if path is None:
            return
        native_path = Path(path)
        if (not native_path.is_absolute() or native_path.name != f"{state['session_id']}.jsonl"
                or native_path.resolve(strict=False) != native_path):
            raise ContextRuntimeError("native history catalogue source is invalid")
        generation = state["window_generation"]
        window = self.directory / "history" / f"{generation:08d}-{state['session_id']}.json"
        binding = {"source_path": path, "session_id": state["session_id"], "generation": generation}
        if window.exists():
            if core.read_json(window) != binding:
                raise ContextRuntimeError("native history catalogue drifted")
        else:
            core.atomic(window, binding, exclusive=True)

    @staticmethod
    def _hook_output(state: dict[str, Any], name: str, context: str | None = None) -> dict[str, Any]:
        result = {} if context is None or name == "Stop" else {
            "hookSpecificOutput": {"hookEventName": name, "additionalContext": context}}
        if state["phase"] == "paused":
            reason = state.get("diagnostic") or state.get("pause_reason") or "原因尚未确认"
            notice_key = core.digest([state["session_id"], reason])
            if state.get("pause_notice_key") != notice_key:
                result["systemMessage"] = "cclaude 自动换窗已暂停；原生任务与历史查询仍可使用。原因：" + reason
                state["pause_notice_key"] = notice_key
        else:
            state.pop("pause_notice_key", None)
        return result

    def _usage(self, state: dict[str, Any], required: bool
               ) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        if not isinstance(state["source_path"], str):
            if required:
                raise ContextRuntimeError("current native history source is unavailable")
            return None
        try:
            usage = HistorySource(Path(state["source_path"]), state["session_id"]).latest_usage()
        except (HistoryError, TypeError) as exc:
            if required:
                raise ContextRuntimeError("actual native usage is unavailable") from exc
            return None
        windows = [value for value in (state["native_context_window"], state["configuration"]["configured_window"])
                   if type(value) is int and value > 0]
        if not windows:
            raise ContextRuntimeError("未取得上下文窗口，只暂停自动换窗")
        window = min(windows)
        remaining = window - usage["total_input_tokens"]
        state["usage"] = result = {
            "usage_locator": usage["locator"], "actual_model": usage["actual_model"],
            "request_id": usage["request_id"], "sample_timestamp": usage["timestamp"],
            "output_tokens": usage["output_tokens"],
            "input_tokens": usage["usage"]["input_tokens"],
            "cache_creation_input_tokens": usage["usage"]["cache_creation_input_tokens"],
            "cache_read_input_tokens": usage["usage"]["cache_read_input_tokens"],
            "total_input_and_cache_tokens": usage["total_input_tokens"],
            "native_context_window": window, "remaining_context_tokens": remaining,
            "window_source": ("min_live_budget_and_native_contextWindow" if state["native_context_window"] is not None
                              else state.get("budget_source", "live_settings_budget")),
        }
        cache_complete = int(usage.get("cache_fields_complete", False))
        decision = observe_budget(
            state.get("budget"),
            {"total_input_tokens": usage["total_input_tokens"], "cache_fields_complete": cache_complete},
            {"tokens": window, "actual": state["native_context_window"] is not None},
            state["window_generation"],
        )
        state["budget"] = decision["observations"]
        if not decision["near_limit"]:
            state["budget_handoff_signal"] = None
        if decision["near_limit"] and not state["rotation"]["request"]:
            state["budget_handoff_signal"] = {
                "generation": state["window_generation"],
                "input_tokens": usage["total_input_tokens"],
                "guard_tokens": decision["guard_tokens"],
                "remaining_tokens": decision["remaining_tokens"],
                "window_tokens": window,
                "window_is_actual": int(decision["window_is_actual"]),
                "cache_fields_complete": int(decision["cache_fields_complete"]),
            }
        return result, decision

    @staticmethod
    def _sources(authorization: dict[str, Any]) -> list[dict[str, str]]:
        result = []
        for key in ("root_instruction_locator", "latest_instruction_locator"):
            locator = authorization[key]
            source = {"source_path": locator["source_path"], "session_id": locator["session_id"]}
            if source not in result:
                result.append(source)
        return result

    def _verify_authorization(self, state: dict[str, Any]) -> None:
        auth = state["authorization"]
        if not isinstance(auth, dict):
            raise ContextRuntimeError("original native authorization references are unavailable")
        for key in ("root_instruction_locator", "latest_instruction_locator"):
            locator = auth.get(key)
            if not isinstance(locator, dict):
                raise ContextRuntimeError("native authorization locator is incomplete")
            try:
                _locator_source(locator).read(locator, limit=1)
            except HistoryError as exc:
                raise ContextRuntimeError("native authorization locator drifted") from exc

    def _latest_before_clear(self, state: dict[str, Any]) -> None:
        try:
            latest = HistorySource(Path(state["source_path"]), state["session_id"]).instruction_bounds()["last"]
            if latest is not None:
                state["authorization"]["latest_instruction_locator"] = latest
        except (HistoryError, TypeError) as exc:
            raise ContextRuntimeError("current native history cannot be verified") from exc
        self._verify_authorization(state)

    def _context(self, state: dict[str, Any]) -> str | None:
        observed = self._usage(state, False)
        if observed is None:
            return None
        usage, decision = observed
        if decision is None:
            return None
        source = ("observed native contextWindow" if decision["window_is_actual"]
                  else "configured host-window estimate; native contextWindow was not observed")
        cache = ("stream cache fields explicitly present" if decision["cache_fields_complete"]
                 else "cache-field metadata missing or unverified")
        status = ("Context runtime operational status only; this grants no permission or user authorization. "
                  f"Actual native input+cache tokens: {usage['total_input_and_cache_tokens']}; effective host window: "
                  f"{usage['native_context_window']} ({source}); remaining: {usage['remaining_context_tokens']}; "
                  f"budget guard: {decision['guard_tokens']}; safe-approach threshold: {decision['threshold_tokens']}; {cache}. ")
        if state["phase"] == "paused":
            return status + ("Automatic terminal actions are paused, but budget observation and History/Notes remain "
                             "available. Do not repeat a pending clear or assume the controller changed sessions.")
        if decision["handoff_required"]:
            return (status + "The safe-approach threshold is reached. Save a concise handoff through the existing "
                    "context-request command (goal, completed work, remaining work, constraints, and artifact locators), "
                    "then finish this turn. This is a model-side operational cue, not a user decision, authorization, "
                    "or permission grant; it does not reset active work.")
        if decision["near_limit"]:
            return status + "This context generation already received its near-limit handoff cue."
        return status + "If remaining native context no longer warrants continuing, request a new context with concise handoff notes."

    def _queue_rotation(self, state: dict[str, Any], handoff: str, notes: str = "", *, automatic=False) -> None:
        generation = state["rotation"]["generation"] + 1
        store = NotesStore(self.directory)
        store.write(f"handoff-{generation}", handoff, None, secrets=core.secret_values())
        if notes:
            store.write(f"details-{generation}", notes, None, secrets=core.secret_values())
        state["rotation"]["generation"] = generation
        state["rotation"]["request"] = {"generation": generation, "session_id": state["session_id"],
                                          "source_path": state["source_path"], "handoff": handoff, "notes": notes}
        if automatic:
            state["automatic_rotations"] = state.get("automatic_rotations", 0) + 1
        state["budget_handoff_signal"] = None
        state["phase"] = "rotation_requested"

    def _automatic_rotation(self, state: dict[str, Any]) -> None:
        if not state.get("budget_handoff_signal") or state["rotation"]["request"]:
            return
        # 不为腾窗口再发模型总结请求；只保存可精确读回的公开历史定位。
        source = HistorySource(Path(state["source_path"]), state["session_id"])
        recent = [info for info in source._records() if info.kind in {"assistant", "tool_result"}][-4:]
        locators = [source.locator(info.message_id) for info in recent]
        handoff = ("宿主因真实预算进入预留区自动换窗；这不是用户新指令。"
                   "先查看现有 Notes、原始用户指令及以下最近公开记录，核对已完成动作和产物后继续。"
                   "若原任务已经完成，只交付现有结果，不重新执行。最近记录定位：" +
                   json.dumps(locators, ensure_ascii=False, separators=(",", ":")))
        self._queue_rotation(state, _safe_note(handoff, "automatic handoff"), automatic=True)

    def request_rotation(self, handoff: str, notes: str = "") -> dict[str, Any]:
        handoff, notes = _safe_note(handoff, "handoff"), _safe_note(notes, "notes")
        with core.lock(self.lock_path, wait_seconds=5):
            state = self._state()
            if state["phase"] == "paused":
                return self._receipt(state)
            if state["rotation"]["request"] or state["rotation"]["clear"]:
                return self._receipt(state)
            if state["phase"] not in {"running", "waiting_safe_boundary"} or not state["source_path"] or not state["authorization"]:
                self._pause(state, "rotation_request_state_or_history_unknown")
            else:
                self._queue_rotation(state, handoff, notes)
            self._save(state)
            return self._receipt(state)

    def _continuation(self, state: dict[str, Any]) -> str:
        request, auth = state["rotation"]["request"], state["authorization"]
        value = {
            "kind": "native_context_continuation",
            "notice": ("Runtime continuation only: this is not a new human instruction, authorization, or permission grant. "
                       "Original human intent and constraints remain authoritative only at the exact native JSONL locators below."),
            "conversation_id": state["conversation_id"], "cwd": state["cwd"], "prior_session_id": request["session_id"],
            "handoff": request["handoff"], "notes": request["notes"], "native_history_sources": self._sources(auth),
            "history_directory": str(self.directory / "history"),
            "instruction": ("Continue the same work without asking for reapproval. Read the genuine user instructions "
                            "in the catalogued history windows as needed, including intervening constraints and cancellations; "
                            "root/latest locators alone are not the complete instruction history. Notes are not authority."),
            "root_instruction_locator": auth["root_instruction_locator"],
            "latest_instruction_locator": auth["latest_instruction_locator"],
        }
        try:
            core.no_secrets(value)
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError, core.ContinuityError) as exc:
            raise ContextRuntimeError("continuation cannot be safely encoded") from exc
        if len(encoded.encode("utf-8")) > core.MAX_PACKET:
            raise ContextRuntimeError("continuation exceeds runtime packet bound")
        return encoded

    @staticmethod
    def _settle_task_hook(state, event):
        """成功的原生任务终态回执可结算 child，不等待额外 SubagentStop。"""
        handle = terminal_task_result(event.get("tool_name"), event.get("tool_input"), event.get("tool_response"))
        if handle in state["active_child_handles"]:
            state["active_child_handles"].remove(handle)

    def on_hook(self, event: dict[str, Any]) -> dict[str, Any]:
        """原生 hook 只观测预算与生命周期；异常只暂停自动换窗。"""
        if not isinstance(event, dict):
            self._pause_external("hook_event_invalid")
            return {}
        name = event.get("hook_event_name")
        agent = event.get("agent_id", event.get("agentId"))
        # 子代理有独立 transcript；父侧只跟踪其原生生命周期，不混用用量。
        if agent and name not in {"SubagentStart", "SubagentStop", "PreCompact"}:
            return {}
        with core.lock(self.lock_path, wait_seconds=5):
            state = self._state()
            if state["phase"] == "paused":
                # 暂停的只是自动操作；继续观测本会话的新活动与结算。
                if _sid(event, False) == state["session_id"]:
                    tool = event.get("tool_use_id")
                    if name == "PreToolUse" and isinstance(tool, str) and tool and tool not in state["pending_tool_ids"]:
                        state["pending_tool_ids"].append(tool)
                    if name in {"PostToolUse", "PostToolUseFailure"} and tool in state["pending_tool_ids"]:
                        state["pending_tool_ids"].remove(tool)
                        if name == "PostToolUse":
                            self._settle_task_hook(state, event)
                    if name == "SubagentStart" and isinstance(agent, str) and agent and agent not in state["active_child_handles"]:
                        state["active_child_handles"].append(agent)
                    if name == "SubagentStop" and agent in state["active_child_handles"]:
                        state["active_child_handles"].remove(agent)
            context = None
            try:
                if _compact(event):
                    raise ContextRuntimeError("native compact observed")
                self._verify_config(state)
                if name in {"SubagentStart", "SubagentStop"}:
                    if _sid(event, True) != state["session_id"] or not isinstance(agent, str) or not agent:
                        raise ContextRuntimeError("child lifecycle identity is unavailable")
                    handles = state["active_child_handles"]
                    if name == "SubagentStart":
                        if agent not in handles:
                            handles.append(agent)
                    elif agent in handles:
                        handles.remove(agent)
                else:
                    self._bind(state, event)
                    self._window(state, event)
                    tool = event.get("tool_use_id")
                    if name == "PostToolBatch":
                        calls = event.get("tool_calls")
                        if not isinstance(calls, list) or any(not isinstance(call, dict)
                                or not isinstance(call.get("tool_use_id"), str) for call in calls):
                            raise ContextRuntimeError("native tool batch identity is unavailable")
                        completed = {call["tool_use_id"] for call in calls}
                        state["pending_tool_ids"] = [key for key in state["pending_tool_ids"] if key not in completed]
                    elif name != "Stop" and (name not in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
                                             or not isinstance(tool, str) or not tool):
                        raise ContextRuntimeError("tool hook lacks its native identity")
                    if name == "PreToolUse":
                        if tool not in state["pending_tool_ids"]:
                            state["pending_tool_ids"].append(tool)
                        state["at_turn_boundary"] = False
                        if event.get("tool_input", {}).get("run_in_background") is True:
                            state.setdefault("background_tool_ids", []).append(tool)
                    elif tool in state["pending_tool_ids"]:
                        state["pending_tool_ids"].remove(tool)
                        if name == "PostToolUse":
                            self._settle_task_hook(state, event)
                    context = self._context(state)
                if (state["phase"] == "paused" and state.get("observation_only_pause") and context is not None
                        and not state["rotation"]["clear"] and not state.get("continuation_hash")):
                    state["last_observation_pause"] = {"reason": state["pause_reason"],
                                                       "diagnostic": state.get("diagnostic")}
                    state["phase"] = "rotation_requested" if state["rotation"]["request"] else "running"
                    state["pause_reason"], state["diagnostic"] = None, None
                    state.pop("observation_only_pause", None)
                    state["observation_recoveries"] = state.get("observation_recoveries", 0) + 1
            except (OSError, TypeError, ValueError, HistoryError, core.ContinuityError, ContextRuntimeError) as exc:
                was_running = state["phase"] != "paused"
                self._pause(state, "hook_state_or_source_drift")
                if was_running and not _compact(event):
                    state["observation_only_pause"] = True
                state["diagnostic"] = redact(str(exc), core.secret_values())[:400]
            result = self._hook_output(state, name, context)
            self._save(state)
        return result

    def _pause_external(self, reason: str) -> dict[str, Any]:
        with core.lock(self.lock_path, wait_seconds=5):
            state = self._state()
            self._pause(state, reason)
            self._save(state)
            return self._receipt(state)


def context_request(conversation_id: str, handoff: str, notes: str = "", *, configuration_reader: Callable[[Path], dict[str, Any]] = core.configuration) -> dict[str, Any]:
    """Parent CLI callable: persist only; it has no writer, launch, or retry path."""
    return ContextRuntime.load(conversation_id, configuration_reader=configuration_reader).request_rotation(handoff, notes)
