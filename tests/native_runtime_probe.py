"""显式运行的原生 TUI 集成探针；模型响应由本机固定脚本提供，不调用外部模型。"""
from __future__ import annotations

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
from claude_context_continuity.history import HistorySource, _content, _texts

ROOT_INPUT = "NATIVE_FIXTURE_ONLY: run the isolated producer, then inspect history at later steps; no other work."
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
    def __init__(self, workspace):
        self.workspace = workspace
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
        self.lock = threading.Lock()

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


def _submit_fixture_input(runtime, text):
    control = runtime._state()["native_control"]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(control["socket_path"])
        client.sendall((json.dumps({"role": "attacher", "auth": runtime._control_auth}) + "\n" +
                       json.dumps({"type": "reply", "text": text}) + "\n").encode())


def main():
    # 短私有路径满足 macOS Unix socket 长度限制；不复用任何活动控制器目录。
    home = Path.home() / ".claude" / ("ct-" + uuid4().hex[:8])
    home.mkdir(mode=0o700)
    workspace = home / "workspace"
    workspace.mkdir()
    (workspace / ".claude").mkdir()
    (workspace / ".claude/settings.local.json").write_text(json.dumps({"env": {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1"}}))
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
    model = ScriptedModel(workspace)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(model))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    started = time.monotonic()
    runtime = None
    result = {"scope": "real native TUI and tools with scripted localhost model/usage, no external LLM",
              "fixture_home": str(home), "status": "failed"}
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
            runtime = tui_runtime.create(workspace, width=140, height=40, native_args=(
                "--setting-sources", "local",
                "--model", "claude-sonnet-4-6", "--permission-mode", "default",
                "--allowedTools", "Bash,Read"))
            initial_sid = None
            switched_before_finish = False
            submitted = deferred_submitted = draft_entered = False
            while time.monotonic() - started < 100:
                state = runtime._state()
                if state.get("initial_session_started") and native_control.ready(state["native_control"]) and not submitted:
                    initial_sid = state["session_id"]
                    _submit_fixture_input(runtime, ROOT_INPUT)
                    submitted = True
                if submitted:
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
            if (workspace / "producer.starts").exists():
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
