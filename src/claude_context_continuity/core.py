"""上下文工具共用的持久化、路径和预算读取；不执行或审批用户任务。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

STATE_DIRECTORY_ENV = "CLAUDE_CONTEXT_CONTINUITY_DIR"
STATE_DIRECTORY_NAME = "session-continuity"
PACKAGE_MODULE = "claude_context_continuity"
MAX_PACKET = 64 * 1024


def config_directory() -> Path:
    """Return the effective Claude configuration directory without creating it."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    candidate = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return candidate.resolve(strict=False)


def state_directory() -> Path:
    """Return this package's state root; no migration or I/O occurs here."""
    override = os.environ.get(STATE_DIRECTORY_ENV)
    candidate = Path(override).expanduser() if override else config_directory() / STATE_DIRECTORY_NAME
    return candidate.resolve(strict=False)


def module_argv(*arguments: str) -> list[str]:
    """Build a portable module invocation for generated hooks and controllers."""
    return [sys.executable, "-B", "-m", PACKAGE_MODULE, *arguments]


# Kept as a patchable module attribute for callers that isolate runtime state in tests.
HOME = state_directory()


class ContinuityError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def sha(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(64 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ContinuityError("必须使用完整、准确的 UUID")
    return value


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_PACKET:
        raise ContinuityError("交接 JSON 超过 64 KiB；请改用准确引用")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic(path, value, *, exclusive=False):
    path = Path(path)
    if path.is_symlink():
        raise ContinuityError("运营文件不得是 symlink")
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(raw.encode()) > MAX_PACKET:
        raise ContinuityError("运营文件超过 64 KiB")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".continuity-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            os.link(name, path)
        else:
            os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def lock(path, *, wait_seconds=0):
    """Acquire an operations lock; default callers remain strictly nonblocking."""
    import fcntl
    path = Path(path)
    if path.is_symlink():
        raise ContinuityError("锁文件不得是 symlink")
    if not isinstance(wait_seconds, (int, float)) or isinstance(wait_seconds, bool) or wait_seconds < 0:
        raise ContinuityError("锁等待时间必须是非负秒数")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + wait_seconds
    with path.open("a+") as f:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if not wait_seconds or time.monotonic() >= deadline:
                    raise ContinuityError("已有执行者；不争抢、不自动重试") from exc
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def safe_path(root, value, *, exists=True):
    root = Path(root).resolve(strict=exists)
    candidate = Path(value).expanduser()
    candidate = candidate if candidate.is_absolute() else root / candidate
    lexical = Path(os.path.abspath(candidate))
    if lexical.is_relative_to(root):
        current = root
        for part in lexical.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                raise ContinuityError("绑定路径包含 symlink；请显式使用准确的真实路径")
    path = candidate.resolve(strict=exists)
    if not path.is_relative_to(root):
        raise ContinuityError("文件越出准确项目根目录")
    return path


def secret_values():
    # 只在内存中用于拒绝意外落盘，不保存环境或凭证。
    return tuple(v for k, v in os.environ.items() if v and (k == "CLAUDE_BG_RV_AUTH" or re.search(
        r"(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|SECRET|PRIVATE_KEY)$", k)))


def no_secrets(value):
    from .history import redact
    text = json.dumps(value, ensure_ascii=False)
    if redact(text, secret_values()) != text:
        raise ContinuityError("交接含敏感信息，拒绝落盘；请移除凭证")


def configuration(root, *, live_window=False):
    """只读取预算及压缩偏好；模型、权限和配置解释仍由原生 CLI 负责。"""
    home = config_directory()
    files = [home / "settings.json", Path(root) / ".claude/settings.json",
             Path(root) / ".claude/settings.local.json"]
    effective = {}
    auto_compact = None
    for path in files:
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                continue
            if isinstance(data.get("env"), dict):
                effective.update(data["env"])
            auto_compact = data.get("autoCompactEnabled", auto_compact)
        except (OSError, ValueError):
            continue  # 原生宿主自行处理配置错误，预算观察不成为启动门槛。
    names = ("DISABLE_COMPACT", "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_MAX_CONTEXT_TOKENS")
    selected = {key: os.environ.get(key, effective.get(key)) for key in names}
    if live_window:
        for key in names[1:]:
            if key in effective:
                selected[key] = effective[key]
    result = {"env": selected, "no_compact_configured": selected["DISABLE_COMPACT"] == "1" and auto_compact is False}
    no_secrets(result)
    return {**result, "hash": digest(result)}


def configured_window(config):
    """Return the effective configured host window without guessing a default."""
    try:
        value = config["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"]
    except (KeyError, TypeError):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None
