"""原生交互 TUI 的薄控制层；仅操作自己创建的独立 tmux server。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from uuid import uuid4

from . import core, __version__
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
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop",
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

1. 遵守当前项目指令和 shell 前缀约束。停止派发新工作，不杀任务；已有工具或后台任务未完成时，说明正在等待什么，等待真实终态后再交接。
2. 保存简短交接：当前目标、限制、已完成和未完成事项、准确产物/历史来源，以及下一步。必要细节使用现有 Notes；不复制整段 transcript、隐藏思考或凭证。
3. 按本项目命令约束调用现有唯一请求入口，替换下面的交接占位文字并正确引用 shell 参数：

```sh
{request} --handoff '简短且真实的目标、进展、约束、来源和下一步'
```

4. 查看返回的 phase/pause_reason。rotation_requested 或 waiting_safe_boundary 只表示请求/等待，不能宣称换窗已成功；若已存在请求，不重复提交，可用下面的只读命令核对：

```sh
{status}
```

5. 请求被接受后结束当前回合，让原生 Stop 与现有控制器在空输入和活动结算后的安全边界切换。若暂停则说明实际原因，不清状态、不盲目重试、不要求用户为已完成动作重新授权。

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
            "durable_cron_compat")}

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
                or state["pending_tool_ids"] or state["active_child_handles"]
                or state.get("background_tool_ids")):
            raise ContextRuntimeError("仍有未结活动；保留原生恢复，仅暂停自动换窗")

    def _bind_resume(self, state, event, sid):
        """绑定原生已选中的停止来源，并在首次模型请求前取得真实用量。"""
        self._resume_eligible(state, sid)
        source = _source(event.get("transcript_path"), sid)
        activity, latest = source.activity(), source.latest_usage()
        if latest["cwd"] != state["cwd"]:
            raise ContextRuntimeError("恢复来源的实际模型用量工作目录不符")
        if activity["pending_tools"] or activity["background_handles"]:
            raise ContextRuntimeError("恢复来源仍有未结算工具或后台句柄，不自动接管")
        candidate = dict(state)
        candidate["phase"], candidate["pause_reason"] = "running", None
        candidate["rotation"] = {**state["rotation"], "request": None, "clear": None}
        candidate["session_id"], candidate["source_path"], candidate["authorization"] = sid, None, None
        candidate["native_context_window"] = None
        candidate["usage"], candidate["budget_stream"], candidate["budget_handoff_signal"] = None, None, None
        candidate["budget"] = {}
        candidate.pop("rotation_model", None)
        candidate.pop("rotation_permission", None)
        for key in ("stop_observed_at", "stop_text_hash", "stop_serial", "stop_turn_generation",
                    "stop_snapshot", "settlement_stop_serial", "resume_safe_boundary", "resume_snapshot"):
            candidate.pop(key, None)
        source = self._bind(candidate, event)
        self._window(candidate, event)
        observed = self._usage(candidate, True)
        if observed is None:
            raise ContextRuntimeError("恢复来源没有可用的实际原生用量")
        usage, _ = observed
        candidate["native_session_model"] = usage["actual_model"]
        candidate["resume_safe_boundary"] = True
        candidate["resume_snapshot"] = {
            "instruction_head": candidate["authorization"]["latest_instruction_locator"],
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
            with core.lock(self.lock_path, wait_seconds=5):
                state = self._state()
                if event.get("session_id") != state["session_id"]:
                    self._pause(state, "输入所属会话不符")
                elif state["phase"] in {"clear_sent", "awaiting_tui_prompt", "continuation_dispatching"} and event.get("prompt", "").strip() != "/clear":
                    self._pause(state, "自动换窗期间收到用户输入；不再自动注入")
                state.pop("resume_safe_boundary", None)
                state.pop("resume_snapshot", None)
                state["at_turn_boundary"] = False
                state["turn_generation"] = state.get("turn_generation", 0) + 1
                self._save(state)
                if state["phase"] == "paused":
                    return {}
            return {}
        if name != "SessionStart":
            if name in {"SubagentStart", "SubagentStop"}:
                with core.lock(self.lock_path, wait_seconds=5):
                    state = self._state()
                    state["at_turn_boundary"] = False
                    self._save(state)
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
                if name == "Stop" and state["phase"] != "paused" and state.get("source_path"):
                    source = HistorySource(Path(state["source_path"]), state["session_id"])
                    final_text = event.get("last_assistant_message")
                    state["stop_text_hash"] = core.digest(final_text.strip()) if isinstance(final_text, str) else None
                    state["at_turn_boundary"] = True
                    state["stop_serial"] = state.get("stop_serial", 0) + 1
                    state["stop_turn_generation"] = state.get("turn_generation", 0)
                    state["stop_snapshot"] = {"instruction_head": source.instruction_bounds()["last"]}
                    self._save(state)
                if state["phase"] != "paused" and state.get("continuation_hash") and state.get("source_path"):
                    source = HistorySource(Path(state["source_path"]), state["session_id"])
                    from .history import _content, _texts
                    observed = any(core.digest("\n".join(_texts(_content(info.data)))) == state["continuation_hash"]
                                   for info in source._records() if info.data.get("type") == "user")
                    if observed:
                        state["continuation_observed"] = True
                        state.pop("continuation_hash", None)
                        state.pop("rotation_model", None)
                        state.pop("rotation_permission", None)
                        state["phase"] = "running"
                        self._save(state)
                result = self._repair_durable_cron(state, event, result)
            return result
        with core.lock(self.lock_path, wait_seconds=5):
            state = self._state()
            try:
                self._verify_config(state)
                sid = _uuid(event["session_id"], "native session_id")
                state["cwd"] = str(Path(event["cwd"]).resolve())
                if state["phase"] == "created" and event.get("source") in {"startup", "resume"}:
                    state["durable_cron_compat"] = {
                        "enabled": _durable_cron_compat_enabled(), "scheduler_session_id": sid}
                if state["phase"] == "created" and event.get("source") == "startup":
                    state["session_id"] = sid
                    state["phase"] = "running"
                    state["initial_session_started"] = True
                elif event.get("source") == "resume":
                    self._bind_resume(state, event, sid)
                elif state["phase"] == "clear_sent" and event.get("source") == "clear":
                    clear = state["rotation"]["clear"]
                    if sid == clear["old_session_id"]:
                        raise ContextRuntimeError("原生 clear 没有产生新 session ID")
                    clear.update(reset_seen=True, new_session_id=sid)
                    state["session_id"], state["source_path"] = sid, None
                    state["window_generation"] = state["rotation"]["generation"]
                    state["budget"], state["budget_handoff_signal"] = {}, None
                    state["usage"], state["budget_stream"] = None, None
                    state.pop("resume_safe_boundary", None)
                    state.pop("resume_snapshot", None)
                    state["phase"] = "awaiting_tui_prompt"
                    state["native_clear_confirmations"] = state.get("native_clear_confirmations", 0) + 1
                elif (state["phase"] == "running" and event.get("source") == "clear"
                      and sid != state["session_id"] and not state["rotation"]["request"]
                      and not state["pending_tool_ids"] and not state["active_child_handles"]):
                    # 用户主动 /clear 保持原生语义：新任务从空上下文开始，不注入旧交接。
                    state["session_id"], state["source_path"], state["authorization"] = sid, None, None
                    state["window_generation"] += 1
                    state["rotation"]["generation"] = state["window_generation"]
                    state["budget"], state["usage"], state["budget_handoff_signal"] = {}, None, None
                    state.pop("resume_safe_boundary", None)
                    state.pop("resume_snapshot", None)
                    state["at_turn_boundary"] = False
                    state["manual_clear_count"] = state.get("manual_clear_count", 0) + 1
                else:
                    raise ContextRuntimeError("未安排的会话切换；保留状态，不自动接管")
                path = event.get("transcript_path")
                if path is not None:
                    native_path = Path(path)
                    if (not native_path.is_absolute() or native_path.name != f"{sid}.jsonl"
                            or native_path.resolve(strict=False) != native_path):
                        raise ContextRuntimeError("SessionStart 的原生历史路径不符")
                    state["source_path"] = str(native_path)
                model = event.get("model")
                if model is not None:
                    state["native_session_model"] = model
                state["session_start_source"] = event.get("source")
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self._pause(state, redact(str(exc), core.secret_values())[:400])
            self._save(state)
            if state["phase"] == "paused":
                return {}
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context_prompt()}}

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
                    or state["pending_tool_ids"] or state["active_child_handles"]):
                raise ContextRuntimeError("自动输入结果或活动尚未结算；不重放")
            source = _source(state["source_path"], session_id)
            activity = source.activity()
            if activity["pending_tools"] or activity["background_handles"]:
                raise ContextRuntimeError("原生来源仍有未结活动；不恢复自动换窗")
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

    @staticmethod
    def _empty_prompt(binding, screen):
        """只认光标所在行的空原生输入提示符；不清除或覆盖用户输入。"""
        lines = screen.splitlines()
        y, x = binding.get("cursor_y"), binding.get("cursor_x")
        if type(y) is not int or not 0 <= y < len(lines) or type(x) is not int:
            return False
        line = lines[y]
        stripped = line.strip()
        return stripped in {"❯", ">"} and x <= len(line.rstrip()) + 2

    def _stop_flushed(self, state, source):
        from .history import _content, _texts
        if state.get("turn_generation", 0) != state.get("stop_turn_generation", 0):
            state["at_turn_boundary"] = False
            return False
        records = source._records()
        head = source.instruction_bounds()["last"]
        if head != state.get("stop_snapshot", {}).get("instruction_head"):
            state["at_turn_boundary"] = False
            return False
        if head is not None:
            info = next((row for row in records if row.message_id == head["message_id"]), None)
            if info is not None and info.data.get("type") == "attachment":
                return False  # 排队的人类输入尚未转成实际 user 记录，不能抢先 clear。
        latest = next((row for row in reversed(records) if row.kind == "assistant"
                       and row.data.get("message", {}).get("model") != "<synthetic>"), None)
        if latest is None or not state.get("stop_text_hash"):
            return False
        last_input = max((row.start for row in records if row.data.get("type") == "user"
                          or (row.data.get("type") == "attachment"
                              and row.data.get("attachment", {}).get("type") == "queued_command")), default=-1)
        if latest.start <= last_input:
            return False  # 相同的最终文本也可能属于上一轮，必须晚于本轮输入/工具回执。
        text = "\n".join(_texts(_content(latest.data))).strip()
        return core.digest(text) == state["stop_text_hash"]

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
                binding = tmux_transport.inspect(state["tmux"])
                self._verify_config(state)
                if state["phase"] == "awaiting_continuation":
                    self._confirm_continuation(state)
                if state["phase"] in {"clear_sent", "awaiting_tui_prompt", "awaiting_continuation"}:
                    if time.monotonic() > state["rotation_deadline"]:
                        raise ContextRuntimeError("未收到准确的新会话或输入接收确认；不按延迟猜测成功")
                if state["phase"] == "awaiting_tui_prompt":
                    if self._empty_prompt(binding, tmux_transport.capture(state["tmux"])):
                        text = _runtime_message(self._continuation(state))
                        state["continuation_hash"] = core.digest(text)
                        state["continuation_observed"] = False
                        state["phase"] = "continuation_dispatching"
                        action = ("continue", text, state["tmux"])
                elif state["phase"] in {"running", "rotation_requested", "waiting_safe_boundary"}:
                    if (state.get("at_turn_boundary") or state.get("resume_safe_boundary")) and state.get("source_path"):
                        source = HistorySource(Path(state["source_path"]), state["session_id"])
                        resuming = bool(state.get("resume_safe_boundary"))
                        if resuming:
                            if not self._resume_flushed(state, source):
                                self._save(state)
                                return self._receipt(state)
                        elif not self._stop_flushed(state, source):
                            self._save(state)
                            return self._receipt(state)
                        self._usage(state, True)
                        self._automatic_rotation(state)
                        if state["rotation"]["request"]:
                            source = HistorySource(Path(state["source_path"]), state["session_id"])
                            activity = source.activity()
                            if (activity["pending_tools"] or activity["background_handles"]
                                    or state["pending_tool_ids"] or state["active_child_handles"]):
                                state["phase"] = "waiting_safe_boundary"
                                state["settlement_stop_serial"] = state.get("stop_serial", 0)
                            elif not resuming and state.get("stop_serial", 0) <= state.get("settlement_stop_serial", -1):
                                state["phase"] = "waiting_safe_boundary"
                            elif self._empty_prompt(binding, tmux_transport.capture(state["tmux"])):
                                # 不通过休眠猜测清空结果。后续必须收到同一原生进程的 SessionStart(clear)。
                                self._latest_before_clear(state)
                                request = state["rotation"]["request"]
                                state["rotation"]["clear"] = {
                                    "generation": request["generation"], "old_session_id": state["session_id"],
                                    "command_id": str(uuid4()), "reset_seen": False, "new_session_id": None}
                                state["rotation_model"] = state["usage"]["actual_model"]
                                state["rotation_permission"] = state.get("native_permission_mode")
                                state["phase"] = "clear_sent"
                                state["rotation_deadline"] = time.monotonic() + 45
                                action = ("clear", None, state["tmux"])
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
                    binding = tmux_transport.inspect(action[2])
                    if (state["pending_tool_ids"] or state["active_child_handles"]
                            or not self._empty_prompt(binding, tmux_transport.capture(action[2]))):
                        self._pause(state, "自动输入前出现活动或用户正在编辑；不发送")
                        self._save(state)
                        return self._receipt(state)
                    if action[0] == "clear":
                        source = HistorySource(Path(state["source_path"]), state["session_id"])
                        stable = (self._resume_flushed(state, source) if state.get("resume_safe_boundary")
                                  else state.get("at_turn_boundary") and self._stop_flushed(state, source))
                        if not stable:
                            self._pause(state, "自动清空前来源已变化；不发送")
                            self._save(state)
                            return self._receipt(state)
                        tmux_transport.send_clear(action[2])
                    else:
                        tmux_transport.send_text(action[2], action[1])
                        state["phase"] = "awaiting_continuation"
                        state["rotation_deadline"] = time.monotonic() + 45
                        state["rotation"]["request"] = state["rotation"]["clear"] = None
                        state["at_turn_boundary"] = False
                        self._save(state)
            except Exception as exc:
                self._pause_external("终端输入结果未知，不重发：" + redact(str(exc), core.secret_values())[:200])
        return self.receipt()


def create(cwd, *, prompt=None, width=120, height=40, native_args=()):
    from . import tmux_transport
    _durable_cron_compat_enabled()
    cwd = Path(cwd).resolve(strict=True)
    sid, context_id = str(uuid4()), str(uuid4())
    runtime = TuiRuntime.create(cwd=cwd, session_id=sid, conversation_id=context_id,
                                configuration=core.configuration(cwd))
    plugin = runtime.directory / "plugin"
    _write_plugin(plugin, context_id)
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["CLAUDE_CONTINUITY_ID"] = context_id
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
    process = subprocess.Popen(core.module_argv("tui-serve", "--context-id", runtime.conversation_id),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
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
        runtime.recover_observation(session_id)
        with core.lock(runtime.lock_path, wait_seconds=5):
            state = runtime._state()
            if state["phase"] != "running":
                raise ContextRuntimeError("现有自动操作未结算；不启动第二个控制器")
            tmux_transport.inspect(state["tmux"])
            state["at_turn_boundary"] = False
            process = subprocess.Popen(core.module_argv("tui-serve", "--context-id", context_id),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
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
