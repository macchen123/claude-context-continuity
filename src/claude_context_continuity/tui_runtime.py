"""原生交互 TUI 的薄控制层；仅操作自己创建的独立 tmux server。"""
from __future__ import annotations

import json
from copy import deepcopy
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import time
from uuid import uuid4

from . import core, native_control, __version__
from .context_runtime import (ContextRuntime, ContextRuntimeError, _safe_note, _source, _uuid,
                              context_prompt, _runtime_message, _event_context_window)
from .history import HistorySource, redact


DURABLE_CRON_COMPAT_ENV = "CCLAUDE_DURABLE_CRON_COMPAT"


def _durable_cron_compat_enabled():
    value = os.environ.get(DURABLE_CRON_COMPAT_ENV, "on").lower()
    if value not in {"on", "off"}:
        raise ContextRuntimeError(f"{DURABLE_CRON_COMPAT_ENV} 只接受 on 或 off")
    return value == "on"


_PLUGIN_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch", "Stop",
    "SubagentStart", "SubagentStop", "PreCompact",
)


def _plugin_manifest(context_id):
    """Return the unique session-local native plugin manifest without touching settings."""
    context_id = _uuid(context_id, "continuity context_id")
    hook_argv = core.module_argv("tui-hook", "--context-id", context_id)
    hook = {
        "type": "command",
        "command": hook_argv[0],
        "args": hook_argv[1:],
    }
    return {
        "name": "cclaude",
        "version": __version__,
        "author": {"name": "macchen123"},
        "description": "Session-local context budget, History, Notes, and manual context switch.",
        "hooks": {event: [{"hooks": [hook]}] for event in _PLUGIN_EVENTS},
    }


def _new_context_command(context_id):
    """生成绑定当前 context 的手动入口；解释器路径取实际安装环境。"""
    context_id = _uuid(context_id, "continuity context_id")
    request = shlex.join(core.module_argv("context-request", "--context-id", context_id))
    status = shlex.join(core.module_argv("tui-status", "--context-id", context_id))
    return f"""---
name: renew
description: 保存当前进展并请求干净的新上下文，继续同一项工作
disable-model-invocation: true
---

用户手动请求带交接的新上下文。只改变换窗时机，不扩大原任务授权，不重跑已完成工作。

1. 遵守当前项目指令和 shell 前缀约束。记录仍在运行的后台任务 ID、输出位置和下一步，不杀任务，也不必等它们全部完成。
2. 保存简短交接：当前目标、限制、已完成和未完成事项、准确产物/历史来源，以及下一步。必要细节使用现有 Notes；不复制整段 transcript、隐藏思考或凭证。
3. 按本项目命令约束调用现有唯一请求入口，替换下面的交接占位文字并正确引用 shell 参数：

```sh
{request} --handoff '简短且真实的目标、进展、约束、来源和下一步'
```

4. 查看返回的 phase/pause_reason。rotation_requested 或 waiting_safe_boundary 只表示请求/等待，不能宣称换窗已成功；若已存在请求，不重复提交，可用下面的只读命令核对：

```sh
{status}
```

5. 请求被接受后结束当前回合；宿主在当前直接工具调用结算后通过原生队列换窗。后台任务继续运行，已提交输入在新窗口处理，输入框草稿保持原样。若暂停则说明实际原因，不清状态、不盲目重试。

不要自行执行 /clear、向终端注入内容、创建新控制器或修改全局设置。新上下文由既有自动接续机制加载交接与 History/Notes。
"""


def _write_plugin(plugin, context_id):
    """只写当前会话私有 plugin，不安装全局命令或权限。"""
    command = plugin / "commands" / "renew.md"
    command.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with command.open("x", encoding="utf-8") as handle:
        handle.write(_new_context_command(context_id))
    core.atomic(plugin / ".claude-plugin/plugin.json", _plugin_manifest(context_id), exclusive=True)


