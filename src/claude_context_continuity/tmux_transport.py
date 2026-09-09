"""每个 conversation 独享的最小 tmux TUI transport。

本模块只启动、核验和向私有 tmux socket 发送原生命令；不持久化环境、
不接管默认 tmux server，也不负责终止任何终端或服务。
"""
from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from pathlib import Path
from typing import Any

from . import core


TMUX = "tmux"
SESSION_NAME = "continuity"
_CREATE_FORMAT = "#{pane_id}\t#{pane_pid}\t#{pid}\t#{session_name}\t#{pane_current_command}"
_INSPECT_FORMAT = ("#{pane_id}\t#{pane_pid}\t#{pid}\t#{session_name}\t#{pane_dead}\t"
                   "#{cursor_x}\t#{cursor_y}\t#{pane_width}\t#{pane_height}\t#{pane_current_command}")
_BINDING_FIELDS = frozenset({"socket_path", "pane_id", "pane_pid", "session_name", "server_pid"})
_INSPECTION_FIELDS = _BINDING_FIELDS | frozenset({
    "cursor_x", "cursor_y", "pane_width", "pane_height", "pane_current_command",
})
_MAX_MACOS_SOCKET_PATH_BYTES = 103


class TmuxTransportError(core.ContinuityError):
    """私有 tmux transport 的身份或传输状态不可安全确认。"""


def _fail(message: str) -> None:
    raise TmuxTransportError(message)


