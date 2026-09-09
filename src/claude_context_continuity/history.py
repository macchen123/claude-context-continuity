"""Read-only, bounded access to one explicitly selected session JSONL file."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping
import uuid


class HistoryError(ValueError):
    """The explicit history source or requested projection is invalid."""


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_CHARS, _MAX_RECORDS = 8_000, 20
_MAX_UPDATE_RECORDS, _MAX_UPDATE_BYTES = 32, 64 * 1024
_META = frozenset({"system", "file-history-snapshot", "file-history-delta", "queue-operation", "mode", "permission-mode", "atis-latch", "attachment", "ai-title", "last-prompt", "custom-title", "progress", "agent-progress", "session-start", "session-end", "context", "compaction", "hook", "notification", "meta"})
_STATE_ONLY = frozenset({"file-history-snapshot", "file-history-delta", "queue-operation", "mode", "permission-mode", "atis-latch", "attachment", "ai-title", "last-prompt", "cost-state"})
_META = _META | _STATE_ONLY
_RUNTIME = ("<system-reminder>", "<task-notification>", "[request interrupted", "<local-command-",
            "<cross-session-message", "<teammate-message", "[cross-session idle notice]",
            "<continuity-host-event>")
_PATH_FIELDS = ("saved_output", "saved_output_path", "persisted_output", "persisted_output_path", "output_path", "output_file", "file_path", "path")
_ASSIGNMENT = re.compile(r"(?ix)(?P<key>\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth(?:orization)?|password|passwd|secret|client[_-]?secret|private[_-]?key|token)\b)(?P<sep>\s*(?:=|:)\s*)(?P<value>(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|(?:bearer\s+)?[^\s,;]+))")
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----", re.DOTALL)


@dataclass(frozen=True)
class _Record:
    data: dict[str, Any]
    message_id: str
    start: int
    end: int
    sha256: str
    kind: str


@dataclass(frozen=True)
class _Pair:
    ask: _Record
    selections: tuple[tuple[str, str], ...]


def _decode(raw: bytes, start: int) -> dict[str, Any]:
    if not raw.endswith(b"\n"):
        raise HistoryError(f"truncated JSONL record at byte {start}")
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HistoryError(f"invalid JSONL record at byte {start}") from exc
    if not isinstance(value, dict):
        raise HistoryError(f"JSONL record at byte {start} is not an object")
    return value


def _message_id(record: Mapping[str, Any]) -> str:
    value = record.get("uuid")
    if isinstance(value, str) and value:
        return value
    raise HistoryError("content record has no valid uuid")


def _content(record: Mapping[str, Any]) -> Any:
    message = record.get("message")
    if not isinstance(message, dict) or "content" not in message:
        raise HistoryError("record has no usable message content")
    return message["content"]


def _blocks(value: Any) -> list[Any]:
    return [value] if isinstance(value, dict) else value if isinstance(value, list) else []


def _results(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    blocks = [record] if record.get("type") == "tool_result" else [b for b in _blocks(_content(record)) if isinstance(b, dict) and b.get("type") == "tool_result"]
    for block in blocks:
        if not isinstance(block.get("tool_use_id"), str) or not block["tool_use_id"]:
            raise HistoryError("tool_result has no valid tool_use_id")
    return blocks


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [b["text"] for b in _blocks(value) if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]


def _plain_user_text(record: Mapping[str, Any]) -> str | None:
    value = _content(record)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value["text"] if value.get("type") == "text" and isinstance(value.get("text"), str) else None
    if not isinstance(value, list):
        raise HistoryError("user content has an unknown shape")
    if any(not isinstance(block, dict) for block in value):
        raise HistoryError("user content has an unknown block")
    return "\n".join(block["text"] for block in value) if value and all(block.get("type") == "text" and isinstance(block.get("text"), str) for block in value) else None


def _flag(record: Mapping[str, Any], name: str) -> bool:
    value = record.get(name, False)
    if type(value) is not bool:
        raise HistoryError(f"record has non-boolean {name}")
    return value


def _queued_user_text(record: Mapping[str, Any]) -> str | None:
    attachment = record.get("attachment")
    if (record.get("type") != "attachment" or not isinstance(attachment, dict)
            or attachment.get("type") != "queued_command"
            or attachment.get("origin") != {"kind": "human"}
            or attachment.get("commandMode") != "prompt"
            or record.get("userType") != "external"):
        return None
    if not isinstance(attachment.get("source_uuid"), str) or not _UUID.fullmatch(attachment["source_uuid"]):
        raise HistoryError("queued human instruction lacks its exact source_uuid")
    prompt = attachment.get("prompt")
    if isinstance(prompt, str):
        return prompt
    if (not isinstance(prompt, list) or not prompt or any(
            not isinstance(block, dict) or block.get("type") not in {"text", "image"}
            or (block.get("type") == "text" and not isinstance(block.get("text"), str)) for block in prompt)):
        raise HistoryError("queued human instruction has an unknown prompt shape")
    text = "\n".join(_texts(prompt))
    if any(block.get("type") == "image" for block in prompt):
        text += "\n[图像未包含在文本投影中；涉及图像含义时须通过准确来源核对，不能猜测。]"
    return text


def source_kind(record: Mapping[str, Any]) -> str:
    """Classify a record without promoting tool prose to user authorization."""
    if not isinstance(record, Mapping) or not isinstance(record.get("type"), str) or not record["type"]:
        raise HistoryError("record has no valid type")
    record_type = record["type"]
    if record_type not in _META | {"user", "assistant", "summary", "tool_result"}:
        raise HistoryError(f"unknown record type {record_type!r}")
    if record_type in {"user", "assistant"}:
        _content(record)
        if record["message"].get("role", record_type) != record_type:
            raise HistoryError("message role does not match its record type")
    if _flag(record, "isCompactSummary"):
        return "summary"
    if record.get("sourceToolAssistantUUID") and record_type == "user" and not _results(record):
        return "meta"
    if _flag(record, "isSidechain"):
        return "sidechain"
    if record_type == "summary":
        return "summary"
    if not _flag(record, "isMeta") and _queued_user_text(record) is not None:
        return "original_user"
    if record_type in _META:
        return "meta"
    if record_type == "tool_result":
        _results(record)
        return "tool_result"
    if record_type == "assistant":
        return "meta" if _flag(record, "isMeta") else "assistant"
    if _results(record):  # tool_result wins even when quoted text is also present
        return "tool_result"
    if _flag(record, "isMeta") or str(record.get("userType", "")).lower() in {"internal", "system", "meta"}:
        return "meta"
    text = _plain_user_text(record)
    if not text or not text.strip():
        return "meta"
    stripped, lowered = text.lstrip(), text.lstrip().lower()
    # 原生 prompt 型 slash 调用是用户任务入口；只绑定调用名和实参，
    # 不把随后 isMeta 的 skill 展开或本地命令 stdout 当成用户授权。
    invocation = re.fullmatch(
        r"<command-message>([\w:./-]+)</command-message>\s*"
        r"<command-name>/\1</command-name>\s*"
        r"<command-args>([\s\S]*)</command-args>\s*", stripped)
    if invocation is not None:
        return "original_user"
    if lowered.startswith(("<local-command-", "<command-name>", "<command-message>", "<command-args>")):
        return "local_command"
    return "meta" if any(lowered.startswith(prefix) for prefix in _RUNTIME) else "original_user"


def terminal_task_result(tool_name: Any, tool_input: Any, response: Any) -> str | None:
    """只识别原生任务工具的结构化终态，不从输出正文猜测任务已结束。"""
    if not isinstance(tool_input, dict) or not isinstance(response, dict):
        return None
    task_id = tool_input.get("task_id")
    if (not isinstance(task_id, str) or not task_id or response.get("is_error")
            or response.get("error") or response.get("success") is False):
        return None
    terminal = {"completed", "failed", "stopped", "cancelled", "killed"}
    if tool_name == "TaskStop":
        # 成功 TaskStop 的原生回执包含 task_id/task_type；不能把相似正文当回执。
        if (response.get("task_id") == task_id
                and isinstance(response.get("task_type"), str) and response["task_type"]
                and response.get("status", "stopped") in terminal):
            return task_id
    elif tool_name == "TaskOutput" and response.get("retrieval_status") == "success":
        task = response.get("task")
        if isinstance(task, dict) and task.get("task_id") == task_id and task.get("status") in terminal:
            return task_id
    return None


def _task_notice(record: Mapping[str, Any]) -> tuple[str, str] | None:
    """只认原生 task-notification 来源，不采信用户粘贴的同名标签。"""
    if record.get("isSidechain"):
        return None
    if record.get("type") == "user" and record.get("origin") == {"kind": "task-notification"} and record.get("promptSource") == "system":
        content = record.get("message", {}).get("content", "")
    else:
        attachment = record.get("attachment", {})
        if (record.get("type") != "attachment" or not isinstance(attachment, dict)
                or attachment.get("type") != "queued_command"):
            return None
        native_origin = attachment.get("origin") == {"kind": "task-notification"}
        native_mode = attachment.get("commandMode") == "task-notification" and "origin" not in attachment
        if not (native_origin or native_mode):
            return None
        content = attachment.get("prompt", "")
    text = "\n".join(_texts(content)).partition("<task-notification>")[2]
    header = text.split("<summary>", 1)[0].split("<result>", 1)[0]
    task = re.search(r"<task-id>([^<>\r\n]{1,200})</task-id>", header)
    status = re.search(r"<status>(completed|failed|stopped|cancelled|killed)</status>", header)
    return (task.group(1), status.group(1)) if task and status else None


def redact(text: str, secrets: Any = ()) -> str:
    """Conservatively redact supplied known secrets and credential assignments."""
    if not isinstance(text, str):
        raise HistoryError("text to redact must be a string")
    if secrets is None:
        values: tuple[str, ...] = ()
    elif isinstance(secrets, str):
        values = (secrets,)
    else:
        try:
            values = tuple(secrets)
        except TypeError as exc:
            raise HistoryError("secrets must be an iterable of strings") from exc
    if any(not isinstance(value, str) for value in values):
        raise HistoryError("secrets must be an iterable of strings")
    for value in sorted({value for value in values if value}, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _ASSIGNMENT.sub(lambda match: f"{match.group('key')}{match.group('sep')}[REDACTED]", text)
    return _BEARER.sub("Bearer [REDACTED]", text)


def _bounded(text: str, start: int, limit: int) -> tuple[str, int | None, int]:
    if type(start) is not int or start < 0:
        raise HistoryError("start must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= _MAX_CHARS:
        raise HistoryError(f"limit must be between 1 and {_MAX_CHARS}")
    total, end = len(text), min(len(text), start + limit)
    return ("", None, total) if start >= total else (text[start:end], end if end < total else None, total)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HistoryError("record contains a non-JSON pairing value") from exc


def _answer(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, list):
        return ", ".join(_answer(item) for item in value)
    if isinstance(value, dict):
        for key in ("answer", "selected", "value", "label"):
            if key in value:
                return _answer(value[key])
    raise HistoryError("AskUserQuestion answer has an unsupported shape")


def _selections(questions: list[Any], answers: Any) -> tuple[tuple[str, str], ...]:
    labels = [q.get("question") if isinstance(q, dict) else None for q in questions]
    if not labels or any(not isinstance(label, str) or not label for label in labels) or len(set(labels)) != len(labels):
        raise HistoryError("AskUserQuestion questions are empty or ambiguous")
    if isinstance(answers, dict):
        try:
            values = [answers[label] for label in labels]
        except KeyError as exc:
            raise HistoryError("AskUserQuestion answers do not cover every question") from exc
    elif isinstance(answers, list) and len(answers) == len(labels):
        values = []
        for label, value in zip(labels, answers):
            if isinstance(value, dict):
                if "question" in value and value["question"] != label:
                    raise HistoryError("AskUserQuestion answer is paired with another question")
                value = next((value[key] for key in ("answer", "selected", "value", "label") if key in value), _MISSING)
                if value is _MISSING:
                    raise HistoryError("AskUserQuestion answer has no selection")
            values.append(value)
    elif len(labels) == 1:
        values = [answers]
    else:
        raise HistoryError("AskUserQuestion answers do not match questions")
    return tuple((label, _answer(value)) for label, value in zip(labels, values))


def _structured(record: Mapping[str, Any], blocks: list[dict[str, Any]]) -> Any | None:
    holders: list[Mapping[str, Any]] = [record, *blocks]
    if isinstance(record.get("message"), dict):
        holders.append(record["message"])
    values = [holder["toolUseResult"] for holder in holders if "toolUseResult" in holder]
    if len(values) > 1:
        raise HistoryError("ambiguous toolUseResult object")
    return values[0] if values else None


_MISSING = object()


@dataclass(frozen=True)
class HistorySource:
    path: Path
    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not _UUID.fullmatch(self.session_id):
            raise HistoryError("session_id must be a canonical lowercase UUID")
        try:
            if str(uuid.UUID(self.session_id)) != self.session_id:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise HistoryError("session_id must be a canonical UUID") from exc
        path = Path(self.path).expanduser()
        if path.name != f"{self.session_id}.jsonl":
            raise HistoryError("source filename must exactly match <session_id>.jsonl")
        try:
            path = path.resolve(strict=True)
        except OSError as exc:
            raise HistoryError("explicit source does not exist") from exc
        if not path.is_file():
            raise HistoryError("explicit source is not a file")
        object.__setattr__(self, "path", path)

    source_kind = staticmethod(source_kind)

    def _records(
        self,
        *,
        include_task_notifications: bool = False,
        defer_incomplete_tail: bool = False,
        _report_deferred_tail: bool = False,
    ) -> list[_Record] | tuple[list[_Record], bool]:
        """Read the canonical stream, with one index-only incomplete-tail mode.

        Ordinary public HistorySource operations retain strict JSONL behavior.  The
        index projection is allowed to defer only an unterminated final append so
        an actively written native transcript does not turn a prior valid prefix
        into a false parser failure.  A complete malformed line remains an error.
        """
        if type(include_task_notifications) is not bool:
            raise HistoryError("include_task_notifications must be a boolean")
        if type(defer_incomplete_tail) is not bool or type(_report_deferred_tail) is not bool:
            raise HistoryError("history reader options must be booleans")
        records, seen, deferred_tail = [], set(), False
        try:
            with self.path.open("rb") as handle:
                while True:
                    start, raw = handle.tell(), handle.readline(8 * 1024 * 1024 + 1)
                    if not raw:
                        break
                    if len(raw) > 8 * 1024 * 1024:
                        raise HistoryError("single history record exceeds the bounded reader limit")
                    if defer_incomplete_tail and not raw.endswith(b"\n"):
                        deferred_tail = True
                        break
                    data = _decode(raw, start)
                    # 原生 JSONL 的 sessionId 标识持久会话；session_id 可保留另一层传输身份。
                    # 只有无原生字段的记录才使用 stream 字段，不能用它覆盖错误的持久身份。
                    identity = data.get("sessionId", data.get("session_id"))
                    if ("sessionId" in data or "session_id" in data) and identity != self.session_id:
                        raise HistoryError(f"record at byte {start} belongs to another session")
                    kind = source_kind(data)
                    if kind == "meta" and data["type"] in _STATE_ONLY and not (include_task_notifications and _task_notice(data)):
                        continue  # 用户指令和显式需要的原生任务终态不能跳过。
                    message_id = _message_id(data)
                    if message_id in seen:
                        raise HistoryError(f"duplicate message identifier {message_id!r}")
                    seen.add(message_id)
                    records.append(_Record(data, message_id, start, handle.tell(), hashlib.sha256(raw).hexdigest(), kind))
        except OSError as exc:
            raise HistoryError("cannot read explicit source") from exc
        return (records, deferred_tail) if _report_deferred_tail else records

    def _pairs(self, records: list[_Record]) -> dict[str, _Pair]:
        asks: dict[str, tuple[int, _Record, list[Any]]] = {}
        for index, info in enumerate(records):
            if info.kind != "assistant":
                continue
            for block in _blocks(_content(info.data)):
                if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "AskUserQuestion":
                    continue
                tool_id, tool_input = block.get("id"), block.get("input")
                if not isinstance(tool_id, str) or not tool_id or not isinstance(tool_input, dict) or not isinstance(tool_input.get("questions"), list):
                    raise HistoryError("AskUserQuestion tool_use has invalid id or questions")
                if tool_id in asks:
                    raise HistoryError("duplicate AskUserQuestion tool_use_id")
                asks[tool_id] = (index, info, tool_input["questions"])
        pairs: dict[str, _Pair] = {}
        for index, info in enumerate(records):
            if info.kind != "tool_result":
                continue
            blocks = _results(info.data)
            ids = [b["tool_use_id"] for b in blocks if b["tool_use_id"] in asks and asks[b["tool_use_id"]][0] < index]
            structured = _structured(info.data, blocks) if ids else None
            if not ids or structured is None:
                continue
            if not isinstance(structured, dict) or not {"questions", "answers", "annotations"} <= structured.keys() or not isinstance(structured["questions"], list):
                raise HistoryError("AskUserQuestion result has no complete toolUseResult object")
            declared = structured.get("tool_use_id", structured.get("toolUseId"))
            if declared is not None:
                if not isinstance(declared, str) or declared not in ids:
                    raise HistoryError("toolUseResult does not match its tool_result id")
                ids = [declared]
            if len(set(ids)) != 1:
                raise HistoryError("toolUseResult does not identify exactly one AskUserQuestion")
            _, ask, questions = asks[ids[0]]
            if _json(questions) == _json(structured["questions"]):
                pairs[info.message_id] = _Pair(ask, _selections(structured["questions"], structured["answers"]))
        return pairs

    @staticmethod
    def _kind(info: _Record, pairs: Mapping[str, _Pair]) -> str:
        return "verified_user_answer" if info.message_id in pairs else info.kind

    def _locator(self, info: _Record, kind: str, tool_use_id: str | None = None) -> dict[str, Any]:
        locator = {"source_path": str(self.path), "session_id": self.session_id, "message_id": info.message_id, "start_byte": info.start, "end_byte": info.end, "sha256": info.sha256, "source_kind": kind}
        return locator if tool_use_id is None else locator | {"tool_use_id": tool_use_id}

    def locator(self, message_id: str) -> dict[str, Any]:
        if not isinstance(message_id, str) or not message_id:
            raise HistoryError("message_id must be a non-empty string")
        # Exact lookup of an already-complete record remains usable while the
        # host is writing one final unterminated append.  Stream pages and other
        # full-history operations intentionally remain strict.
        records, pairs = self._records(defer_incomplete_tail=True), None
        pairs = self._pairs(records)
        for info in records:
            if info.message_id == message_id:
                return self._locator(info, self._kind(info, pairs))
        raise HistoryError("message_id is not present in the explicit source")

    def _bound(
        self,
        locator: Mapping[str, Any],
        *,
        include_records: bool = False,
        defer_incomplete_tail: bool = False,
    ):
        if type(defer_incomplete_tail) is not bool:
            raise HistoryError("defer_incomplete_tail must be a boolean")
        required = ("source_path", "session_id", "message_id", "start_byte", "end_byte", "sha256", "source_kind")
        if not isinstance(locator, Mapping) or any(key not in locator for key in required):
            raise HistoryError("locator is incomplete")
        start, end, sha = locator["start_byte"], locator["end_byte"], locator["sha256"]
        if locator["source_path"] != str(self.path) or locator["session_id"] != self.session_id or type(start) is not int or type(end) is not int or start < 0 or end <= start or not isinstance(locator["message_id"], str) or not isinstance(sha, str) or not _HASH.fullmatch(sha) or not isinstance(locator["source_kind"], str):
            raise HistoryError("locator has invalid fields")
        records, pairs = self._records(defer_incomplete_tail=defer_incomplete_tail), None
        pairs = self._pairs(records)
        info = next((item for item in records if item.start == start and item.end == end), None)
        if not info or info.message_id != locator["message_id"] or not hmac.compare_digest(info.sha256, sha):
            raise HistoryError("bound record changed, was truncated, or lost its byte boundary")
        kind = self._kind(info, pairs)
        if kind != locator["source_kind"]:
            raise HistoryError("locator source kind is no longer valid")
        tool_id = locator.get("tool_use_id")
        if "tool_use_id" in locator and (not isinstance(tool_id, str) or info.kind != "tool_result" or not any(b["tool_use_id"] == tool_id for b in _results(info.data))):
            raise HistoryError("locator has an invalid tool_use_id")
        canonical = self._locator(info, kind, tool_id)
        return (info, pairs, canonical, records) if include_records else (info, pairs, canonical)

    def _text(self, info: _Record, kind: str, pairs: Mapping[str, _Pair], secrets: Any) -> str:
        if kind == "verified_user_answer":
            text = "\n\n".join(f"Question: {question}\nAnswer: {answer}" for question, answer in pairs[info.message_id].selections)
        elif kind == "original_user" and info.data.get("type") == "attachment":
            text = _queued_user_text(info.data)
        elif kind in {"meta", "sidechain", "local_command"}:
            text = ""
        elif kind == "summary" and isinstance(info.data.get("summary"), str):
            text = info.data["summary"]
        elif kind == "tool_result":
            text = "\n".join(fragment for block in _results(info.data) for fragment in _texts(block.get("content")))
        else:
            text = "\n".join(_texts(_content(info.data)))
        return redact(text, secrets)

    def index_projection(self, *, secrets: Any = ()) -> dict[str, Any]:
        """Project one source into index-safe public records without raw JSONL data.

        This is the only bulk-projection entry point that can defer an
        unterminated final append.  Exact locator/read operations use the same
        narrowly scoped allowance for an already-complete bound record.  The
        projection still uses the canonical parser, source classification,
        AskUserQuestion pairing, redaction, byte ranges, and record hashes.  The
        returned data deliberately excludes tool inputs, thinking, image bytes,
        and every other raw native-record field.
        """
        projected = self._records(defer_incomplete_tail=True, _report_deferred_tail=True)
        records, deferred_tail = projected
        pairs = self._pairs(records)
        calls: dict[str, tuple[int, str]] = {}
        for index, info in enumerate(records):
            if info.kind != "assistant":
                continue
            for block in _blocks(_content(info.data)):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_use_id, name = block.get("id"), block.get("name")
                if not isinstance(tool_use_id, str) or not tool_use_id or not isinstance(name, str) or not name:
                    continue
                prior = calls.get(tool_use_id)
                if prior is not None and prior[1] != name:
                    raise HistoryError("tool_use_id is associated with conflicting tool names")
                if prior is None:
                    calls[tool_use_id] = (index, name)

        entries: list[dict[str, Any]] = []
        for index, info in enumerate(records):
            kind = self._kind(info, pairs)
            text = self._text(info, kind, pairs, secrets)
            # A tool invocation without a public text block must not become an
            # empty history item merely because the index observed its metadata.
            if not text:
                continue
            timestamp = info.data.get("timestamp")
            entry: dict[str, Any] = {
                "locator": self._locator(info, kind),
                "source_kind": kind,
                "timestamp": redact(timestamp, secrets) if isinstance(timestamp, str) else None,
                "text": text,
                "total_chars": len(text),
            }
            if info.kind == "tool_result":
                tool_use_ids: list[str] = []
                tool_names: list[str] = []
                tool_uses: list[dict[str, str | None]] = []
                for block in _results(info.data):
                    tool_use_id = block["tool_use_id"]
                    call = calls.get(tool_use_id)
                    name = call[1] if call is not None and call[0] < index else None
                    tool_uses.append({"tool_use_id": tool_use_id, "tool_name": name})
                    if tool_use_id not in tool_use_ids:
                        tool_use_ids.append(tool_use_id)
                    if name is not None and name not in tool_names:
                        tool_names.append(name)
                if tool_use_ids:
                    entry["tool_use_ids"] = tool_use_ids
                    entry["tool_uses"] = tool_uses
                if tool_names:
                    entry["tool_names"] = tool_names
            entries.append(entry)
        return {"entries": entries, "deferred_incomplete_tail": deferred_tail}

    def read(self, locator: Mapping[str, Any], start: int = 0, limit: int = 2_000, secrets: Any = ()) -> dict[str, Any]:
        # An exact, already-complete bound record remains readable while the
        # native host is appending one unfinished final line.  Other HistorySource
        # operations keep their strict full-stream behavior, and any complete
        # malformed line still fails in the canonical reader.
        info, pairs, canonical = self._bound(locator, defer_incomplete_tail=True)
        text, next_start, total = _bounded(self._text(info, canonical["source_kind"], pairs, secrets), start, limit)
        return {"locator": canonical, "text": text, "source_kind": canonical["source_kind"], "next_start": next_start, "total_chars": total}

    def instruction_updates_since(self, locator: Mapping[str, Any], secrets: Any = ()) -> list[dict[str, Any]]:
        """Return bounded later human instructions after revalidating one bound locator."""
        info, pairs, _, records = self._bound(locator, include_records=True)
        index = next((i for i, item in enumerate(records) if item.start == info.start), None)
        if index is None:
            raise HistoryError("bound instruction is absent from the verified source")
        updates, used = [], 0
        for item in records[index + 1:]:
            kind = self._kind(item, pairs)
            if kind not in {"original_user", "verified_user_answer"}:
                continue
            if len(updates) >= _MAX_UPDATE_RECORDS:
                raise HistoryError("instruction updates exceed the bounded reader limit")
            text, next_start, _ = _bounded(self._text(item, kind, pairs, secrets), 0, _MAX_CHARS)
            if next_start is not None:
                raise HistoryError("instruction update exceeds the bounded public-text limit")
            update = {"locator": self._locator(item, kind), "source_kind": kind, "text": text}
            used += len(_json(update).encode("utf-8"))
            if used > _MAX_UPDATE_BYTES:
                raise HistoryError("instruction updates exceed the 64 KiB report limit")
            updates.append(update)
        return updates

    def page(self, offset: int = 0, limit: int = 10, secrets: Any = (), *, instructions_only: bool = False) -> dict[str, Any]:
        if type(offset) is not int or offset < 0:
            raise HistoryError("offset must be a non-negative byte offset")
        if type(limit) is not int or not 1 <= limit <= _MAX_RECORDS:
            raise HistoryError(f"limit must be between 1 and {_MAX_RECORDS}")
        records, pairs = self._records(), None
        pairs, size = self._pairs(records), self.path.stat().st_size
        if type(instructions_only) is not bool:
            raise HistoryError("instructions_only must be a boolean")
        if instructions_only:
            records = [info for info in records if self._kind(info, pairs) in {"original_user", "verified_user_answer"}]
        starts = {info.start: index for index, info in enumerate(records)}
        if offset == size:
            index = len(records)
        elif offset == 0:
            index = 0  # 初始元数据不影响首条可见记录或空指令页。
        elif offset in starts:
            index = starts[offset]
        else:
            raise HistoryError("offset is not a record byte boundary")
        selected, entries = records[index : index + limit], []
        for info in selected:
            kind, text = self._kind(info, pairs), None
            text = self._text(info, kind, pairs, secrets)
            entries.append({"message_id": info.message_id, "source_kind": kind, "timestamp": info.data.get("timestamp"), "start_byte": info.start, "end_byte": info.end, "sha256": info.sha256, "snippet": " ".join(text.split())[:160], "locator": self._locator(info, kind)})
        next_index = index + len(selected)
        return {"entries": entries, "next_offset": records[next_index].start if next_index < len(records) else None}

    def search(self, query: str, offset: int = 0, limit: int = 10, secrets: Any = (),
               scan_limit: int = 200) -> dict[str, Any]:
        """只在显式来源的脱敏公开文本中检索；返回可继续分页的准确定位。"""
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_CHARS:
            raise HistoryError("query must be a non-empty bounded string")
        if type(offset) is not int or offset < 0:
            raise HistoryError("offset must be a non-negative byte offset")
        if type(limit) is not int or not 1 <= limit <= _MAX_RECORDS:
            raise HistoryError(f"limit must be between 1 and {_MAX_RECORDS}")
        if type(scan_limit) is not int or not 1 <= scan_limit <= 200:
            raise HistoryError("scan_limit must be between 1 and 200")
        records = self._records()
        pairs, size = self._pairs(records), self.path.stat().st_size
        starts = {info.start: index for index, info in enumerate(records)}
        if offset == size:
            index = len(records)
        elif offset == 0 and records:
            index = 0
        elif offset in starts:
            index = starts[offset]
        else:
            raise HistoryError("offset is not a record byte boundary")
        entries, scanned = [], 0
        needle = query.casefold()
        while index < len(records) and scanned < scan_limit and len(entries) < limit:
            info = records[index]
            index += 1
            scanned += 1
            kind = self._kind(info, pairs)
            text = self._text(info, kind, pairs, secrets)
            if needle not in text.casefold():
                continue
            # 用逐词边界搜索只作展示，不用折叠后的下标切割原始 Unicode 文本。
            words = text.split()
            match = next((i for i, word in enumerate(words) if needle in word.casefold()), 0)
            snippet = " ".join(words[max(0, match - 5):])[:160]
            entries.append({"locator": self._locator(info, kind), "source_kind": kind,
                            "snippet": snippet, "total_chars": len(text)})
        return {"entries": entries, "scanned_records": scanned,
                "next_offset": records[index].start if index < len(records) else None}

    def latest_usage(self) -> dict[str, Any]:
        """只投影准确主会话最后一次模型调用的用量，不返回文本或思考。"""
        records = self._records()
        for info in reversed(records):
            if info.kind != "assistant":
                continue
            message = info.data.get("message", {})
            usage = message.get("usage")
            model = message.get("model")
            if model == "<synthetic>":
                continue  # 原生宿主生成的状态记录不是模型调用，零用量不能覆盖真实计量。
            if not isinstance(usage, dict) or not isinstance(model, str) or not model:
                raise HistoryError("latest native assistant record has no actual model/input usage")
            names = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            values = {name: usage.get(name, 0) for name in names}
            if "input_tokens" not in usage or any(type(value) is not int or value < 0 for value in values.values()):
                raise HistoryError("latest native input usage is incomplete")
            output = usage.get("output_tokens", 0)
            if type(output) is not int or output < 0:
                raise HistoryError("latest native output usage is invalid")
            request_id = message.get("id")
            return {"locator": self._locator(info, info.kind), "actual_model": model,
                    "cwd": info.data.get("cwd"), "usage": values, "total_input_tokens": sum(values.values()),
                    "output_tokens": output, "timestamp": info.data.get("timestamp"),
                    "request_id": request_id if isinstance(request_id, str) and request_id else info.message_id,
                    "cache_fields_complete": all(name in usage for name in names[1:])}
        raise HistoryError("no native main-session usage is available")

    def activity(self) -> dict[str, Any]:
        """从准确原生日志重建未结算工具和后台句柄，不复制业务状态。"""
        tools, results, backgrounds, controls = {}, set(), {}, {}
        for info in self._records(include_task_notifications=True):
            notice = _task_notice(info.data)
            if notice is not None:
                backgrounds.pop(notice[0], None)
            if info.kind == "assistant":
                for block in _blocks(_content(info.data)):
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                        tools[block["id"]] = {"name": block.get("name"),
                                               "input_hash": hashlib.sha256(_json(block.get("input", {})).encode()).hexdigest()}
                        request = block.get("input", {})
                        if block.get("name") in {"TaskStop", "TaskOutput"} and isinstance(request, dict):
                            target = request.get("task_id")
                            if isinstance(target, str) and target:
                                controls[block["id"]] = (block["name"], target, backgrounds.get(target))
            if info.kind != "tool_result":
                continue
            ids = {block["tool_use_id"] for block in _results(info.data)}
            new_ids = ids - results
            results.update(ids)
            response = info.data.get("toolUseResult")
            if not new_ids or not isinstance(response, dict):
                continue
            for key in ("backgroundTaskId", "resumedAgentId"):
                handle = response.get(key)
                if isinstance(handle, str) and handle:
                    backgrounds[handle] = {"tool_use_ids": sorted(new_ids), "source_record": info.message_id}
            handle = response.get("agentId")
            if response.get("isAsync") is True and isinstance(handle, str) and handle:
                backgrounds[handle] = {"tool_use_ids": sorted(new_ids), "source_record": info.message_id}
            # 顶层 toolUseResult 必须能唯一配回一次真实任务工具调用。
            if len(ids) == 1 and new_ids and not any(block.get("is_error", False) for block in _results(info.data)):
                control = controls.get(next(iter(ids)))
                if control is not None:
                    name, target, launched = control
                    settled = terminal_task_result(name, {"task_id": target}, response)
                    if settled and launched is not None and backgrounds.get(settled) == launched:
                        backgrounds.pop(settled, None)
        return {"pending_tools": {key: value for key, value in tools.items() if key not in results},
                "background_handles": backgrounds}

    def instruction_bounds(self) -> dict[str, Any]:
        """只返回真实用户指令的首尾定位；没有指令与来源损坏分开处理。"""
        records = self._records()
        pairs = self._pairs(records)
        selected = [info for info in records if self._kind(info, pairs) in {"original_user", "verified_user_answer"}]
        if not selected:
            return {"first": None, "last": None}
        return {"first": self._locator(selected[0], self._kind(selected[0], pairs)),
                "last": self._locator(selected[-1], self._kind(selected[-1], pairs))}

    def latest_instruction(self) -> dict[str, Any]:
        records, pairs = self._records(), None
        pairs = self._pairs(records)
        for info in reversed(records):
            kind = self._kind(info, pairs)
            if kind in {"original_user", "verified_user_answer"}:
                return self._locator(info, kind)
        raise HistoryError("explicit source contains no user instruction")

    def read_answer(self, locator: Mapping[str, Any], secrets: Any = ()) -> dict[str, Any]:
        info, pairs, canonical = self._bound(locator)
        if canonical["source_kind"] != "verified_user_answer":
            raise HistoryError("locator is not a verified AskUserQuestion answer")
        pair = pairs[info.message_id]
        text = redact("\n\n".join(f"Question: {question}\nAnswer: {answer}" for question, answer in pair.selections), secrets)
        return {"locator": canonical, "text": text, "pairing_locator": self._locator(pair.ask, "assistant")}

    def tool_result(self, tool_use_id: str) -> dict[str, Any]:
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise HistoryError("tool_use_id must be a non-empty string")
        records, pairs = self._records(defer_incomplete_tail=True), None
        pairs = self._pairs(records)
        matches = [info for info in records if info.kind == "tool_result" and any(block["tool_use_id"] == tool_use_id for block in _results(info.data))]
        if len(matches) != 1:
            raise HistoryError("tool_use_id does not identify exactly one tool_result")
        return self._locator(matches[0], self._kind(matches[0], pairs), tool_use_id)

    @staticmethod
    def _absolute(value: Any) -> Path | None:
        if not isinstance(value, str):
            return None
        path = Path(value)
        return Path(os.path.abspath(os.fspath(path))) if path.is_absolute() else None

    @staticmethod
    def _mentions(text: str, path: Path) -> bool:
        expected, position = str(path), text.find(str(path))
        before, after = " \t\r\n\"'`=:([{,>", " \t\r\n\"'`),;:]}<"
        while position >= 0:
            left, right = text[position - 1] if position else "", position + len(expected)
            if (not left or left in before) and (right == len(text) or text[right] in after):
                return True
            position = text.find(expected, position + 1)
        return False

    def _confined(self, value: Path) -> Path:
        if not value.is_absolute():
            raise HistoryError("saved output path must be absolute")
        path, root = Path(os.path.abspath(os.fspath(value))), self.path.parent / self.session_id / "tool-results"
        if not root.is_dir() or root.is_symlink():
            raise HistoryError("session tool-results directory is unavailable or symlinked")
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise HistoryError("saved output is outside this session tool-results directory") from exc
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise HistoryError("saved output path contains a symlink")
        if not relative.parts or not path.is_file():
            raise HistoryError("saved output path is not a file")
        try:
            path.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise HistoryError("saved output resolves outside its session directory") from exc
        return path

    def _references(self, blocks: list[dict[str, Any]], path: Path) -> bool:
        return any(self._absolute(block.get(key)) == path for block in blocks for key in _PATH_FIELDS) or any(self._mentions(text, path) for block in blocks for text in _texts(block.get("content")))

    def saved_output(self, locator: Mapping[str, Any], path: Path, expected_hash: str, start: int = 0, limit: int = 2_000, secrets: Any = ()) -> dict[str, Any]:
        info, _, canonical = self._bound(locator)
        if info.kind != "tool_result" or not isinstance(expected_hash, str) or not _HASH.fullmatch(expected_hash):
            raise HistoryError("saved output needs a tool_result locator and lowercase SHA-256 hash")
        candidate, blocks = self._confined(Path(path)), _results(info.data)
        if "tool_use_id" in canonical:
            blocks = [block for block in blocks if block["tool_use_id"] == canonical["tool_use_id"]]
        elif len(blocks) != 1:
            raise HistoryError("locator does not identify exactly one tool_result block")
        if not blocks or not self._references(blocks, candidate):
            raise HistoryError("selected tool_result does not reference this saved output")
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            raise HistoryError("cannot read saved output") from exc
        actual = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(actual, expected_hash):
            raise HistoryError("saved output hash does not match expected_hash")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HistoryError("saved output is not UTF-8") from exc
        text, next_start, total = _bounded(redact(text, secrets), start, limit)
        return {"locator": canonical, "path": str(candidate), "sha256": actual, "text": text, "next_start": next_start, "total_chars": total}