class TuiRuntime(ContextRuntime):
    """复用同一预算、History/Notes、授权和轮换记录，不建立业务任务状态。"""

    @staticmethod
    def _receipt(state):
        return ContextRuntime._receipt(state) | {key: state.get(key) for key in (
            "transport", "initial_session_started", "native_clear_confirmations",
            "native_resume_confirmations", "continuation_observed", "manual_clear_count", "controller_pid",
            "durable_cron_compat", "native_cli_version", "native_control", "output_budget")}

    def _verify_config(self, state):
        # 只读取实时预算；模型、权限和其他设置不属于本层管控范围。
        reader = self.configuration_reader
        observed = reader(Path(state["cwd"]), live_window=True) if reader is core.configuration else reader(Path(state["cwd"]))
        window = core.configured_window(observed)
        startup = state.setdefault("startup_configuration", dict(state["configuration"]))
        state["configuration"] = {"hash": observed["hash"], "configured_window": window}
        state["settings_changed_since_start"] = observed["hash"] != startup["hash"]
        state["budget_source"] = "live_settings_budget"

    def _window(self, state, event):
        observed = _event_context_window(event)
        if observed is not None:
            state["native_context_window"] = observed

    @staticmethod
    def _resume_eligible(state, sid):
        """用户可在任意正常回合恢复；未结自动输入仍不能重放。"""
        if (state["rotation"].get("clear") is not None
                or state.get("continuation_hash") is not None
                or state["pending_tool_ids"]):
            raise ContextRuntimeError("仍有未结活动；保留原生恢复，仅暂停自动换窗")

    @staticmethod
    def _same_resume_binding(state, sid, source_path, cwd):
        return (isinstance(state, dict) and state.get("session_id") == sid
                and state.get("source_path") == source_path and state.get("cwd") == cwd)

    @staticmethod
    def _catalogue_floor(state, entries):
        rotation = state.get("rotation")
        if not isinstance(rotation, dict):
            raise ContextRuntimeError("恢复窗口 rotation 无效")
        values = [entry["generation"] for entry in entries]
        for value in (state.get("window_generation"), rotation.get("generation")):
            if type(value) is not int or value < 0:
                raise ContextRuntimeError("恢复窗口 generation 无效")
            values.append(value)
        return max(values, default=0)

    def _catalogue_entries(self, runtime):
        """Read only canonical reference entries and validate each through _catalogue_source."""
        history = core.safe_path(runtime.directory, "history", exists=False)
        if not history.exists():
            return []
        if not history.is_dir() or history.is_symlink():
            raise ContextRuntimeError("native history catalogue is unavailable")
        entries = []
        for path in sorted(history.glob("*.json"), key=lambda item: item.name):
            name = path.name
            if not (name.endswith(".json") and len(name) > 14 and name[:8].isdigit() and name[8] == "-"):
                continue
            try:
                expected_generation = int(name[:8])
                expected_sid = _uuid(name[9:-5], "catalogue filename session_id")
            except ContextRuntimeError:
                continue
            try:
                raw = core.read_json(core.safe_path(runtime.directory, path))
                if not isinstance(raw, dict):
                    raise ContextRuntimeError("native history catalogue entry is invalid")
                probe = {"session_id": raw.get("session_id"), "source_path": raw.get("source_path"),
                         "window_generation": raw.get("generation")}
                binding = runtime._catalogue_source(probe, require_existing=True)
            except (OSError, TypeError, ValueError, KeyError) as exc:
                raise ContextRuntimeError("native history catalogue entry is invalid") from exc
            expected_path = history / f"{expected_generation:08d}-{expected_sid}.json"
            if raw != binding or path != expected_path:
                raise ContextRuntimeError("native history catalogue entry is invalid")
            entries.append(binding)
        return sorted(entries, key=lambda entry: (entry["generation"], entry["session_id"], entry["source_path"]))

    def _resume_candidate(self, state, incoming_state, source, sid):
        """Reset only transient ownership while retaining the target event identity on a deep copy."""
        entries = self._catalogue_entries(self)
        floor = self._catalogue_floor(state, entries)
        source_path = str(source.path)
        known = [entry["generation"] for entry in entries
                 if (entry["session_id"], entry["source_path"]) == (sid, source_path)]
        incoming_generation = incoming_state.get("window_generation") if isinstance(incoming_state, dict) else None
        if (self._same_resume_binding(incoming_state, sid, source_path, state["cwd"])
                and type(incoming_generation) is int and incoming_generation in known
                and incoming_generation >= floor):
            generation = incoming_generation
        else:
            generation = floor + 1
        candidate = deepcopy(state)
        candidate["phase"], candidate["pause_reason"] = "running", None
        candidate["rotation"] = {"generation": max(floor, generation), "request": None, "clear": None}
        candidate["window_generation"] = generation
        candidate["session_id"], candidate["source_path"], candidate["authorization"] = sid, None, None
        candidate["native_context_window"] = None
        candidate["usage"], candidate["budget_stream"], candidate["budget_handoff_signal"] = None, None, None
        candidate["budget"] = {}
        candidate["deferred_inputs"] = []
        candidate["at_turn_boundary"] = False
        for key in ("output_budget", "tool_batch_boundary", "rotation_model", "rotation_permission",
                    "rotation_deadline", "continuation_hash", "continuation_observed", "diagnostic",
                    "observation_only_pause", "pause_notice_key", "last_observation_pause", "stop_observed_at",
                    "stop_text_hash", "stop_serial", "stop_turn_generation", "stop_snapshot",
                    "settlement_stop_serial", "resume_safe_boundary", "resume_snapshot"):
            candidate.pop(key, None)
        return candidate

    def _lineage_catalogue_entries(self, owner, owner_state, authorization):
        """Return every validated reference from the root window through the owner's exact current source."""
        entries = self._catalogue_entries(owner)
        current = (owner_state["session_id"], owner_state["source_path"])
        current_generation = owner_state["window_generation"]
        if not any((entry["session_id"], entry["source_path"], entry["generation"])
                   == (*current, current_generation) for entry in entries):
            raise ContextRuntimeError("恢复来源未登记为 owner 的当前原生历史")
        root = authorization["root_instruction_locator"]
        latest = authorization["latest_instruction_locator"]
        root_pair = (root["session_id"], root["source_path"])
        latest_pair = (latest["session_id"], latest["source_path"])
        root_generations = [entry["generation"] for entry in entries
                            if (entry["session_id"], entry["source_path"]) == root_pair
                            and entry["generation"] <= current_generation]
        latest_generations = [entry["generation"] for entry in entries
                              if (entry["session_id"], entry["source_path"]) == latest_pair
                              and entry["generation"] <= current_generation]
        if not root_generations or not latest_generations:
            raise ContextRuntimeError("授权 lineage 缺少已登记的原生历史")
        first_generation = min(root_generations)
        if first_generation > current_generation:
            raise ContextRuntimeError("授权 lineage generation 不连续")
        references = [entry for entry in entries
                      if first_generation <= entry["generation"] <= current_generation]
        present = {(entry["session_id"], entry["source_path"]) for entry in references}
        if root_pair not in present or latest_pair not in present or current not in present:
            raise ContextRuntimeError("授权 lineage 历史不完整")
        return references

    def _resolve_resume_authorization(self, incoming_state, source, sid, cwd):
        """Resolve only an exact, settled owner lineage for a host-only native source."""
        source_path = str(source.path)
        owners = []

        def consider(owner, owner_state):
            if not isinstance(owner_state, dict):
                return
            if owner_state.get("session_id") != sid or owner_state.get("source_path") != source_path:
                return
            if owner_state.get("cwd") != cwd:
                raise ContextRuntimeError("恢复来源与已登记 owner 的工作目录不符")
            rotation = owner_state.get("rotation")
            if not isinstance(rotation, dict) or type(owner_state.get("window_generation")) is not int:
                raise ContextRuntimeError("恢复 owner 状态无效")
            if (rotation.get("request") is not None or rotation.get("clear") is not None
                    or owner_state.get("continuation_hash") is not None
                    or owner_state.get("pending_tool_ids")
                    or owner_state.get("phase") in {"clear_sent", "awaiting_tui_prompt",
                                                    "continuation_dispatching", "awaiting_continuation"}):
                raise ContextRuntimeError("恢复 owner 仍有未结自动操作")
            owner._catalogue_source(owner_state, require_existing=True)
            authorization = owner_state.get("authorization")
            if authorization is None:
                return
            owner._verify_authorization(owner_state)
            owners.append((owner.conversation_id, {
                "root_instruction_locator": deepcopy(authorization["root_instruction_locator"]),
                "latest_instruction_locator": deepcopy(authorization["latest_instruction_locator"]),
            }, self._lineage_catalogue_entries(owner, owner_state, authorization)))

        consider(self, incoming_state)
        root = self.directory.parent
        try:
            directories = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise ContextRuntimeError("无法枚举既有私有 context 状态") from exc
        for directory in directories:
            if directory == self.directory or not directory.is_dir() or directory.is_symlink():
                continue
            try:
                owner = ContextRuntime(_uuid(directory.name, "context directory"),
                                       configuration_reader=self.configuration_reader)
                if owner.directory != directory:
                    continue
                owner_state = owner._state()
            except (OSError, TypeError, ValueError, KeyError):
                continue
            consider(owner, owner_state)
        if not owners:
            return None
        root_latest = (owners[0][1]["root_instruction_locator"], owners[0][1]["latest_instruction_locator"])
        if any((authorization["root_instruction_locator"], authorization["latest_instruction_locator"])
               != root_latest for _, authorization, _ in owners[1:]):
            raise ContextRuntimeError("恢复来源存在相互冲突的已验证授权 lineage")
        return min(owners, key=lambda owner: owner[0])[1:]

    def _adopt_lineage_catalogue(self, candidate, incoming_state, source, references):
        """Copy validated source references into this context without touching another owner's registry."""
        entries = self._catalogue_entries(self)
        floor = self._catalogue_floor(candidate, entries)
        source_path = str(source.path)
        target = (candidate["session_id"], source_path)
        unique = []
        seen = set()
        for reference in references:
            key = (reference["session_id"], reference["source_path"])
            if key not in seen:
                seen.add(key)
                unique.append(reference)
        if target not in seen:
            raise ContextRuntimeError("恢复 lineage 缺少当前来源引用")
        local = {}
        for entry in entries:
            local.setdefault((entry["session_id"], entry["source_path"]), []).append(entry["generation"])
        for reference in unique:
            key = (reference["session_id"], reference["source_path"])
            if key == target or key in local:
                continue
            floor += 1
            probe = {"session_id": reference["session_id"], "source_path": reference["source_path"],
                     "window_generation": floor}
            self._catalogue_source(probe)
            local[key] = [floor]
        target_generations = local.get(target, [])
        incoming_bound = self._same_resume_binding(incoming_state, *target, candidate["cwd"])
        if incoming_bound and any(generation >= floor for generation in target_generations):
            target_generation = max(generation for generation in target_generations if generation >= floor)
        else:
            floor += 1
            target_generation = floor
            self._catalogue_source({"session_id": target[0], "source_path": target[1],
                                    "window_generation": target_generation})
        candidate["window_generation"] = target_generation
        candidate["rotation"]["generation"] = max(candidate["rotation"]["generation"], floor, target_generation)

    def _bind_resume(self, state, event, sid, *, incoming_state=None):
        """Bind an exact native resume source, including a verified prior owner for host-only windows."""
        self._resume_eligible(state, sid)
        source = _source(event.get("transcript_path"), sid)
        activity, latest = source.activity(), source.latest_usage()
        if latest["cwd"] != state["cwd"]:
            raise ContextRuntimeError("恢复来源的实际模型用量工作目录不符")
        if activity["pending_tools"]:
            raise ContextRuntimeError("恢复来源仍有未结算的直接工具调用，不自动接管")
        incoming_state = state if incoming_state is None else incoming_state
        candidate = self._resume_candidate(state, incoming_state, source, sid)
        candidate["source_path"] = str(source.path)
        bounds = source.instruction_bounds()
        if bounds["first"] is None:
            selected = self._resolve_resume_authorization(incoming_state, source, sid, state["cwd"])
            if selected is None:
                self._catalogue_source(candidate)
                self._window(candidate, event)
                observed = self._usage(candidate, True)
                if observed is None:
                    raise ContextRuntimeError("恢复来源没有可用的实际原生用量")
                usage, _ = observed
                candidate["native_session_model"] = usage["actual_model"]
                candidate["phase"] = "paused"
                candidate["pause_reason"] = "native history has no root user instruction"
                candidate["observation_only_pause"] = True
                state.clear()
                state.update(candidate)
                return
            authorization, references = selected
            candidate["authorization"] = deepcopy(authorization)
            self._verify_authorization(candidate)
            self._adopt_lineage_catalogue(candidate, incoming_state, source, references)
            source = self._bind(candidate, event)
        else:
            source = self._bind(candidate, event)
        self._verify_authorization(candidate)
        self._window(candidate, event)
        observed = self._usage(candidate, True)
        if observed is None:
            raise ContextRuntimeError("恢复来源没有可用的实际原生用量")
        usage, _ = observed
        candidate["native_session_model"] = usage["actual_model"]
        candidate["resume_safe_boundary"] = True
        candidate["resume_snapshot"] = {
            "instruction_head": source.instruction_bounds()["last"],
            "usage_locator": usage["usage_locator"],
        }
        candidate["native_resume_confirmations"] = candidate.get("native_resume_confirmations", 0) + 1
        state.clear()
        state.update(candidate)

    def _resume_flushed(self, state, source):
        """恢复后的首次清空只接受未被并发来源改写的精确历史快照。"""
        snapshot = state.get("resume_snapshot")
        if not isinstance(snapshot, dict):
            raise ContextRuntimeError("恢复安全边界缺少历史快照")
        if source.instruction_bounds()["last"] != snapshot.get("instruction_head"):
            self._pause(state, "恢复来源在首次安全边界前收到新的用户指令，不自动清空")
            return False
        if source.latest_usage()["locator"] != snapshot.get("usage_locator"):
            self._pause(state, "恢复来源在首次安全边界前发生新的模型活动，不自动清空")
            return False
        return True

    def _repair_durable_cron(self, state, event, result):
        """只在原生工具尚处于忙碌回合的同步回执中修复自身新建任务。"""
        tool_input = event.get("tool_input")
        if (event.get("hook_event_name") != "PostToolUse" or event.get("tool_name") != "CronCreate"
                or not isinstance(tool_input, dict) or tool_input.get("durable") is not True):
            return result
        compat = state.get("durable_cron_compat", {})
        if not compat.get("enabled"):
            return result
        from . import durable_cron, tmux_transport
        try:
            tmux_transport.inspect(state["tmux"])
            outcome = durable_cron.repair_created_task(state, event, self.directory)
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            outcome = {"status": "error", "reason": redact(str(exc), core.secret_values())[:300]}
        compat["last_result"] = outcome
        self._save(state)
        if outcome.get("status") == "not_needed":
            return result
        if outcome.get("status") == "applied":
            notice = ("cclaude 持久任务兼容层已修正本进程的调度会话绑定，未改变任务内容、时间或数量；"
                      "这不代表任务已自动触发，仍须按实际触发记录报告。")
        else:
            notice = ("cclaude 持久任务兼容检查未应用修复，不能据创建成功宣称自动触发正常。"
                      "用 tui-status 检查 durable_cron_compat.last_result；不改锁或重放任务来掩盖失败。")
        result = dict(result)
        specific = dict(result.get("hookSpecificOutput", {}))
        previous = specific.get("additionalContext", "")
        specific.update(hookEventName="PostToolUse", additionalContext="\n".join(filter(None, (previous, notice))))
        result["hookSpecificOutput"] = specific
        return result

    def on_hook(self, event):
        if not isinstance(event, dict):
            return super().on_hook(event)
        name = event.get("hook_event_name")
        if event.get("agent_id") or event.get("agentId"):
            if name not in {"SubagentStart", "SubagentStop", "PreCompact"}:
                return {}
        if name == "Stop":
            try:
                self.recover_observation(event.get("session_id"))
            except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
                pass  # 本轮仍无法确认时，只保留自动换窗暂停。
        with core.lock(self.lock_path, wait_seconds=5):
            state = self._state()
            permission = event.get("permission_mode")
            if permission is not None:
                state["native_permission_mode"] = permission
                self._save(state)
        if name == "UserPromptSubmit":
            return self._user_prompt(event)
        if name != "SessionStart":
            if name == "Stop":
                with core.lock(self.lock_path, wait_seconds=5):
                    state = self._state()
                    if event.get("session_id") != state["session_id"]:
                        self._pause(state, "TUI Stop 会话身份不符")
                    else:
                        state["at_turn_boundary"] = False
                        state["stop_observed_at"] = time.monotonic()
                    self._save(state)
            result = super().on_hook(event)
            # 从真实 hook 绑定的新 JSONL 确认接续已被原生宿主消费。
            with core.lock(self.lock_path, wait_seconds=5):
                state = self._state()
                if event.get("session_id") == state["session_id"]:
                    if name == "PostToolUse":
                        result = self._observe_output(state, event, result)
                    elif name == "PostToolBatch":
                        result = self._finish_batch(state, event, result)
                if name == "Stop" and event.get("session_id") == state["session_id"] and state.get("source_path"):
                    source = HistorySource(Path(state["source_path"]), state["session_id"])
                    final_text = event.get("last_assistant_message")
                    state["stop_text_hash"] = core.digest(final_text.strip()) if isinstance(final_text, str) else None
                    state["at_turn_boundary"] = True
                    state["stop_serial"] = state.get("stop_serial", 0) + 1
                    state["stop_turn_generation"] = state.get("turn_generation", 0)
                    state["stop_snapshot"] = {"instruction_head": source.instruction_bounds()["last"]}
                    self._save(state)
                if state["phase"] != "paused" and state.get("continuation_hash"):
                    self._confirm_continuation(state)
                result = self._repair_durable_cron(state, event, result)
                self._save(state)
            return result
        return self._session_start(event)

    @staticmethod
    def _output_accounting(state):
        usage = state.get("usage")
        if not usage:
            return None
        sample = core.digest([state["session_id"], usage["request_id"]])
        accounting = state.get("output_budget")
        if not accounting or accounting["sample"] != sample:
            accounting = {"sample": sample, "items": {}, "total_text_bytes": 0,
                          "basis": "native_input_and_output_tokens_plus_pending_utf8_json_bytes"}
            state["output_budget"] = accounting
        return accounting

    def _observe_output(self, state, event, result, *, can_replace=True):
        from .result_budget import bound_result
        accounting = self._output_accounting(state)
        tool_id = event.get("tool_use_id")
        if accounting is None or not isinstance(tool_id, str) or tool_id in accounting["items"]:
            return result
        response, name = event.get("tool_response"), event.get("tool_name")
        blocks = response if isinstance(response, list) else response.get("content", []) if isinstance(response, dict) else []
        rich = (isinstance(response, dict) and (
            name == "Read" and response.get("type") != "text" or response.get("isImage") is True))
        rich = rich or isinstance(blocks, list) and any(isinstance(block, dict)
            and block.get("type") in {"image", "image_url", "audio", "resource"} for block in blocks)
        if rich or response is None:
            # 多模态 payload 交由原生计量，不把 base64 字节冒充文本 token。
            accounting["items"][tool_id] = {"text_bytes": 0, "native_rich_or_unmeasured": True, "defer": False}
            return result
        raw_bytes = len(json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        usage = state["usage"]
        room = (usage["remaining_context_tokens"] - usage.get("output_tokens", 0)
                - accounting["total_text_bytes"] - state["budget"]["guard_tokens"])
        limit = min(16_000, max(1, room))
        model_bytes, defer = raw_bytes, raw_bytes > max(0, room)
        if can_replace and raw_bytes > limit:
            bounded = bound_result(name, response, directory=self.directory, byte_limit=limit)
            model_bytes = bounded["model_bytes"] if bounded["model_bytes"] is not None else raw_bytes
            defer = bounded["defer_required"] and raw_bytes > max(0, room)
            if bounded["replaced"]:
                result = dict(result)
                specific = dict(result.get("hookSpecificOutput", {}))
                specific.update(hookEventName="PostToolUse", updatedToolOutput=bounded["response"])
                reference = bounded["reference"]
                notice = (f"本次工具输出已限长，完整脱敏结果：{reference['path']}，SHA256={reference['sha256']}。"
                          "需要细节时有界读取，不为恢复输出重跑已完成工具。")
                specific["additionalContext"] = "\n".join(filter(None, (specific.get("additionalContext"), notice)))
                result["hookSpecificOutput"] = specific
        context = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        model_bytes += len(context.encode("utf-8"))
        accounting["items"][tool_id] = {"text_bytes": model_bytes, "raw_bytes": raw_bytes, "defer": defer}
        accounting["total_text_bytes"] += model_bytes
        return result

    def _finish_batch(self, state, event, result):
        if state["phase"] == "paused" or not state.get("usage"):
            return result
        for call in event["tool_calls"]:
            self._observe_output(state, call, {}, can_replace=False)
        accounting = self._output_accounting(state)
        usage = state["usage"]
        projected = (usage["total_input_and_cache_tokens"] + usage.get("output_tokens", 0)
                     + accounting["total_text_bytes"]
                     + len(result.get("hookSpecificOutput", {}).get("additionalContext", "").encode("utf-8")))
        if (projected >= usage["native_context_window"] - state["budget"]["guard_tokens"]
                or any(item["defer"] for item in accounting["items"].values())):
            state["budget_handoff_signal"] = {"generation": state["window_generation"],
                "reason": "pending_tool_output_budget", "projected_text_budget": projected}
        self._automatic_rotation(state)
        if state["rotation"]["request"]:
            state["tool_batch_boundary"] = {"session_id": state["session_id"],
                "turn_generation": state.get("turn_generation", 0), "usage_locator": usage["usage_locator"]}
            state["at_turn_boundary"] = True
            result = dict(result)
            result["continue"] = False
            result["stopReason"] = "正在自动切换上下文；后台任务继续运行，后续输入和结果在新窗口处理。"
        return result

    def _recover_prompt_observation(self, state, event):
        """Let a genuine newly persisted user record restore an observation-only paused source."""
        if state["phase"] != "paused" or not state.get("observation_only_pause"):
            return False
        if state["rotation"]["clear"] or state.get("continuation_hash") or state["pending_tool_ids"]:
            return False
        try:
            self._verify_config(state)
            self._bind(state, event)
            self._verify_authorization(state)
            self._window(state, event)
            if self._usage(state, True) is None:
                raise ContextRuntimeError("恢复来源没有可用的实际原生用量")
        except (OSError, TypeError, ValueError, KeyError) as exc:
            state["diagnostic"] = redact(str(exc), core.secret_values())[:400]
            return False
        state["last_observation_pause"] = {"reason": state["pause_reason"],
                                           "diagnostic": state.get("diagnostic")}
        state["phase"] = "rotation_requested" if state["rotation"]["request"] else "running"
        state["pause_reason"] = None
        state.pop("diagnostic", None)
        state.pop("observation_only_pause", None)
        state["background_tool_ids"] = []
        state["at_turn_boundary"] = False
        state.pop("resume_safe_boundary", None)
        state.pop("resume_snapshot", None)
        state.pop("tool_batch_boundary", None)
        state["observation_recoveries"] = state.get("observation_recoveries", 0) + 1
        return True

    def _user_prompt(self, event):
        with core.lock(self.lock_path, wait_seconds=5):
            state = self._state()
            name = "UserPromptSubmit"
            if event.get("session_id") != state["session_id"]:
                self._pause(state, "输入所属会话不符")
                result = self._hook_output(state, name)
                self._save(state)
                return result
            self._recover_prompt_observation(state, event)
            prompt = event.get("prompt", "")
            own_input = isinstance(prompt, str) and core.digest(prompt.strip()) == state.get("continuation_hash")
            if state.get("continuation_hash") and not own_input:
                self._confirm_continuation(state)
            state.pop("resume_safe_boundary", None)
            state.pop("resume_snapshot", None)
            state.pop("tool_batch_boundary", None)
            state["at_turn_boundary"] = False
            state["turn_generation"] = state.get("turn_generation", 0) + 1
            result = self._hook_output(state, name)
            if not own_input and state["phase"] != "paused" and prompt.strip() != "/clear":
                self._usage(state, False)
                self._automatic_rotation(state)
                switching = state["rotation"]["request"] is not None or state["phase"] in {
                    "clear_sent", "awaiting_tui_prompt", "continuation_dispatching", "awaiting_continuation"}
                if switching:
                    # continue:false 保留原生 user 记录但不调用模型；这里只记来源与哈希，不另造输入队列。
                    path = event.get("transcript_path") or state.get("source_path")
                    receipt = {"session_id": state["session_id"], "source_path": path,
                               "prompt_hash": core.digest(prompt.strip()),
                               "after_byte": Path(path).stat().st_size if path and Path(path).is_file() else 0,
                               "turn_generation": state["turn_generation"]}
                    state.setdefault("deferred_inputs", []).append(receipt)
                    state["at_turn_boundary"] = True
                    result["continue"] = False
                    result["stopReason"] = "正在切换上下文；刚提交的内容将在新窗口处理，后台任务照常运行。"
            self._save(state)
        return result

    @staticmethod
    def _deferred_input_locators(state):
        from .history import _content, _texts
        locators = []
        for item in state.get("deferred_inputs", []):
            path = item.get("source_path")
            if not path or not Path(path).is_file():
                return None
            source = _source(path, item["session_id"])
            found = next((row for row in source._records()
                          if row.start >= item["after_byte"] and row.data.get("type") == "user" and row.kind != "tool_result"
                          and core.digest("\n".join(_texts(_content(row.data))).strip()) == item["prompt_hash"]), None)
            if found is None:
                return None
            locators.append(source.locator(found.message_id))
        return locators

    def _session_start(self, event):
        """原生换窗事实始终可观测；暂停只约束后续自动输入。"""
        with core.lock(self.lock_path, wait_seconds=5):
            previous = self._state()
            state = deepcopy(previous)
            try:
                self._verify_config(state)
                sid = _uuid(event["session_id"], "native session_id")
                cwd = Path(event["cwd"]).resolve(strict=True)
                if not cwd.is_dir():
                    raise ContextRuntimeError("SessionStart 的工作目录不可用")
                path = event.get("transcript_path")
                if path is not None:
                    native_path = Path(path)
                    if (not native_path.is_absolute() or native_path.name != f"{sid}.jsonl"
                            or native_path.resolve(strict=False) != native_path):
                        raise ContextRuntimeError("SessionStart 的原生历史路径不符")
                old_sid, source = state["session_id"], event.get("source")
                duplicate = (state["phase"] != "created" and sid == old_sid
                             and source == state.get("session_start_source"))
                if duplicate and path is not None and state.get("source_path") not in {None, path}:
                    raise ContextRuntimeError("重复 SessionStart 的原生历史路径不符")
                state["cwd"] = str(cwd)
                if state["phase"] == "created" and source in {"startup", "resume"}:
                    state["durable_cron_compat"] = {
                        "enabled": _durable_cron_compat_enabled(), "scheduler_session_id": sid}
                if state["phase"] == "created" and source == "startup":
                    state["session_id"] = sid
                    state["phase"] = "running"
                    state["initial_session_started"] = True
                elif duplicate:
                    pass
                elif source == "resume":
                    self._bind_resume(state, event, sid, incoming_state=previous)
                elif source == "clear" and sid != old_sid:
                    clear = state["rotation"]["clear"]
                    expected = (clear is not None and clear["old_session_id"] == old_sid
                                and not clear["reset_seen"])
                    if expected:
                        clear.update(reset_seen=True, new_session_id=sid)
                        state["window_generation"] = clear["generation"]
                        state["phase"] = "awaiting_tui_prompt"
                        state["rotation_deadline"] = time.monotonic() + 45
                        state["native_clear_confirmations"] = state.get("native_clear_confirmations", 0) + 1
                    else:
                        # 手动 /clear 只绑定新的空上下文，不复活旧任务或旧交接。
                        state["authorization"] = None
                        state["deferred_inputs"] = []
                        state["window_generation"] = max(state["window_generation"], state["rotation"]["generation"]) + 1
                        state["rotation"] = {"generation": state["window_generation"], "request": None, "clear": None}
                        state["phase"] = "running"
                        state["manual_clear_count"] = state.get("manual_clear_count", 0) + 1
                        state.pop("continuation_hash", None)
                        state.pop("rotation_model", None)
                        state.pop("rotation_permission", None)
                    if previous["phase"] == "paused":
                        state["last_observation_pause"] = {
                            "reason": previous["pause_reason"], "diagnostic": previous.get("diagnostic")}
                    state["pause_reason"], state["diagnostic"] = None, None
                    state["session_id"], state["source_path"] = sid, None
                    state["native_context_window"] = None
                    state["budget"], state["budget_handoff_signal"] = {}, None
                    state["usage"], state["budget_stream"] = None, None
                    state.pop("output_budget", None)
                    state.pop("tool_batch_boundary", None)
                    state["at_turn_boundary"] = False
                    for key in ("resume_safe_boundary", "resume_snapshot", "stop_snapshot", "stop_text_hash"):
                        state.pop(key, None)
                else:
                    raise ContextRuntimeError("未安排的会话切换；保留状态，不自动接管")
                if path is not None:
                    state["source_path"] = path
                    self._catalogue_source(state)
                self._window(state, event)
                if event.get("model") is not None:
                    state["native_session_model"] = event["model"]
                state["session_start_source"] = source
            except (ValueError, OSError, KeyError, TypeError) as exc:
                state = previous
                self._pause(state, redact(str(exc), core.secret_values())[:400])
            result = self._hook_output(state, "SessionStart", context_prompt())
            self._save(state)
        return result

    def recover_observation(self, session_id):
        """重新观测准确的现有会话，不输入、清空或重新执行任何任务。"""
        from . import tmux_transport
        with core.lock(self.lock_path, wait_seconds=5):
            state = self._state()
            if session_id != state["session_id"] or state.get("transport") != "tmux_tui":
                raise ContextRuntimeError("恢复目标不是准确的原生会话")
            if state["phase"] != "paused":
                return self._receipt(state)
            tmux_transport.inspect(state["tmux"])
            if (state["rotation"]["clear"] or state.get("continuation_hash")
                    or state["pending_tool_ids"]):
                raise ContextRuntimeError("自动输入结果或活动尚未结算；不重放")
            source = _source(state["source_path"], session_id)
            activity = source.activity()
            if activity["pending_tools"]:
                raise ContextRuntimeError("原生来源仍有未结算的直接工具调用；不恢复自动换窗")
            self._verify_config(state)
            self._bind(state, {"session_id": session_id, "cwd": state["cwd"],
                               "transcript_path": state["source_path"]})
            self._usage(state, True)
            state["last_observation_pause"] = {"reason": state["pause_reason"],
                                               "diagnostic": state.get("diagnostic")}
            state["phase"], state["pause_reason"] = "running", None
            state["diagnostic"] = None
            state["background_tool_ids"] = []
            state["at_turn_boundary"] = False
            state.pop("resume_safe_boundary", None)
            state.pop("resume_snapshot", None)
            state["observation_recoveries"] = state.get("observation_recoveries", 0) + 1
            self._save(state)
            return self._receipt(state)

    def _boundary_flushed(self, state, source):
        if state.get("deferred_inputs"):
            return self._deferred_input_locators(state) is not None
        batch = state.get("tool_batch_boundary")
        if batch is not None:
            if (batch["session_id"] != state["session_id"]
                    or batch["turn_generation"] != state.get("turn_generation", 0)):
                state["at_turn_boundary"] = False
                return False
            return source.latest_usage()["locator"] == batch["usage_locator"]
        return self._stop_flushed(state, source)

    def _stop_flushed(self, state, source):
        from .history import _content, _texts
        if state.get("turn_generation", 0) != state.get("stop_turn_generation", 0):
            state["at_turn_boundary"] = False
            return False
        records = source._records()
        head = source.instruction_bounds()["last"]
        if head != state.get("stop_snapshot", {}).get("instruction_head"):
            info = next((row for row in records if head is not None and row.message_id == head["message_id"]), None)
            if info is None or info.data.get("type") != "attachment":
                state["at_turn_boundary"] = False
                return False
            # 尚未执行的原生排队输入留在队列中，不必为了清空队列继续消耗旧窗口。
        latest = next((row for row in reversed(records) if row.kind == "assistant"
                       and row.data.get("message", {}).get("model") != "<synthetic>"), None)
        if latest is None or not state.get("stop_text_hash"):
            return False
        last_input = max((row.start for row in records if row.data.get("type") == "user"), default=-1)
        if latest.start <= last_input:
            return False  # 相同的最终文本也可能属于上一轮，必须晚于本轮输入/工具回执。
        text = "\n".join(_texts(_content(latest.data))).strip()
        return core.digest(text) == state["stop_text_hash"]

    def _continuation(self, state):
        value = json.loads(super()._continuation(state))
        deferred = self._deferred_input_locators(state)
        if deferred is None:
            raise ContextRuntimeError("延后输入尚未写入原生历史，不按内存副本重放")
        value["deferred_inputs"] = deferred
        value["deferred_input_instruction"] = (
            "These submitted inputs were persisted by the native host before any model execution. "
            "Read and handle them in order, respecting later corrections or cancellations. Do not rerun completed tools.")
        value["active_background_agents"] = list(state["active_child_handles"])
        source_path = state["rotation"]["request"].get("source_path")
        if source_path:
            source = HistorySource(Path(source_path), state["rotation"]["request"]["session_id"])
            latest = source.instruction_bounds()["last"]
            if latest is not None:
                value["latest_instruction_locator"] = latest
            value["background_handles"] = source.activity()["background_handles"]
            recent = [row for row in source._records() if row.kind in {"assistant", "tool_result", "original_user"}][-4:]
            value["recent_history_locators"] = [source.locator(row.message_id) for row in recent]
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        core.no_secrets(value)
        if len(encoded.encode("utf-8")) > core.MAX_PACKET:
            raise ContextRuntimeError("接续记录超过有界消息大小")
        return encoded

    def _confirm_continuation(self, state):
        from .history import _content, _texts
        path = state.get("source_path")
        if not path or not Path(path).exists() or not state.get("continuation_hash"):
            return False
        source = _source(path, state["session_id"])
        observed = any(core.digest("\n".join(_texts(_content(info.data)))) == state["continuation_hash"]
                       for info in source._records() if info.data.get("type") == "user")
        if observed:
            state["continuation_observed"] = True
            state.pop("continuation_hash", None)
            state.pop("rotation_model", None)
            state.pop("rotation_permission", None)
            state["deferred_inputs"] = []
            state["phase"] = "running"
            state["at_turn_boundary"] = False
        return observed

    def advance(self):
        from . import tmux_transport
        action = None
        with core.lock(self.lock_path, wait_seconds=5):
            state = self._state()
            if state["phase"] in {"paused", "closed"} or not state.get("tmux"):
                return self._receipt(state)
            try:
                tmux_transport.inspect(state["tmux"])
                self._verify_config(state)
                control = state.get("native_control")
                if not control:
                    raise ContextRuntimeError("当前控制器未建立原生消息通道；不回退到输入框注入")
                if state["phase"] == "awaiting_continuation":
                    self._confirm_continuation(state)
                if state["phase"] in {"clear_sent", "awaiting_tui_prompt", "awaiting_continuation"}:
                    if time.monotonic() > state["rotation_deadline"]:
                        raise ContextRuntimeError("未收到准确的新会话或输入接收确认；不按延迟猜测成功")
                if state["phase"] == "awaiting_tui_prompt":
                    if native_control.ready(control) and self._deferred_input_locators(state) is not None:
                        text = _runtime_message(self._continuation(state))
                        state["continuation_hash"] = core.digest(text)
                        state["continuation_observed"] = False
                        state["phase"] = "continuation_dispatching"
                        action = ("continue", text, control)
                elif state["phase"] in {"running", "rotation_requested", "waiting_safe_boundary"}:
                    if (state.get("at_turn_boundary") or state.get("resume_safe_boundary")) and state.get("source_path"):
                        source = HistorySource(Path(state["source_path"]), state["session_id"])
                        resuming = bool(state.get("resume_safe_boundary"))
                        stable = self._resume_flushed(state, source) if resuming else self._boundary_flushed(state, source)
                        if not stable:
                            self._save(state)
                            return self._receipt(state)
                        self._usage(state, True)
                        self._automatic_rotation(state)
                        if state["rotation"]["request"]:
                            activity = source.activity()
                            if activity["pending_tools"] or state["pending_tool_ids"]:
                                state["phase"] = "waiting_safe_boundary"
                            elif native_control.ready(control):
                                self._latest_before_clear(state)
                                request = state["rotation"]["request"]
                                state["rotation"]["clear"] = {
                                    "generation": request["generation"], "old_session_id": state["session_id"],
                                    "command_id": str(uuid4()), "reset_seen": False, "new_session_id": None}
                                state["rotation_model"] = state["usage"]["actual_model"]
                                state["rotation_permission"] = state.get("native_permission_mode")
                                state["phase"] = "clear_sent"
                                state["rotation_deadline"] = time.monotonic() + 45
                                action = ("clear", None, control)
                        elif resuming:
                            state.pop("resume_safe_boundary", None)
                            state.pop("resume_snapshot", None)
                            state["at_turn_boundary"] = False
            except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                try:
                    os.kill(state["tmux"]["pane_pid"], 0)
                except ProcessLookupError:
                    state["phase"] = "closed"
                    state["native_process_exited"] = True
                else:
                    self._pause(state, redact(str(exc), core.secret_values())[:400])
            self._save(state)
        if action:
            try:
                with core.lock(self.lock_path, wait_seconds=5):
                    state = self._state()
                    expected = "clear_sent" if action[0] == "clear" else "continuation_dispatching"
                    if state["phase"] != expected:
                        return self._receipt(state)
                    tmux_transport.inspect(state["tmux"])
                    auth = getattr(self, "_control_auth", None) or os.environ.get(native_control.AUTH_ENV)
                    if action[0] == "clear":
                        native_control.send_clear(action[2], auth)
                    else:
                        native_control.send_continuation(action[2], auth, action[1])
                        state["phase"] = "awaiting_continuation"
                        state["rotation_deadline"] = time.monotonic() + 45
                        state["rotation"]["request"] = state["rotation"]["clear"] = None
                        state["at_turn_boundary"] = False
                        self._save(state)
            except Exception as exc:
                self._pause_external("原生消息投递结果未知，不重发：" + redact(str(exc), core.secret_values())[:200])
        return self.receipt()


def create(cwd, *, prompt=None, width=120, height=40, native_args=()):
    from . import tmux_transport
    _durable_cron_compat_enabled()
    native_version = native_control.require_supported_cli()
    cwd = Path(cwd).resolve(strict=True)
    sid, context_id = str(uuid4()), str(uuid4())
    runtime = TuiRuntime.create(cwd=cwd, session_id=sid, conversation_id=context_id,
                                configuration=core.configuration(cwd))
    plugin = runtime.directory / "plugin"
    _write_plugin(plugin, context_id)
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CLAUDE_CODE_SESSION_KIND", None)
    env.pop("CLAUDE_JOB_DIR", None)
    env.pop("CLAUDE_BG_SOCKET_TOKENS_PATH", None)
    env["CLAUDE_CONTINUITY_ID"] = context_id
    control = native_control.binding(context_id)
    native_control.prepare(control)
    runtime._control_auth = secrets.token_urlsafe(32)
    env.update(native_control.environment(control, runtime._control_auth))
    # 原生参数保持原样；可重复的 plugin-dir 不覆盖 settings、prompt 或 session-id。
    argv = ["claude", "--plugin-dir", str(plugin), *native_args]
    if prompt is not None:
        argv.append(prompt)
    socket = Path(core.HOME) / "runtime" / "tmux" / f"{context_id}.sock"
    binding = tmux_transport.create(socket, cwd, argv, env, width=width, height=height)
    with core.lock(runtime.lock_path, wait_seconds=5):
        state = runtime._state()
        state["transport"] = "tmux_tui"
        state["tmux"] = binding
        state["native_control"] = control
        state["native_cli_version"] = native_version
        state["owned_pid"] = binding["pane_pid"]
        runtime._save(state)
    return runtime


def serve(context_id):
    runtime = TuiRuntime.load(context_id)
    with core.lock(runtime.directory / "controller.lock", wait_seconds=5):
        while True:
            outcome = runtime.advance()
            if outcome["phase"] == "closed":
                return outcome
            if outcome["phase"] == "paused":
                try:
                    os.kill(outcome["owned_pid"], 0)
                except ProcessLookupError:
                    with core.lock(runtime.lock_path, wait_seconds=5):
                        state = runtime._state()
                        state["phase"] = "closed"
                        state["native_process_exited"] = True
                        runtime._save(state)
                    return runtime.receipt()
            time.sleep(0.25)


def run(cwd, *, prompt=None, detached=False, native_args=()):
    from . import tmux_transport
    if not detached and not sys.stdin.isatty():
        raise ContextRuntimeError("原生 TUI 需从终端启动；隔离验证可显式使用 --detached")
    size = os.get_terminal_size() if sys.stdin.isatty() else os.terminal_size((120, 40))
    runtime = create(cwd, prompt=prompt, width=size.columns, height=size.lines, native_args=native_args)
    controller_env = {**os.environ, native_control.AUTH_ENV: runtime._control_auth,
                      "CLAUDE_CONTINUITY_ID": runtime.conversation_id}
    process = subprocess.Popen(core.module_argv("tui-serve", "--context-id", runtime.conversation_id),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True, env=controller_env)
    with core.lock(runtime.lock_path, wait_seconds=5):
        state = runtime._state()
        state["controller_pid"] = process.pid
        runtime._save(state)
    if not detached:
        subprocess.run(tmux_transport.attach_argv(state["tmux"]), check=False)
    return {"status": "started", "context_id": runtime.conversation_id,
            "interface": "native_claude_tui_in_private_tmux", "state_path": str(runtime.state_path),
            "attach_command": shlex.join(core.module_argv(
                "tui-attach", "--context-id", runtime.conversation_id))}


def recover(context_id, session_id):
    """为已退出的附加控制器恢复监测；原生终端和任务保持不动。"""
    from . import tmux_transport
    runtime = TuiRuntime.load(context_id)
    with core.lock(runtime.directory / "controller-start.lock"), core.lock(runtime.directory / "controller.lock"):
        with core.lock(runtime.lock_path, wait_seconds=5):
            state = runtime._state()
            control = state.get("native_control")
            if (not control or os.environ.get("CLAUDE_CONTINUITY_ID") != context_id
                    or os.environ.get(native_control.SOCKET_ENV) != control["socket_path"]):
                raise ContextRuntimeError("恢复需要对应受管终端的原生通道环境；不从其他会话接管")
            control_env = native_control.environment(control, os.environ.get(native_control.AUTH_ENV))
        runtime.recover_observation(session_id)
        with core.lock(runtime.lock_path, wait_seconds=5):
            state = runtime._state()
            if state["phase"] != "running":
                raise ContextRuntimeError("现有自动操作未结算；不启动第二个控制器")
            tmux_transport.inspect(state["tmux"])
            state["at_turn_boundary"] = False
            process = subprocess.Popen(core.module_argv("tui-serve", "--context-id", context_id),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True, env={**os.environ, **control_env})
            state["controller_pid"] = process.pid
            runtime._save(state)
    return runtime.receipt()


def attach(context_id):
    from . import tmux_transport
    runtime = TuiRuntime.load(context_id)
    with core.lock(runtime.lock_path, wait_seconds=5):
        state = runtime._state()
    tmux_transport.inspect(state["tmux"])
    return {"returncode": subprocess.run(tmux_transport.attach_argv(state["tmux"]), check=False).returncode}