def _absolute_path(value: str | os.PathLike[str], label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        _fail(f"{label} 必须是绝对路径")
    try:
        path = Path(value)
    except (TypeError, ValueError):
        _fail(f"{label} 必须是绝对路径")
    if not path.is_absolute():
        _fail(f"{label} 必须是绝对路径")
    return Path(os.path.abspath(path))


def _assert_no_symlink(root: Path, path: Path) -> None:
    """拒绝 runtime 根目录以下的 symlink，避免 socket 身份被重定向。"""
    try:
        relative = path.relative_to(root)
    except ValueError:
        _fail("tmux socket 不属于 continuity runtime")
    current = root
    for part in (".", *relative.parts):
        if part != ".":
            current /= part
        try:
            if os.path.lexists(current) and current.is_symlink():
                _fail("tmux socket 路径包含 symlink")
        except OSError:
            _fail("tmux socket 路径无法核验")


def _context_socket_path(value: str | os.PathLike[str]) -> Path:
    """接受准确的 conversation 专用 socket，且永不回退到 /tmp 或默认 server。"""
    socket_path = _absolute_path(value, "tmux socket")
    home = _absolute_path(core.HOME, "continuity HOME")
    runtime = home / "runtime"
    try:
        relative = socket_path.relative_to(runtime)
    except ValueError:
        _fail("tmux socket 不属于 continuity runtime")

    # 标准上下文目录保留给较短 HOME 路径；macOS 下可改用 runtime/tmux/<uuid>.sock。
    parts = relative.parts
    conversation_id: str | None = None
    if len(parts) == 4 and parts[0] == "contexts" and parts[2] == "tui" and parts[3]:
        conversation_id = parts[1]
    elif len(parts) == 2 and parts[0] == "tmux" and parts[1].endswith(".sock"):
        conversation_id = parts[1][:-len(".sock")]
    if conversation_id is None:
        _fail("tmux socket 必须是准确的 conversation 专用 runtime 路径")
    try:
        core.uuid(conversation_id)
    except (TypeError, ValueError, core.ContinuityError):
        _fail("tmux socket 的 conversation ID 无效")
    if len(os.fsencode(str(socket_path))) > _MAX_MACOS_SOCKET_PATH_BYTES:
        _fail("tmux socket 路径超过 macOS AF_UNIX 103-byte 限制")
    _assert_no_symlink(home, socket_path)
    return socket_path


def _working_directory(value: str | os.PathLike[str]) -> Path:
    path = _absolute_path(value, "cwd")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        _fail("cwd 不存在或无法核验")
    if not resolved.is_dir():
        _fail("cwd 必须是现有目录")
    return resolved


def _positive_dimension(value: int, label: str) -> int:
    if type(value) is not int or value <= 0:
        _fail(f"{label} 必须是正整数")
    return value


def _native_argv(value: list[str]) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        _fail("原生 claude argv 必须是非空字符串列表")
    if Path(value[0]).name != "claude":
        _fail("transport 只启动原生 claude TUI")
    return list(value)


def _environment(value: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict):
        _fail("环境必须是字符串键值字典")
    result: dict[str, str] = {}
    for key, item in value.items():
        if (not isinstance(key, str) or not key or "=" in key or "\x00" in key
                or not isinstance(item, str) or "\x00" in item):
            _fail("环境必须是字符串键值字典")
        result[key] = item
    # 仅返回内存副本，绝不写入 runtime、tmux environment 或日志。
    return result


def _socket_exists(path: Path) -> bool:
    try:
        return os.path.lexists(path)
    except OSError:
        _fail("tmux socket 状态无法核验")


def _prepare_socket_directory(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        _fail("tmux socket 目录无法创建")
    _assert_no_symlink(_absolute_path(core.HOME, "continuity HOME"), path)
    try:
        if not path.parent.is_dir():
            _fail("tmux socket 目录不可用")
    except OSError:
        _fail("tmux socket 目录无法核验")


def _require_private_socket(path: Path) -> None:
    _assert_no_symlink(_absolute_path(core.HOME, "continuity HOME"), path)
    try:
        mode = path.lstat().st_mode
    except OSError:
        _fail("私有 tmux socket 不存在")
    if not stat.S_ISSOCK(mode):
        _fail("私有 tmux socket 身份无效")


def _pane_id(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"%[0-9]+", value) is None:
        _fail("tmux pane ID 无效")
    return value


def _pid(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        _fail(f"{label} 无效")
    return value


def _coordinate(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        _fail(f"{label} 无效")
    return value


def _binding(value: dict[str, Any], *, require_socket: bool) -> dict[str, Any]:
    # inspect 的返回可直接传回其他 API；额外观测字段不参与 pane 身份绑定。
    if not isinstance(value, dict) or set(value) not in {_BINDING_FIELDS, _INSPECTION_FIELDS}:
        _fail("tmux binding 字段不完整或包含未知字段")
    socket_path = _context_socket_path(value["socket_path"])
    result = {
        "socket_path": str(socket_path),
        "pane_id": _pane_id(value["pane_id"]),
        "pane_pid": _pid(value["pane_pid"], "tmux pane PID"),
        "session_name": value["session_name"],
        "server_pid": _pid(value["server_pid"], "tmux server PID"),
    }
    if result["session_name"] != SESSION_NAME:
        _fail("tmux session 不是 continuity")
    if require_socket:
        _require_private_socket(socket_path)
    return result


def _run(command: list[str], *, cwd: Path | None = None,
         environment: dict[str, str] | None = None) -> str:
    """运行一次 tmux client；任何失败都不重试，也不暴露 stderr。"""
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "strict",
        "check": False,
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if environment is not None:
        kwargs["env"] = environment
    try:
        completed = subprocess.run(command, **kwargs)
    except (OSError, subprocess.SubprocessError):
        _fail("tmux client 无法执行")
    if getattr(completed, "returncode", 1) != 0:
        _fail("tmux client 命令失败")
    output = getattr(completed, "stdout", "")
    if output is None:
        return ""
    if not isinstance(output, str):
        _fail("tmux client 返回格式无效")
    return output


def _tmux(socket_path: Path, *arguments: str) -> list[str]:
    """每个 tmux client 都显式连接私有 socket，绝不回退默认 server。"""
    return [TMUX, "-S", str(socket_path), *arguments]


def _one_record(output: str, fields: int) -> list[str]:
    lines = output.splitlines()
    if len(lines) != 1:
        _fail("tmux identity 输出不是唯一记录")
    values = lines[0].split("\t")
    if len(values) != fields:
        _fail("tmux identity 输出格式无效")
    return values


def _creation_binding(socket_path: Path, output: str) -> dict[str, Any]:
    pane_id, pane_pid, server_pid, session_name, current_command = _one_record(output, 5)
    if session_name != SESSION_NAME or not current_command:
        _fail("新 tmux pane 身份未确认")
    try:
        return {
            "socket_path": str(socket_path),
            "pane_id": _pane_id(pane_id),
            "pane_pid": _pid(int(pane_pid), "tmux pane PID"),
            "session_name": session_name,
            "server_pid": _pid(int(server_pid), "tmux server PID"),
        }
    except (TypeError, ValueError, TmuxTransportError):
        _fail("新 tmux pane 身份未确认")


def create(socket_path: Path, cwd: Path, argv: list[str], environment: dict[str, str],
           width: int = 120, height: int = 40) -> dict[str, Any]:
    """在准确 conversation 目录启动一个 detached、私有的原生 claude TUI。"""
    socket = _context_socket_path(socket_path)
    directory = _working_directory(cwd)
    command_argv = _native_argv(argv)
    process_environment = _environment(environment)
    width, height = _positive_dimension(width, "width"), _positive_dimension(height, "height")
    if _socket_exists(socket):
        _fail("私有 tmux socket 已存在，拒绝接管")
    _prepare_socket_directory(socket)
    if _socket_exists(socket):
        _fail("私有 tmux socket 已存在，拒绝接管")

    # tmux 只收到一个 shell-command；默认 shell 负责 exec，避免额外 shell 层残留。
    shell_command = shlex.join(["exec", *command_argv])
    _run(_tmux(socket, "new-session", "-d", "-s", SESSION_NAME,
               "-x", str(width), "-y", str(height), "-c", str(directory), shell_command),
         cwd=directory, environment=process_environment)
    identity = _run(_tmux(socket, "display-message", "-p", "-t", f"{SESSION_NAME}:0.0", _CREATE_FORMAT),
                    cwd=directory, environment=process_environment)
    return _creation_binding(socket, identity)


def inspect(binding: dict[str, Any]) -> dict[str, Any]:
    """核验身份并返回光标/尺寸观测，绝不读取 pane 内容或终端 history。"""
    expected = _binding(binding, require_socket=True)
    socket = Path(expected["socket_path"])
    output = _run(_tmux(socket, "list-panes", "-s", "-t", SESSION_NAME, "-F", _INSPECT_FORMAT))
    (pane_id, pane_pid, server_pid, session_name, pane_dead,
     cursor_x, cursor_y, pane_width, pane_height, current_command) = _one_record(output, 10)
    try:
        actual = {
            "pane_id": _pane_id(pane_id),
            "pane_pid": _pid(int(pane_pid), "tmux pane PID"),
            "server_pid": _pid(int(server_pid), "tmux server PID"),
            "session_name": session_name,
            "cursor_x": _coordinate(int(cursor_x), "tmux cursor_x"),
            "cursor_y": _coordinate(int(cursor_y), "tmux cursor_y"),
            "pane_width": _pid(int(pane_width), "tmux pane_width"),
            "pane_height": _pid(int(pane_height), "tmux pane_height"),
            "pane_current_command": current_command,
        }
    except (TypeError, ValueError, TmuxTransportError):
        _fail("tmux pane 身份输出无效")
    if (actual["session_name"] != SESSION_NAME or pane_dead != "0"
            or not actual["pane_current_command"]
            or actual["cursor_x"] >= actual["pane_width"]
            or actual["cursor_y"] >= actual["pane_height"]):
        _fail("tmux pane 已结束、尺寸无效或 session 身份不符")
    for key in ("pane_id", "pane_pid", "server_pid", "session_name"):
        if actual[key] != expected[key]:
            _fail("tmux binding 与当前 pane 身份不符")
    return {**expected, **{key: actual[key] for key in (
        "cursor_x", "cursor_y", "pane_width", "pane_height", "pane_current_command",
    )}}


def attach_argv(binding: dict[str, Any]) -> list[str]:
    """返回供用户终端执行的 attach 参数；不执行、不接管现有 session。"""
    expected = _binding(binding, require_socket=False)
    return _tmux(Path(expected["socket_path"]), "attach-session", "-t", SESSION_NAME)


def capture(binding: dict[str, Any]) -> str:
    """只读捕获已核验 pane 的当前屏幕，不访问 tmux history 或其他 pane。"""
    expected = inspect(binding)
    return _run(_tmux(Path(expected["socket_path"]), "capture-pane", "-p", "-t", expected["pane_id"]))
