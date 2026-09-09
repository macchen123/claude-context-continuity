"""原生工具结果的有界存档与替换。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable

from . import core
from .history import HistoryError, redact


_ARCHIVE_DIRECTORY = "outputs"
_BASH = "Bash"
_READ = "Read"
_AGENT = "Agent"


class ResultBudgetError(ValueError):
    """结果不能安全归档或替换。"""


def bound_result(
    tool_name: str,
    tool_response: Any,
    *,
    directory: Path,
    byte_limit: int,
) -> dict[str, Any]:
    """返回原结果或可忠实表达的有界替换结果。

    ``raw_bytes`` 与 ``model_bytes`` 是紧凑 UTF-8 JSON 的字节数，
    不是 token 估计，也不代表完整模型 payload 的上界。仅当超限结果
    能保持原生结构并附带私有 archive 定位时才写入磁盘。
    """
    raw = _compact_json_bytes(tool_response)
    raw_bytes = None if raw is None else len(raw)
    if not _positive_integer(byte_limit):
        return _deferred(tool_response, raw_bytes, "byte_limit must be a positive non-boolean integer")
    if raw is None:
        return _deferred(tool_response, None, "tool response is not finite UTF-8 JSON")

    # 小结果不必由本模块理解：保留原生 rich/unknown 输出，交给宿主。
    if raw_bytes <= byte_limit:
        return _decision(
            tool_response,
            replaced=False,
            raw_bytes=raw_bytes,
            model_bytes=raw_bytes,
            reference=None,
            defer_required=False,
        )

    kind = _supported_kind(tool_name, tool_response)
    if kind is None:
        return _deferred(tool_response, raw_bytes, "unsupported oversized native tool result")

    try:
        redacted_response = _redacted_copy(tool_response)
        archive_bytes = _archive_json_bytes(redacted_response)
        digest = hashlib.sha256(archive_bytes).hexdigest()
        root, outputs, archive_path = _archive_plan(directory, digest)
        reference = {
            "path": str(archive_path),
            "sha256": digest,
            "bytes": len(archive_bytes),
            "redacted": True,
        }
        replacement = _replacement(kind, tool_response, redacted_response, reference, byte_limit)
        if replacement is None:
            return _deferred(
                tool_response,
                raw_bytes,
                "result exceeds the budget but no faithful schema-plus-locator replacement fits",
            )
        model = _compact_json_bytes(replacement)
        if model is None or len(model) > byte_limit or not _replacement_valid(kind, replacement):
            return _deferred(tool_response, raw_bytes, "result has no faithful bounded replacement")
        _store_archive(root, outputs, archive_path, digest, archive_bytes, redacted_response)
    except (HistoryError, OSError, TypeError, ValueError, UnicodeError, core.ContinuityError):
        # 异常文本可能含路径或凭证；调用方只需要安全的停止信号。
        return _deferred(tool_response, raw_bytes, "private result archive is unavailable")

    return _decision(
        replacement,
        replaced=True,
        raw_bytes=raw_bytes,
        model_bytes=len(model),
        reference=reference,
        defer_required=False,
    )


def _decision(
    response: Any,
    *,
    replaced: bool,
    raw_bytes: int | None,
    model_bytes: int | None,
    reference: dict[str, Any] | None,
    defer_required: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "response": response,
        "replaced": replaced,
        "raw_bytes": raw_bytes,
        "model_bytes": model_bytes,
        "reference": reference,
        "defer_required": defer_required,
    }
    if reason is not None:
        result["reason"] = reason
    return result


def _deferred(response: Any, raw_bytes: int | None, reason: str) -> dict[str, Any]:
    return _decision(
        response,
        replaced=False,
        raw_bytes=raw_bytes,
        model_bytes=raw_bytes,
        reference=None,
        defer_required=True,
        reason=reason,
    )


def _positive_integer(value: Any) -> bool:
    return type(value) is int and value > 0


def _compact_json_bytes(value: Any) -> bytes | None:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None


def _archive_json_bytes(value: Any) -> bytes:
    """生成与 ``core.atomic`` 相同格式的 JSON 字节。"""
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ResultBudgetError("archive value cannot be encoded") from exc


def _redacted_copy(value: Any) -> Any:
    """递归复制待归档 JSON，并在字符串层面脱敏。"""
    secrets = core.secret_values()

    def copy(item: Any) -> Any:
        if type(item) is str:
            return redact(item, secrets)
        if type(item) is list:
            return [copy(child) for child in item]
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ResultBudgetError("archive object key is not a string")
            # 协议键不改写；若键本身有凭证，no_secrets 会拒绝落盘。
            return {key: copy(child) for key, child in item.items()}
        if item is None or type(item) in {bool, int, float}:
            return item
        raise ResultBudgetError("archive value is not JSON")

    redacted = copy(value)
    core.no_secrets(redacted)
    return redacted


def _supported_kind(tool_name: Any, response: Any) -> str | None:
    """只识别本模块可以仅改文本字段的原生结果。"""
    if type(tool_name) is not str or type(response) is not dict:
        return None
    if tool_name == _BASH:
        if isinstance(response.get("stdout"), str) and isinstance(response.get("stderr"), str):
            # 图像 stdout 必须原样交给原生宿主，不伪装为文本已审阅。
            return None if response.get("isImage") is True else _BASH
        return None
    if tool_name == _READ:
        file = response.get("file")
        if response.get("type") == "text" and type(file) is dict and isinstance(file.get("content"), str):
            return _READ
        return None
    if tool_name == _AGENT:
        content = response.get("content")
        if response.get("status") != "completed" or type(content) is not list:
            return None
        if all(type(block) is dict and block.get("type") == "text" and isinstance(block.get("text"), str) for block in content):
            return _AGENT
    return None


def _replacement(
    kind: str,
    original: dict[str, Any],
    redacted: dict[str, Any],
    reference: dict[str, Any],
    byte_limit: int,
) -> dict[str, Any] | None:
    if kind == _BASH:
        return _bounded_bash(original, redacted, reference, byte_limit)
    if kind == _READ:
        return _bounded_text_read(original, redacted, byte_limit)
    if kind == _AGENT:
        return _bounded_completed_agent(original, redacted, reference, byte_limit)
    return None


def _replacement_valid(kind: str, value: dict[str, Any]) -> bool:
    if _supported_kind(kind, value) != kind:
        return False
    return kind != _READ or value["file"].get("truncatedByTokenCap") is True


def _fit_prefixes(
    build: Callable[[int], dict[str, Any]],
    max_characters: int,
    byte_limit: int,
) -> dict[str, Any] | None:
    """二分选取前缀，按 Python 字符切分而不会断开 UTF-8 码点。"""
    if max_characters < 0:
        return None
    baseline = build(0)
    encoded = _compact_json_bytes(baseline)
    if encoded is None or len(encoded) > byte_limit:
        return None
    best = baseline
    low, high = 0, max_characters
    while low < high:
        middle = (low + high + 1) // 2
        candidate = build(middle)
        encoded = _compact_json_bytes(candidate)
        if encoded is not None and len(encoded) <= byte_limit:
            low, best = middle, candidate
        else:
            high = middle - 1
    return best


def _bounded_bash(
    original: dict[str, Any],
    redacted: dict[str, Any],
    reference: dict[str, Any],
    byte_limit: int,
) -> dict[str, Any] | None:
    path, digest = reference["path"], reference["sha256"]
    stdout_notice = (
        "\n[cclaude bounded preview; redacted full result: "
        f"{path}; sha256={digest}; do not rerun this command solely to recover output.]"
    )
    stderr_notice = (
        "\n[cclaude bounded stderr preview; redacted full result: "
        f"{path}; sha256={digest}; do not rerun this command solely to recover output.]"
    )
    stdout, stderr = redacted["stdout"], redacted["stderr"]

    def build(limit: int) -> dict[str, Any]:
        result = dict(original)
        # 原生 persistedOutputPath 等元数据只保留，不复制进提示文本。
        result["stdout"] = stdout[:limit] + stdout_notice
        result["stderr"] = stderr[:limit] + stderr_notice
        return result

    return _fit_prefixes(build, max(len(stdout), len(stderr)), byte_limit)


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _bounded_text_read(
    original: dict[str, Any],
    redacted: dict[str, Any],
    byte_limit: int,
) -> dict[str, Any] | None:
    content = redacted["file"]["content"]
    if not content:
        return None

    def build(limit: int) -> dict[str, Any]:
        preview = content[:limit]
        file = dict(original["file"])
        file["content"] = preview
        file["numLines"] = _line_count(preview)
        # 标记局部内容，避免把预览伪装成完整文件读取。
        file["truncatedByTokenCap"] = True
        result = dict(original)
        result["file"] = file
        return result

    return _fit_prefixes(build, len(content) - 1, byte_limit)


def _bounded_completed_agent(
    original: dict[str, Any],
    redacted: dict[str, Any],
    reference: dict[str, Any],
    byte_limit: int,
) -> dict[str, Any] | None:
    original_blocks = original["content"]
    redacted_blocks = redacted["content"]
    if not original_blocks:
        return None
    path, digest = reference["path"], reference["sha256"]
    notice = (
        "\n[cclaude bounded preview; redacted completed-agent result: "
        f"{path}; sha256={digest}; do not rerun the completed agent solely to recover output.]"
    )

    def build(limit: int) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        for index, (source, archived) in enumerate(zip(original_blocks, redacted_blocks)):
            block = dict(source)
            text = archived["text"][:limit]
            if index == len(original_blocks) - 1:
                text += notice
            block["text"] = text
            blocks.append(block)
        result = dict(original)
        result["content"] = blocks
        return result

    return _fit_prefixes(build, max(len(block["text"]) for block in redacted_blocks), byte_limit)


def _root_directory(directory: Path) -> Path:
    try:
        root = Path(directory)
    except TypeError as exc:
        raise ResultBudgetError("private directory is invalid") from exc
    if not root.is_absolute():
        raise ResultBudgetError("private directory must be absolute")
    try:
        root_stat = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ResultBudgetError("private directory is unavailable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode) or resolved != root:
        raise ResultBudgetError("private directory has a symlink or alias")
    return root


def _archive_plan(directory: Path, digest: str) -> tuple[Path, Path, Path]:
    root = _root_directory(directory)
    outputs = core.safe_path(root, _ARCHIVE_DIRECTORY, exists=False)
    output_stat = _lstat(outputs)
    if output_stat is not None and (
        stat.S_ISLNK(output_stat.st_mode) or not stat.S_ISDIR(output_stat.st_mode)
    ):
        raise ResultBudgetError("archive directory is not a regular directory")
    archive_path = core.safe_path(root, Path(_ARCHIVE_DIRECTORY) / f"{digest}.json", exists=False)
    return root, outputs, archive_path


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _ensure_archive_directory(root: Path, outputs: Path) -> Path:
    try:
        outputs.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ResultBudgetError("archive directory cannot be created") from exc
    try:
        verified = core.safe_path(root, _ARCHIVE_DIRECTORY, exists=True)
        output_stat = outputs.lstat()
    except (OSError, core.ContinuityError) as exc:
        raise ResultBudgetError("archive directory cannot be verified") from exc
    if verified != outputs or stat.S_ISLNK(output_stat.st_mode) or not stat.S_ISDIR(output_stat.st_mode):
        raise ResultBudgetError("archive directory drifted")
    return verified


def _store_archive(
    root: Path,
    outputs: Path,
    archive_path: Path,
    digest: str,
    archive_bytes: bytes,
    archive_value: dict[str, Any],
) -> None:
    outputs = _ensure_archive_directory(root, outputs)
    planned = core.safe_path(root, Path(_ARCHIVE_DIRECTORY) / archive_path.name, exists=False)
    if planned != archive_path or planned.parent != outputs:
        raise ResultBudgetError("archive path drifted")
    if _lstat(archive_path) is not None:
        _verify_archive(root, archive_path, digest, archive_bytes)
        return
    try:
        if len(archive_bytes) <= core.MAX_PACKET:
            core.atomic(archive_path, archive_value, exclusive=True)
        else:
            _atomic_large_archive(archive_path, archive_bytes)
    except FileExistsError:
        # 并发创建后只接受内容完全一致的既有文件。
        pass
    _verify_archive(root, archive_path, digest, archive_bytes)


def _atomic_large_archive(path: Path, payload: bytes) -> None:
    """保留大结果，沿用共享原子写的临时文件和 fsync 语义。"""
    if path.is_symlink():
        raise ResultBudgetError("archive path is a symlink")
    fd, temporary_name = tempfile.mkstemp(prefix=".result-budget-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_archive(root: Path, path: Path, digest: str, expected: bytes) -> None:
    try:
        verified = core.safe_path(root, path, exists=True)
        file_stat = path.lstat()
    except (OSError, core.ContinuityError) as exc:
        raise ResultBudgetError("archive file cannot be verified") from exc
    if verified != path or stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ResultBudgetError("archive file is not a regular bound file")
    try:
        observed = core.sha(path)
        exact = path.read_bytes()
    except OSError as exc:
        raise ResultBudgetError("archive file cannot be read") from exc
    if not hmac.compare_digest(observed, digest) or exact != expected:
        raise ResultBudgetError("archive content binding differs")
