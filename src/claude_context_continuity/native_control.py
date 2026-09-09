"""通过原生后台会话的本地消息通道投递换窗，不读写输入框。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess

from . import core

AUTH_ENV = "CLAUDE_BG_RV_AUTH"
SOCKET_ENV = "CLAUDE_BG_RENDEZVOUS_SOCK"
PROTOCOL = "claude-background-rendezvous.v1"
_MAX_PACKET_BYTES = 2 * core.MAX_PACKET


class NativeControlError(core.ContinuityError):
    pass


def require_supported_cli() -> str:
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NativeControlError("无法核实 Claude Code 版本；本版本需要 2.1.266 或更新版本") from exc
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    if result.returncode != 0 or match is None or tuple(map(int, match.groups())) < (2, 1, 266):
        raise NativeControlError("当前 Claude Code 不支持所需原生批次 hook 与消息通道；需要 2.1.266 或更新版本")
    return match.group(0)


def binding(context_id: str) -> dict[str, str]:
    context_id = core.uuid(context_id)
    path = core.safe_path(core.HOME, f"runtime/control/{context_id}.sock", exists=False)
    if len(os.fsencode(path)) > 103:
        raise NativeControlError("原生控制 socket 路径超过 macOS 限制")
    return {"protocol": PROTOCOL, "context_id": context_id, "socket_path": str(path)}


def prepare(control: dict[str, str]) -> None:
    path = _path(control)
    if os.path.lexists(path):
        raise NativeControlError("原生控制 socket 已存在，不接管其他进程")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def environment(control: dict[str, str], auth: str) -> dict[str, str]:
    path = _path(control)
    _auth(auth)
    return {
        "CLAUDE_BG_BACKEND": "daemon",
        SOCKET_ENV: str(path),
        AUTH_ENV: auth,
    }


def _path(control: dict[str, str]) -> Path:
    if not isinstance(control, dict) or set(control) != {"protocol", "context_id", "socket_path"}:
        raise NativeControlError("原生控制绑定不完整")
    if control["protocol"] != PROTOCOL:
        raise NativeControlError("原生控制协议不符")
    context_id = core.uuid(control["context_id"])
    expected = core.safe_path(core.HOME, f"runtime/control/{context_id}.sock", exists=False)
    if str(expected) != control["socket_path"]:
        raise NativeControlError("原生控制 socket 不属于当前上下文")
    return expected


def _auth(auth: str) -> None:
    if not isinstance(auth, str) or not 32 <= len(auth) <= 256 or any(c.isspace() for c in auth):
        raise NativeControlError("原生控制凭证不可用；请从对应受管终端恢复，不重建或落盘凭证")


def ready(control: dict[str, str]) -> bool:
    path = _path(control)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise NativeControlError("原生控制 socket 类型或所有者不符")
    return True


def _send(control: dict[str, str], auth: str, text: str) -> None:
    _auth(auth)
    if not ready(control):
        raise NativeControlError("原生控制通道尚未就绪")
    path = _path(control)
    packet = "\n".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in (
        {"role": "attacher", "auth": auth}, {"type": "reply", "text": text})) + "\n"
    encoded = packet.encode("utf-8")
    if len(encoded) > _MAX_PACKET_BYTES:
        raise NativeControlError("原生控制消息超过有界交接大小")
    # sendall 只证明写入本地通道；真正成功仍由 SessionStart/原生历史确认。
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(path))
            client.sendall(encoded)
    except (OSError, ValueError) as exc:
        raise NativeControlError("原生消息投递结果未知，不自动重发") from exc


def send_clear(control: dict[str, str], auth: str) -> None:
    _send(control, auth, "/clear")


def send_continuation(control: dict[str, str], auth: str, text: str) -> None:
    from .context_runtime import RUNTIME_SIGNAL
    if not isinstance(text, str) or not text.startswith(RUNTIME_SIGNAL + "\n"):
        raise NativeControlError("自动接续必须标记为宿主信号，不能伪装成新用户指令")
    core.no_secrets(text)
    _send(control, auth, text)
