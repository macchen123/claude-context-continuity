"""跨项目全局交接入口，不导入任何项目包。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import core


def parser():
    p = argparse.ArgumentParser(description="Claude Code 全局无 compact 交接")
    sub = p.add_subparsers(dest="action", required=True)
    history = sub.add_parser("history", help="准确 ID 定位或有界分页")
    history.add_argument("--source", required=True)
    history.add_argument("--session-id", required=True)
    selection = history.add_mutually_exclusive_group()
    selection.add_argument("--message-id")
    selection.add_argument("--tool-use-id")
    selection.add_argument("--search", help="仅检索显式来源中的脱敏公开文本")
    history.add_argument("--scan-limit", type=int, default=200)
    history.add_argument("--instructions", action="store_true", help="只列举真实用户指令的定位")
    history.add_argument("--offset", type=int, default=0)
    history.add_argument("--start", type=int, default=0)
    history.add_argument("--limit", type=int, default=2000)
    history.add_argument("--page-size", type=int, default=10)
    history.add_argument("--expected-sha256", help="读回搜索命中时核对原始记录哈希")
    search = sub.add_parser("history-search", help="跨窗口 FTS5 搜索或有界浏览，任何回合均可使用")
    scope = search.add_mutually_exclusive_group()
    scope.add_argument("--context-id")
    scope.add_argument("--source", help="显式选择单份 JSONL，不扫描其他会话")
    search.add_argument("--session-id", help="显式来源文件对应的 session ID")
    search.add_argument("--query", help="字面子串；省略时浏览可见历史")
    search.add_argument("--window", type=int, action="append", default=[], help="只返回指定 generation，可重复")
    search.add_argument("--session", action="append", default=[], help="只返回指定 session，可重复")
    kinds = search.add_mutually_exclusive_group()
    kinds.add_argument("--source-kind", action="append", default=[],
                       choices=("original_user", "verified_user_answer", "assistant", "tool_result", "summary"))
    kinds.add_argument("--role", action="append", default=[], choices=("user", "assistant", "tool", "summary"))
    search.add_argument("--tool", help="按真实工具名过滤，不搜索工具调用输入")
    search.add_argument("--recent-first", action="store_true")
    search.add_argument("--cursor", help="沿用相同范围、查询、过滤和顺序的下一页游标")
    search.add_argument("--page-size", type=int, default=10)
    windows = sub.add_parser("history-windows", help="列举当前连续会话的历史窗口")
    windows.add_argument("--context-id", default=os.environ.get("CLAUDE_CONTINUITY_ID"))
    windows.add_argument("--offset", type=int, default=0)
    windows.add_argument("--limit", type=int, default=10)
    notes = sub.add_parser("notes", help="当前连续会话的笔记，不需要任务登记或执行合同")
    notes.add_argument("operation", choices=("list", "read", "search", "write", "append"))
    notes.add_argument("name", nargs="?")
    notes.add_argument("--context-id", default=os.environ.get("CLAUDE_CONTINUITY_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID"))
    notes.add_argument("--query")
    notes.add_argument("--expected-sha256")
    notes.add_argument("--revision")
    notes.add_argument("--offset", type=int, default=0)
    notes.add_argument("--start", type=int, default=0)
    notes.add_argument("--limit", type=int, default=2000)
    notes.add_argument("--page-size", type=int, default=10)
    notes.add_argument("--scan-limit", type=int, default=64)
    native = sub.add_parser("context-run", help="正常工具与权限下运行任务，统一自动管理上下文")
    native.add_argument("--cwd", default=os.getcwd())
    native.add_argument("--detached", action="store_true", help="仅隔离验证或稍后 attach，不替代前台 TUI")
    native.add_argument("native_args", nargs=argparse.REMAINDER)
    for action in ("tui-hook", "tui-serve", "tui-attach", "tui-status", "tui-recover"):
        command = sub.add_parser(action, help=argparse.SUPPRESS if action in {"tui-hook", "tui-serve"} else "原生 TUI 会话操作")
        command.add_argument("--context-id", required=True)
        if action == "tui-recover":
            command.add_argument("--session-id", required=True)
    native_input = native.add_mutually_exclusive_group()
    native_input.add_argument("--prompt")
    native_input.add_argument("--prompt-file")
    request = sub.add_parser("context-request", help="模型主动请求新上下文，不创建新业务任务")
    request.add_argument("--context-id", default=os.environ.get("CLAUDE_CONTINUITY_ID"))
    request.add_argument("--handoff", required=True)
    request.add_argument("--notes", default="")
    return p


def dispatch_notes(args):
    from .notes import MAX_NOTE_BYTES, NotesStore

    context_id = core.uuid(args.context_id)
    directory = core.safe_path(core.HOME, f"runtime/contexts/{context_id}", exists=False)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = NotesStore(directory)
    secrets = core.secret_values()
    if args.operation == "list":
        result = store.list(args.offset, args.page_size)
    elif args.operation == "search":
        result = store.search(args.query, args.offset, args.page_size,
                              scan_limit=args.scan_limit, secrets=secrets)
    elif args.operation == "read":
        result = store.read(args.name, args.start, args.limit, args.revision, secrets=secrets)
    else:
        raw = sys.stdin.buffer.read(MAX_NOTE_BYTES + 1)
        if len(raw) > MAX_NOTE_BYTES:
            raise core.ContinuityError("笔记输入超过单次大小上限，不截断写入")
        operation = store.append if args.operation == "append" else store.write
        result = operation(args.name, raw.decode("utf-8"), args.expected_sha256, secrets=secrets)
    return {"context_id": context_id, **result}


def context_windows(context_id, offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise core.ContinuityError("窗口分页需要非负 offset 和 1–20 的 limit")
    context_id = core.uuid(context_id)
    directory = core.safe_path(core.HOME, f"runtime/contexts/{context_id}/history", exists=False)
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    entries = []
    for path in paths[offset:offset + limit]:
        binding = core.read_json(core.safe_path(core.HOME, path))
        if (set(binding) != {"session_id", "source_path", "generation"}
                or type(binding["generation"]) is not int or binding["generation"] < 0
                or path.stem != f"{binding['generation']:08d}-{core.uuid(binding['session_id'])}"):
            raise core.ContinuityError("上下文历史目录的来源绑定不符")
        entries.append(binding)
    return {"context_id": context_id, "coverage": "native_context_history_sources", "entries": entries,
            "next_offset": offset + limit if offset + limit < len(paths) else None, "write_authority": False}


def dispatch_history_search(args):
    from .history import HistorySource
    from .history_index import HistoryIndex

    if args.source is not None:
        if not args.source or not args.session_id:
            raise core.ContinuityError("--source 必须为有效路径并同时指定 --session-id")
        source = HistorySource(Path(args.source), args.session_id)
        sources = [{"source_path": str(source.path), "session_id": source.session_id, "generation": 0}]
        scope_id = core.digest(sources)
        relative = f"runtime/history-index/{scope_id}/history.sqlite3"
        scope = {"coverage": "explicit_history_source"}
    else:
        if args.session_id:
            raise core.ContinuityError("--session-id 仅配合 --source；过滤窗口请使用 --session")
        context_id = args.context_id if args.context_id is not None else os.environ.get("CLAUDE_CONTINUITY_ID")
        if not context_id:
            raise core.ContinuityError("请指定 --context-id，或显式提供 --source 和 --session-id")
        context_id = core.uuid(context_id)
        sources, offset = [], 0
        while True:
            page = context_windows(context_id, offset, 20)
            sources.extend(page["entries"])
            offset = page["next_offset"]
            if offset is None:
                break
        relative = f"runtime/contexts/{context_id}/.cache/history.sqlite3"
        scope = {"context_id": context_id, "coverage": "native_context_history_sources"}
    role_kinds = {"user": ("original_user", "verified_user_answer"), "assistant": ("assistant",),
                  "tool": ("tool_result",), "summary": ("summary",)}
    selected_kinds = tuple(args.source_kind) or tuple(kind for role in args.role for kind in role_kinds[role])
    path = core.safe_path(core.HOME, relative, exists=False)
    with HistoryIndex(path) as index:
        result = index.search(sources, args.query, source_kinds=selected_kinds, tool=args.tool,
                              windows=tuple(args.window), sessions=tuple(args.session),
                              recent_first=args.recent_first, limit=args.page_size,
                              cursor=args.cursor, secrets=core.secret_values())
    return {**scope, **result, "write_authority": False}


def dispatch(args):
    if args.action.startswith("tui-"):
        from . import tui_runtime
        if args.action == "tui-hook":
            if os.environ.get("CLAUDE_CONTINUITY_ID") != args.context_id:
                raise core.ContinuityError("TUI hook 不属于当前自有进程")
            return tui_runtime.TuiRuntime.load(args.context_id).on_hook(json.load(sys.stdin))
        if args.action == "tui-serve":
            return tui_runtime.serve(args.context_id)
        if args.action == "tui-attach":
            return tui_runtime.attach(args.context_id)
        if args.action == "tui-recover":
            return tui_runtime.recover(args.context_id, args.session_id)
        return tui_runtime.TuiRuntime.load(args.context_id).receipt()
    if args.action == "context-run":
        from . import tui_runtime
        prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
        native_args = args.native_args[1:] if args.native_args[:1] == ["--"] else args.native_args
        return tui_runtime.run(args.cwd, prompt=prompt, detached=args.detached, native_args=native_args)
    if args.action == "context-request":
        from .context_runtime import context_request
        return context_request(args.context_id, args.handoff, args.notes)
    if args.action == "notes":
        return dispatch_notes(args)
    if args.action == "history-windows":
        return context_windows(args.context_id, args.offset, args.limit)
    if args.action == "history-search":
        return dispatch_history_search(args)
    if args.action == "history":
        from .history import HistorySource
        src = HistorySource(Path(args.source), args.session_id)
        if args.expected_sha256 is not None and not (args.message_id or args.tool_use_id):
            raise core.ContinuityError("--expected-sha256 必须配合准确消息或工具调用 ID")
        if args.search is not None:
            return src.search(args.search, offset=args.offset, limit=args.page_size,
                              scan_limit=args.scan_limit, secrets=core.secret_values())
        if args.message_id or args.tool_use_id:
            loc = src.locator(args.message_id) if args.message_id else src.tool_result(args.tool_use_id)
            if args.expected_sha256 is not None and args.expected_sha256 != loc["sha256"]:
                raise core.ContinuityError("原始记录与搜索命中的 SHA256 不一致，请重新搜索核对")
            return src.read(loc, start=args.start, limit=args.limit, secrets=core.secret_values())
        return src.page(offset=args.offset, limit=args.page_size, secrets=core.secret_values(),
                        instructions_only=args.instructions)
    raise core.ContinuityError("未知命令")


def main():
    args = parser().parse_args()
    try:
        result = dispatch(args)
        core.no_secrets(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get("status") == "paused" else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        if args.action == "tui-hook":
            # 连续性观测失败不能变成原生工具的权限拒绝。
            try:
                from .context_runtime import ContextRuntime
                if os.environ.get("CLAUDE_CONTINUITY_ID") == args.context_id:
                    ContextRuntime.load(args.context_id)._pause_external("hook_observation_failed")
            except (ValueError, OSError, KeyError, TypeError):
                pass
            print("{}")
            return 0
        from .history import redact
        print(json.dumps({"status": "paused", "error": redact(str(exc), core.secret_values())[:600]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
