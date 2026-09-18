"""显式运行的原生 TUI 集成探针；模型响应由本机固定脚本提供，不调用外部模型。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import core, native_control, tmux_transport, tui_runtime
from claude_context_continuity.history import HistorySource, _blocks, _content, _results, _texts

ROOT_INPUT = "NATIVE_FIXTURE_ONLY: run the isolated producer, then inspect history at later steps; no other work."
BATCH_ONLY_INPUT = "NATIVE_BATCH_ONLY_FIXTURE: inspect the isolated fixture across automatic continuation; no other work."
QUEUED_STOP_INPUT = "NATIVE_QUEUED_STOP_FIXTURE: list the remaining fixture tasks without using tools."
QUEUED_STOP_REPLY = "NATIVE_QUEUED_REPLY_DONE"
DEFERRED_INPUT = "NATIVE_DEFERRED_ONLY: inspect the existing result after switching; do not run the producer again."
DRAFT = "NATIVE_UNSUBMITTED_DRAFT"
CHECKPOINT = "NATIVE_OVERSIZED_RESULT\n" + "x" * 30000 + "\n"


def _json_values(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _json_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_values(item)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            yield from _json_values(json.loads(value))
        except ValueError:
            pass


class ScriptedModel:
    def __init__(self, workspace, *, batch_only=False, queued_stop=False):
        self.workspace = workspace
        self.batch_only = batch_only
        self.queued_stop = queued_stop
        self.waiting_for_queued_input = threading.Event()
        self.queued_input_observed = threading.Event()
        self.queued_reply_response_id = None
        self.requests = []
        self.history_calls = []
        self.history_read_requests = {}
        self.successful_history_reads = set()
        self.bounded_tool_results = set()
        self.locator = None
        self.stage = 0
        self.done = False
        self.failed = None
        self.auxiliary_requests = 0
        self.batch_only_tool_calls = []
        self.blocked_batch_response_id = None
        self.completion_response_id = None
        self.lock = threading.Lock()

    def _batch_only_response(self, body):
        continued = any(message.get("role") == "user" and any(
            text.startswith("<continuity-host-event>") for text in _texts(message.get("content")))
            for message in body.get("messages", []) if isinstance(message, dict))
        if continued and self.batch_only_tool_calls:
            self.done = True
            self.completion_response_id = f"msg_native_probe_{len(self.requests)}"
            return None, "NATIVE_BATCH_ONLY_DONE", 100
        if self.queued_stop:
            if not self.batch_only_tool_calls:
                self.waiting_for_queued_input.set()
                if not self.queued_input_observed.wait(20):
                    self.failed = "queued_input_not_persisted"
                    return None, "NATIVE_QUEUED_STOP_UNAVAILABLE", 100
            else:
                queued = any(QUEUED_STOP_INPUT in text for message in body.get("messages", [])
                             if isinstance(message, dict) for text in _texts(message.get("content")))
                if not queued or self.queued_reply_response_id is not None:
                    self.failed = "queued_reply_request_unexpected"
                    return None, "NATIVE_QUEUED_STOP_UNAVAILABLE", 100
                self.queued_reply_response_id = f"msg_native_probe_{len(self.requests)}"
                return None, QUEUED_STOP_REPLY, 60000
        if len(self.batch_only_tool_calls) >= 4:
            self.failed = "batch_only_continuation_not_received"
            return None, "NATIVE_BATCH_ONLY_UNAVAILABLE", 100
        self.stage += 1
        self.batch_only_tool_calls.append("Read")
        self.blocked_batch_response_id = f"msg_native_probe_{len(self.requests)}"
        return "Read", {"file_path": str(self.workspace / "batch-only.txt")}, 60000 if self.queued_stop else 95000

    def respond(self, body):
        with self.lock:
            names = [tool["name"] for tool in body.get("tools", []) if isinstance(tool, dict) and "name" in tool]
            if not names:
                self.auxiliary_requests += 1
                return None, "Isolated native probe", 100
            if len(self.requests) >= 20:
                self.failed = "scripted_main_request_limit"
                return None, "NATIVE_FIXTURE_UNAVAILABLE", 100
            for value in _json_values(body.get("messages", [])):
                tool_id = value.get("tool_use_id")
                if value.get("type") == "tool_result":
                    content = json.dumps(value.get("content"), ensure_ascii=False)
                    if "NATIVE_OVERSIZED_RESULT" in content and len(content.encode("utf-8")) < 20000:
                        self.bounded_tool_results.add(tool_id)
                expected_text = self.history_read_requests.get(tool_id)
                if value.get("type") == "tool_result" and expected_text and not value.get("is_error"):
                    if any(item.get("source_kind") == "original_user" and item.get("text") == expected_text
                           for item in _json_values(value.get("content"))):
                        self.successful_history_reads.add(tool_id)
                if "entries" in value:
                    for entry in value["entries"]:
                        locator = entry.get("locator", {}) if isinstance(entry, dict) else {}
                        if locator.get("source_kind") == "original_user":
                            self.locator = locator
            self.requests.append({"stage": self.stage, "input_bytes": len(json.dumps(body).encode()),
                                  "tool_names": names if len(self.requests) < 2 else []})
            if self.batch_only:
                return self._batch_only_response(body)
            if self.stage == 0:
                self.stage += 1
                return "Bash", {"command": shlex.join([
                                    sys.executable, "-B", str(self.workspace / "producer.py")]),
                                "run_in_background": True, "description": "Run isolated native continuity fixture"}, 85000
            if self.stage in {1, 3, 5, 7, 8}:
                action = {1: "history-windows", 3: "history-search", 5: "history",
                          7: "history-search", 8: "history"}[self.stage]
                arguments = core.module_argv(action)
                if action == "history-search":
                    query = "NATIVE_DEFERRED_ONLY" if self.stage == 7 else "NATIVE_FIXTURE_ONLY"
                    arguments += ["--query", query, "--role", "user", "--page-size", "2"]
                elif action == "history":
                    if not self.locator:
                        raise RuntimeError("history search did not return an exact original-user locator")
                    self.history_read_requests[f"tool_native_probe_{len(self.requests)}"] = (
                        ROOT_INPUT if self.stage == 5 else DEFERRED_INPUT)
                    arguments += ["--source", self.locator["source_path"],
                                  "--session-id", self.locator["session_id"],
                                  "--message-id", self.locator["message_id"],
                                  "--expected-sha256", self.locator["sha256"], "--limit", "300"]
                self.history_calls.append(action)
                self.stage += 1
                return "Bash", {"command": shlex.join(arguments),
                                "description": "Read isolated native history through existing CLI"}, 20000
            if self.stage in {2, 4, 6}:
                self.stage += 1
                return "Read", {"file_path": str(self.workspace / "checkpoint.txt")}, 20000
            if self.stage == 9:
                deadline = time.monotonic() + 20
                while not (self.workspace / "producer.done").exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                self.stage += 1
                return "Read", {"file_path": str(self.workspace / "producer.done")}, 20000
            self.done = True
            return None, "NATIVE_FIXTURE_DONE", 20000


def _handler(model):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw)
            if "count_tokens" in self.path:
                payload = json.dumps({"input_tokens": max(1, len(raw) // 4)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            try:
                tool, value, usage = model.respond(body)
            except Exception:
                self.send_error(500, "isolated scripted-provider failure")
                return
            index = len(model.requests)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def event(kind, value):
                self.wfile.write(f"event: {kind}\ndata: {json.dumps(value)}\n\n".encode())
                self.wfile.flush()

            try:
                event("message_start", {"type": "message_start", "message": {
                    "id": f"msg_native_probe_{index}", "type": "message", "role": "assistant",
                    "model": body.get("model", "claude-sonnet-4-6"), "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": usage, "output_tokens": 0,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})
                if tool:
                    event("content_block_start", {"type": "content_block_start", "index": 0,
                        "content_block": {"type": "tool_use", "id": f"tool_native_probe_{index}", "name": tool, "input": {}}})
                    event("content_block_delta", {"type": "content_block_delta", "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)}})
                else:
                    event("content_block_start", {"type": "content_block_start", "index": 0,
                                                  "content_block": {"type": "text", "text": ""}})
                    event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                  "delta": {"type": "text_delta", "text": value}})
                event("content_block_stop", {"type": "content_block_stop", "index": 0})
                event("message_delta", {"type": "message_delta", "delta": {
                    "stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 100}})
                event("message_stop", {"type": "message_stop"})
            except (BrokenPipeError, ConnectionResetError):
                pass
    return Handler


@contextmanager
def isolated_environment(values):
    previous = dict(os.environ)
    try:
        for key in list(os.environ):
            if (key.startswith("ANTHROPIC_") or re.search(
                    r"(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|SECRET|PRIVATE_KEY)$", key)):
                os.environ.pop(key, None)
        os.environ.update(values)
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _submit_fixture_input(runtime, text, *, keyboard=False):
    state = runtime._state()
    if keyboard:
        binding = state["tmux"]
        command = ["tmux", "-S", binding["socket_path"], "send-keys", "-t", binding["pane_id"]]
        subprocess.run([*command, "-l", text], capture_output=True, check=True, timeout=5)
        subprocess.run([*command, "Enter"], capture_output=True, check=True, timeout=5)
        return
    control = state["native_control"]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(control["socket_path"])
        client.sendall((json.dumps({"role": "attacher", "auth": runtime._control_auth}) + "\n" +
                       json.dumps({"type": "reply", "text": text}) + "\n").encode())


def _queued_fixture_input_observed(native_config, session_id):
    for path in native_config.glob(f"projects/*/{session_id}.jsonl"):
        with path.open() as handle:
            for line in handle:
                if not line.endswith("\n"):
                    continue
                row = json.loads(line)
                if (row.get("type") == "queue-operation" and row.get("operation") == "enqueue"
                        and row.get("content") == QUEUED_STOP_INPUT):
                    return True
    return False


def _batch_only_artifacts(runtime, initial_sid, continued_sid, continued_source_path, blocked_response_id):
    initial_sessions, original_user_records = [], []
    continued_session_binding = False
    continuation_records = 0
    response_sessions = {}
    blocked_request_tool_ids, settled_tool_ids = set(), set()
    stopped_response_ids, old_responses_after_stop, final_stop_response_ids = [], [], []
    queued_before_batch_stop = False
    seen_sources = set()
    for binding_path in (runtime.directory / "history").glob("*.json"):
        binding = core.read_json(binding_path)
        sid, source_path = binding["session_id"], binding["source_path"]
        if (sid, source_path) in seen_sources:
            continue
        seen_sources.add((sid, source_path))
        source = HistorySource(Path(source_path), sid)
        if sid == continued_sid and source_path == continued_source_path:
            continued_session_binding = True
        for row in source._records():
            text = None
            if row.kind == "original_user" or row.data.get("type") == "user":
                text = "\n".join(_texts(_content(row.data)))
            if row.kind == "original_user":
                original_user_records.append((sid, text))
                if text == BATCH_ONLY_INPUT:
                    initial_sessions.append(sid)
            if (sid == continued_sid and isinstance(text, str)
                    and text.startswith("<continuity-host-event>")):
                continuation_records += 1
            if row.kind == "assistant":
                message = row.data.get("message", {})
                response_id = message.get("id") if isinstance(message, dict) else None
                if isinstance(response_id, str) and re.fullmatch(r"msg_native_probe_\d+", response_id):
                    response_sessions.setdefault(response_id, set()).add(sid)
                if response_id == blocked_response_id:
                    blocked_request_tool_ids.update(
                        block["id"] for block in _blocks(_content(row.data))
                        if isinstance(block, dict) and block.get("type") == "tool_use"
                        and isinstance(block.get("id"), str))
            elif row.kind == "tool_result":
                settled_tool_ids.update(block["tool_use_id"] for block in _results(row.data))
        if sid == initial_sid:
            latest_response_id = None
            queued_seen = False
            with Path(source_path).open() as handle:
                for line in handle:
                    row = json.loads(line)
                    if (row.get("type") == "queue-operation" and row.get("operation") == "enqueue"
                            and row.get("content") == QUEUED_STOP_INPUT):
                        queued_seen = True
                    if row.get("type") == "assistant" and row.get("message", {}).get("model") != "<synthetic>":
                        latest_response_id = row["message"].get("id")
                        if stopped_response_ids and latest_response_id not in stopped_response_ids:
                            old_responses_after_stop.append(latest_response_id)
                    attachment = row.get("attachment", {})
                    if (attachment.get("type") == "hook_stopped_continuation"
                            and attachment.get("hookEvent") == "PostToolBatch"):
                        stopped_response_ids.append(latest_response_id)
                        queued_before_batch_stop = queued_seen
                    if attachment.get("type") == "hook_success" and attachment.get("hookEvent") == "Stop":
                        final_stop_response_ids.append(latest_response_id)
    return {
        "queued_fixture_records_in_initial_session": original_user_records.count((initial_sid, QUEUED_STOP_INPUT)),
        "queued_before_batch_stop": queued_before_batch_stop,
        "final_stop_response_ids": final_stop_response_ids,
        "initial_fixture_user_records": len(initial_sessions),
        "initial_fixture_records_in_initial_session": initial_sessions.count(initial_sid),
        "original_user_record_count": len(original_user_records),
        "continued_session_binding": continued_session_binding,
        "continuation_records_in_new_session": continuation_records,
        "response_sessions": {key: sorted(value) for key, value in response_sessions.items()},
        "blocked_request_tool_ids": sorted(blocked_request_tool_ids),
        "blocked_result_tool_ids": sorted(blocked_request_tool_ids & settled_tool_ids),
        "stopped_response_ids": stopped_response_ids,
        "old_responses_after_stop": old_responses_after_stop,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the isolated native TUI continuity probe.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--batch-only", action="store_true",
                       help="Exercise one Read-only PostToolBatch automatic rotation.")
    modes.add_argument("--queued-stop", action="store_true",
                       help="Exercise a queued tool-free reply and Stop before advancing the rotation.")
    args = parser.parse_args(argv)
    queued_stop = args.queued_stop
    batch_only = args.batch_only or queued_stop
    # 短私有路径满足 macOS Unix socket 长度限制；不复用任何活动控制器目录。
    home = Path.home() / ".claude" / ("ct-" + uuid4().hex[:8])
    home.mkdir(mode=0o700)
    workspace = home / "workspace"
    workspace.mkdir()
    (workspace / ".claude").mkdir()
    (workspace / ".claude/settings.local.json").write_text(json.dumps({"env": {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1"}}))
    if batch_only:
        # 排队场景靠待处理输出触发预算保护，同时给原生客户端留出执行排队回答的空间。
        payload = "NATIVE_BATCH_ONLY_RESULT\n" + ("x" * 20000 if queued_stop else "")
        (workspace / "batch-only.txt").write_text(payload)
    else:
        (workspace / "checkpoint.txt").write_text(CHECKPOINT)
        (workspace / "producer.py").write_text(
            "from pathlib import Path\nimport time\nr=Path(__file__).resolve().parent\n"
            "with (r/'producer.starts').open('a') as f:f.write('started\\n')\n"
            "time.sleep(8)\n(r/'producer.done').write_text('producer finished exactly once\\n')\n")
    temporary = home / "tmp"
    temporary.mkdir()
    native_config = home / "native-config"
    native_config.mkdir()
    (native_config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark",
        "projects": {str(workspace): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
    model = ScriptedModel(workspace, batch_only=batch_only, queued_stop=queued_stop)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(model))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    started = time.monotonic()
    runtime = None
    result = {"scope": "real native TUI and tools with scripted localhost model/usage, no external LLM",
              "fixture_home": str(home), "status": "failed"}
    if batch_only:
        result["mode"] = "queued-stop" if queued_stop else "batch-only"
    previous_home = core.HOME
    values = {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "ANTHROPIC_AUTH_TOKEN": "synthetic-local-fixture-token",
        "CLAUDE_CONTEXT_CONTINUITY_DIR": str(home),
        "CLAUDE_CONFIG_DIR": str(native_config),
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000",
        "DISABLE_COMPACT": "1", "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "TMPDIR": str(temporary), "CLAUDE_CODE_TMPDIR": str(temporary),
        "PYTHONPATH": str(ROOT / "src"),
    }
    try:
        with isolated_environment(values):
            core.HOME = home
            allowed_tools = "Read" if batch_only else "Bash,Read"
            runtime = tui_runtime.create(workspace, width=140, height=40, native_args=(
                "--setting-sources", "local",
                "--model", "claude-sonnet-4-6", "--permission-mode", "default",
                "--allowedTools", allowed_tools))
            initial_sid = None
            switched_before_finish = False
            submitted = deferred_submitted = draft_entered = False
            queued_submitted = queued_reply_stop_observed = False
            queued_stop_observed_at = None
            initial_input = BATCH_ONLY_INPUT if batch_only else ROOT_INPUT
            while time.monotonic() - started < 100:
                state = runtime._state()
                if state.get("initial_session_started") and native_control.ready(state["native_control"]) and not submitted:
                    initial_sid = state["session_id"]
                    _submit_fixture_input(runtime, initial_input)
                    submitted = True
                if submitted:
                    if batch_only:
                        if model.failed:
                            break
                        if queued_stop and not queued_reply_stop_observed:
                            if state.get("deferred_inputs"):
                                model.failed = "queued_input_deferred_instead_of_executed"
                                break
                            if model.waiting_for_queued_input.is_set() and not queued_submitted:
                                _submit_fixture_input(runtime, QUEUED_STOP_INPUT, keyboard=True)
                                queued_submitted = True
                            if queued_submitted and not model.queued_input_observed.is_set():
                                if _queued_fixture_input_observed(native_config, initial_sid):
                                    model.queued_input_observed.set()
                            # 只控制探针的轮询次序，让真实排队输入先执行并收到 Stop；不改运行状态。
                            if (state["session_id"] == initial_sid and state.get("stop_serial", 0) > 0
                                    and state.get("stop_text_hash") == core.digest(QUEUED_STOP_REPLY)):
                                queued_reply_stop_observed = True
                                queued_stop_observed_at = time.monotonic()
                            else:
                                time.sleep(.05)
                                continue
                        runtime.advance()
                        state = runtime._state()
                        if (queued_stop_observed_at is not None and state["session_id"] == initial_sid
                                and time.monotonic() - queued_stop_observed_at > 10):
                            model.failed = "queued_stop_rotation_timeout"
                            break
                        if (model.done and state.get("continuation_observed")
                                and state["session_id"] != initial_sid and state.get("source_path")):
                            records = HistorySource(Path(state["source_path"]), state["session_id"])._records(
                                defer_incomplete_tail=True)
                            if any(row.kind == "assistant" and row.data["message"].get("id")
                                   == model.completion_response_id for row in records):
                                break
                        time.sleep(.05)
                        continue
                    if not draft_entered and (workspace / "producer.starts").exists():
                        # 仅给自建探针输入未提交草稿；生产换窗通道不使用 send-keys。
                        subprocess.run(["tmux", "-S", state["tmux"]["socket_path"], "send-keys",
                                        "-t", state["tmux"]["pane_id"], "-l", DRAFT],
                                       capture_output=True, check=True, timeout=5)
                        draft_entered = True
                    runtime.advance()
                    state = runtime._state()
                    if state["phase"] in {"clear_sent", "awaiting_tui_prompt"} and not deferred_submitted:
                        _submit_fixture_input(runtime, DEFERRED_INPUT)
                        deferred_submitted = True
                    if state["session_id"] != initial_sid and not (workspace / "producer.done").exists():
                        switched_before_finish = True
                    if model.failed:
                        break
                    if model.done and state.get("continuation_observed") and (workspace / "producer.done").exists():
                        break
                time.sleep(.05)
            if not batch_only and (workspace / "producer.starts").exists():
                finish_deadline = time.monotonic() + 12
                while not (workspace / "producer.done").exists() and time.monotonic() < finish_deadline:
                    runtime.advance()
                    time.sleep(.1)
            state = runtime._state()
            starts = (workspace / "producer.starts").read_text().splitlines() if (workspace / "producer.starts").exists() else []
            screen = tmux_transport.capture(state["tmux"])
            draft_preserved = draft_entered and DRAFT in screen
            submitted_inputs = []
            for binding_path in (runtime.directory / "history").glob("*.json"):
                binding = core.read_json(binding_path)
                source = HistorySource(Path(binding["source_path"]), binding["session_id"])
                submitted_inputs.extend("\n".join(_texts(_content(row.data)))
                                        for row in source._records() if row.kind == "original_user")
            deferred_count = submitted_inputs.count(DEFERRED_INPUT)
            archive_verified = False
            for path in (runtime.directory / "outputs").glob("*.json"):
                archived = json.loads(path.read_text())
                content = archived.get("file", {}).get("content")
                if (isinstance(content, str) and content.rstrip("\n") == CHECKPOINT.rstrip("\n")
                        and core.sha(path) == path.stem):
                    archive_verified = True
            batch_artifacts = None
            post_tool_batch_stop_observed = False
            old_session_model_requests_after_block = []
            completion_bound_to_new_session = False
            no_model_request_resumed_in_old_session = False
            if batch_only:
                batch_artifacts = _batch_only_artifacts(
                    runtime, initial_sid, state["session_id"], state.get("source_path"),
                    model.blocked_batch_response_id)
                post_tool_batch_stop_observed = bool(
                    batch_artifacts["stopped_response_ids"] == [model.blocked_batch_response_id]
                    and batch_artifacts["blocked_request_tool_ids"]
                    and batch_artifacts["blocked_request_tool_ids"] == batch_artifacts["blocked_result_tool_ids"])
                old_session_model_requests_after_block = batch_artifacts["old_responses_after_stop"]
                no_model_request_resumed_in_old_session = not old_session_model_requests_after_block
                completion_bound_to_new_session = (
                    model.completion_response_id is not None
                    and batch_artifacts["response_sessions"].get(model.completion_response_id) == [state["session_id"]])
            result.update(native_version=state.get("native_cli_version"), phase=state["phase"],
                          pause_reason=state.get("pause_reason"), diagnostic=state.get("diagnostic"),
                          new_session_confirmed=state["session_id"] != initial_sid,
                          switch_before_background_finished=switched_before_finish,
                          background_producer_runs=len(starts), producer_finished=(workspace / "producer.done").exists(),
                          continuation_observed=state.get("continuation_observed", False),
                          history_calls=model.history_calls, request_summaries=model.requests,
                          successful_exact_history_reads=len(model.successful_history_reads),
                          bounded_tool_results_observed=len(model.bounded_tool_results),
                          full_result_archive_verified=archive_verified,
                          deferred_input_submitted=deferred_submitted, deferred_input_records=deferred_count,
                          draft_preserved=draft_preserved, draft_submitted=DRAFT in submitted_inputs,
                          auxiliary_request_count=model.auxiliary_requests, fixture_failure=model.failed,
                          elapsed_seconds=round(time.monotonic() - started, 3))
            if batch_only:
                result.update(
                    post_tool_batch_stop_observed=post_tool_batch_stop_observed,
                    post_tool_batch_tool_ids=batch_artifacts["blocked_request_tool_ids"],
                    initial_fixture_user_records=batch_artifacts["initial_fixture_user_records"],
                    initial_fixture_records_in_initial_session=batch_artifacts["initial_fixture_records_in_initial_session"],
                    original_user_record_count=batch_artifacts["original_user_record_count"],
                    continued_session_binding=batch_artifacts["continued_session_binding"],
                    continuation_records_in_new_session=batch_artifacts["continuation_records_in_new_session"],
                    scripted_read_calls=model.batch_only_tool_calls,
                    scripted_main_request_count=len(model.requests),
                    blocked_batch_response_id=model.blocked_batch_response_id,
                    completion_response_id=model.completion_response_id,
                    scripted_response_sessions=batch_artifacts["response_sessions"],
                    old_session_model_requests_after_block=old_session_model_requests_after_block,
                    no_model_request_resumed_in_old_session=no_model_request_resumed_in_old_session,
                    completion_bound_to_new_session=completion_bound_to_new_session)
            if queued_stop:
                queued_path_verified = (
                    queued_submitted and queued_reply_stop_observed and batch_artifacts["queued_before_batch_stop"]
                    and batch_artifacts["queued_fixture_records_in_initial_session"] == 1
                    and model.queued_reply_response_id is not None
                    and set(old_session_model_requests_after_block) == {model.queued_reply_response_id}
                    and batch_artifacts["final_stop_response_ids"] == [model.queued_reply_response_id]
                    and batch_artifacts["original_user_record_count"] == 2)
                result.update(
                    controller_advance_held_until_queued_stop=True,
                    queued_before_batch_stop=batch_artifacts["queued_before_batch_stop"],
                    queued_fixture_records_in_initial_session=batch_artifacts["queued_fixture_records_in_initial_session"],
                    queued_reply_response_id=model.queued_reply_response_id,
                    queued_reply_stop_observed=queued_reply_stop_observed,
                    final_stop_response_ids=batch_artifacts["final_stop_response_ids"],
                    no_additional_prompt_after_queued_reply=queued_path_verified)
            if batch_only:
                success = (model.done and model.failed is None and state["session_id"] != initial_sid
                           and state.get("continuation_observed") and post_tool_batch_stop_observed
                           and (queued_path_verified if queued_stop else no_model_request_resumed_in_old_session)
                           and completion_bound_to_new_session
                           and batch_artifacts["initial_fixture_records_in_initial_session"] == 1
                           and batch_artifacts["original_user_record_count"] == (2 if queued_stop else 1)
                           and batch_artifacts["continued_session_binding"]
                           and batch_artifacts["continuation_records_in_new_session"] == 1
                           and set(model.batch_only_tool_calls) == {"Read"}
                           and len(model.requests) == len(model.batch_only_tool_calls) + (2 if queued_stop else 1)
                           and not starts and not deferred_submitted and not draft_entered)
            else:
                success = (model.done and switched_before_finish and len(starts) == 1
                           and state.get("continuation_observed") and len(model.successful_history_reads) == 2
                           and archive_verified and model.bounded_tool_results
                           and deferred_submitted and deferred_count == 1 and draft_preserved and DRAFT not in submitted_inputs
                           and model.history_calls == ["history-windows", "history-search", "history", "history-search", "history"])
            result["status"] = "passed" if success else "failed"
            if not success:
                # 只保留固定探针终端；不捕获或查看用户活动窗口。
                (home / "probe-screen.txt").write_text(screen)
    finally:
        if runtime is not None:
            state = runtime._state()
            try:
                tmux_transport.inspect(state["tmux"])
                subprocess.run(["tmux", "-S", state["tmux"]["socket_path"], "kill-server"],
                               capture_output=True, timeout=10)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        server.shutdown()
        server.server_close()
        core.HOME = previous_home
        result["receipt_path"] = str(home / "receipt.json")
        (home / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
