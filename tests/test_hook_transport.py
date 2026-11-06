"""Real local hook transport, with no native CLI or model requests."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import continuity, core, hook_transport, native_control
from claude_context_continuity.tui_runtime import TuiRuntime

AUTH = "isolated-hook-transport-credential-123456"


@pytest.fixture
def home(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ccc-hooks-", dir="/tmp") as directory:
        path = Path(directory).resolve()
        monkeypatch.setattr(core, "HOME", path)
        monkeypatch.setenv(core.STATE_DIRECTORY_ENV, str(path))
        monkeypatch.setenv(native_control.HOOK_AUTH_ENV, AUTH)
        yield path


def serve_once(server):
    thread = threading.Thread(target=server.serve_one, args=(10,), daemon=True)
    thread.start()
    return thread


def test_missing_endpoint_keeps_original_local_entry(home):
    assert hook_transport.request(str(uuid4()), {"hook_event_name": "Stop"}) is None


def test_stale_socket_runs_the_original_hook_and_observer_repair(home, monkeypatch):
    from claude_context_continuity import tui_runtime
    cid = str(uuid4())
    path = hook_transport._path(cid)
    path.parent.mkdir(parents=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(path))
    repairs = []
    runtime = SimpleNamespace(on_hook=lambda event: {"observed": event["hook_event_name"]})
    monkeypatch.setattr(TuiRuntime, "load", lambda _: runtime)
    monkeypatch.setattr(tui_runtime, "repair_controller_from_hook", lambda *args: repairs.append(args))
    result = continuity._dispatch_tui_hook(cid, {"hook_event_name": "Stop"})
    assert result == {"observed": "Stop"}
    assert len(repairs) == 1
    events = (home / "runtime" / "contexts" / cid / "events.jsonl").read_text()
    assert "ConnectionRefusedError" in events


def test_bad_handler_result_does_not_stop_the_hook_server(home):
    cid = str(uuid4())
    with hook_transport.HookServer(cid, AUTH, lambda event: {"bad"} if event["bad"] else {}) as server:
        thread = serve_once(server)
        with pytest.raises(hook_transport.HookExecutionError) as caught:
            hook_transport.request(cid, {"bad": True})
        thread.join(5)
        assert caught.value.error_type == "TypeError"
        thread = serve_once(server)
        assert hook_transport.request(cid, {"bad": False}) == {}
        thread.join(5)
        assert not thread.is_alive()


def test_reply_timeout_does_not_stop_the_hook_server(home):
    cid = str(uuid4())
    with hook_transport.HookServer(cid, AUTH, lambda event: {"text": "x" * 4000000} if event["large"] else {}) as server:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            client.connect(str(server.path))
            client.sendall((json.dumps({"auth": AUTH, "event": {"large": True}}) + "\n").encode())
            thread = serve_once(server)
            thread.join(7)
            assert not thread.is_alive()
        thread = serve_once(server)
        assert hook_transport.request(cid, {"large": False}) == {}
        thread.join(5)
        assert not thread.is_alive()
    events = (home / "runtime" / "contexts" / cid / "events.jsonl").read_text()
    assert "hook_reply_unavailable" in events
    assert "TimeoutError" in events


def test_transport_preserves_large_native_payload(home):
    cid = str(uuid4())
    event = {"hook_event_name": "PostToolUse", "tool_response": {"stdout": "x" * 300000}}
    with hook_transport.HookServer(cid, AUTH, lambda value: {"length": len(value["tool_response"]["stdout"])}) as server:
        thread = serve_once(server)
        assert hook_transport.request(cid, event) == {"length": 300000}
        thread.join(5)
        assert not thread.is_alive()
    assert not hook_transport._path(cid).exists()


def test_wrong_capability_does_not_execute_event(home, monkeypatch):
    calls = []
    with hook_transport.HookServer(str(uuid4()), AUTH, lambda value: calls.append(value)) as server:
        cid = server.path.stem
        monkeypatch.setenv(native_control.HOOK_AUTH_ENV, "different-fixture-credential-123456")
        thread = serve_once(server)
        with pytest.raises(hook_transport.HookExecutionError, match="authentication rejected"):
            hook_transport.request(cid, {"hook_event_name": "Stop"})
        thread.join(5)
    assert calls == []


def test_transport_preserves_actual_failure_type(home):
    def fail(_):
        raise core.LockBusy("fixture lock contention")
    cid = str(uuid4())
    with hook_transport.HookServer(cid, AUTH, fail) as server:
        thread = serve_once(server)
        with pytest.raises(hook_transport.HookExecutionError) as caught:
            hook_transport.request(cid, {"hook_event_name": "Stop"})
        thread.join(5)
    assert caught.value.error_type == "LockBusy"
    assert str(caught.value) == "fixture lock contention"


def test_separate_hook_processes_share_the_controller_reader(home):
    sid = str(uuid4())
    config = {"hash": "fixture-config", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
    runtime = TuiRuntime.create(cwd=home, session_id=sid, configuration=config,
                                configuration_reader=lambda _: config)
    source = home / f"{sid}.jsonl"
    def assistant(message_id):
        return {"type": "assistant", "uuid": message_id, "sessionId": sid,
                "message": {"role": "assistant", "content": "fixture response", "model": "fixture-model",
                            "usage": {"input_tokens": 125, "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 0}}}
    source.write_text("".join(json.dumps(row) + "\n" for row in [
        {"type": "user", "uuid": "original-user", "sessionId": sid,
         "message": {"role": "user", "content": "Continue the authorized fixture."}},
        assistant("response-1"),
    ]))
    runtime.on_hook({"hook_event_name": "SessionStart", "source": "startup", "session_id": sid,
                     "cwd": str(home), "transcript_path": str(source)})
    code = """import json,sys
sys.path.insert(0,sys.argv[1])
from claude_context_continuity.hook_transport import request
result=request(sys.argv[2],json.loads(sys.argv[3]))
assert isinstance(result,dict)
print('received')
"""
    with hook_transport.HookServer(runtime.conversation_id, AUTH, lambda event: continuity._dispatch_tui_hook(
            runtime.conversation_id, event, runtime=runtime)) as server:
        counts = []
        for name in ("PreToolUse", "PostToolUse", "PostToolBatch"):
            if name == "PostToolBatch":
                with source.open("a") as stream:
                    stream.write(json.dumps(assistant("response-2")) + "\n")
            event = {"hook_event_name": name, "session_id": sid, "cwd": str(home),
                     "transcript_path": str(source), "tool_name": "Read", "tool_use_id": "fixture-tool",
                     "tool_calls": []}
            thread = serve_once(server)
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src"),
                                     runtime.conversation_id, json.dumps(event)],
                                    capture_output=True, text=True, timeout=15, env=os.environ.copy())
            thread.join(5)
            assert result.returncode == 0, result.stderr
            assert not thread.is_alive()
            counts.append(runtime._observed_source.reading_work["parsed_records"])
        assert counts == [2, 0, 1]
        assert runtime._state()["authorization"]["root_instruction_locator"]["message_id"] == "original-user"


def test_history_cli_reuses_current_reader_and_preserves_client_redaction(home, monkeypatch):
    sid = str(uuid4())
    config = {"hash": "fixture-config", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
    runtime = TuiRuntime.create(cwd=home, session_id=sid, configuration=config,
                                configuration_reader=lambda _: config)
    source = home / f"{sid}.jsonl"
    secret = "fixture-client-only-sensitive-value-123456"
    def row(message_id):
        return {"type": "user", "uuid": message_id, "sessionId": sid,
                "message": {"role": "user", "content": f"needle {secret}"}}
    source.write_text(json.dumps(row("user-1")) + "\n")
    event = {"hook_event_name": "SessionStart", "source": "startup", "session_id": sid,
             "cwd": str(home), "transcript_path": str(source)}
    runtime.on_hook(event)
    runtime.on_hook({**event, "hook_event_name": "PreToolUse", "tool_use_id": "bootstrap"})
    monkeypatch.setenv("CLAUDE_CONTINUITY_ID", runtime.conversation_id)
    client_env = os.environ | {"FIXTURE_CLIENT_API_KEY": secret}
    code = """import json,sys
sys.path.insert(0,sys.argv[1])
from claude_context_continuity import continuity
args=continuity.parser().parse_args(['history-search','--query','needle'])
print(json.dumps(continuity.dispatch(args)))
"""
    with ExitStack() as resources:
        server = resources.enter_context(hook_transport.HookServer(
            runtime.conversation_id, AUTH,
            lambda value: continuity._dispatch_controller_event(runtime, value)))
        counts = []
        for step in range(3):
            if step == 1:
                with source.open("a") as stream:
                    stream.write(json.dumps(row("user-2")) + "\n")
            thread = serve_once(server) if step < 2 else None
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src")],
                                    env=client_env, capture_output=True, text=True, timeout=15)
            if thread is not None:
                thread.join(5)
                assert not thread.is_alive()
            assert result.returncode == 0, result.stderr
            assert secret not in result.stdout
            response = json.loads(result.stdout)
            assert len(response["entries"]) == (1 if step == 0 else 2)
            counts.append(response["refresh"]["parsed_records"])
        assert counts == [0, 1, 0]
        assert not hasattr(runtime, "_history_index")
        assert secret.encode() not in (runtime.directory / ".cache" / "history.sqlite3").read_bytes()


def test_controller_lifetime_owns_the_hook_endpoint(home, monkeypatch):
    from claude_context_continuity import tui_runtime
    sid = str(uuid4())
    config = {"hash": "fixture-config", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
    runtime = TuiRuntime.create(cwd=home, session_id=sid, configuration=config,
                                configuration_reader=lambda _: config)
    state = runtime._state()
    control = native_control.binding(runtime.conversation_id)
    state.update(controller_managed=True, native_control=control)
    runtime._save(state)
    for key, value in native_control.environment(control, AUTH).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CLAUDE_CONTINUITY_ID", runtime.conversation_id)
    ready, stopped = threading.Event(), threading.Event()
    class ObservedServer(hook_transport.HookServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            ready.set()
    monkeypatch.setattr(hook_transport, "HookServer", ObservedServer)
    monkeypatch.setattr(TuiRuntime, "advance", lambda _: {
        "phase": "closed" if stopped.is_set() else "running", "owned_pid": os.getpid()})
    monkeypatch.setattr(TuiRuntime, "on_hook", lambda self, event: {"event": event["hook_event_name"]})
    thread = threading.Thread(target=tui_runtime.serve, args=(runtime.conversation_id,), daemon=True)
    thread.start()
    try:
        assert ready.wait(5)
        assert hook_transport.request(runtime.conversation_id, {"hook_event_name": "Stop"}) == {"event": "Stop"}
    finally:
        stopped.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not hook_transport._path(runtime.conversation_id).exists()


def test_ignored_state_payload_is_not_retained_as_a_parsed_record(home):
    from claude_context_continuity.history import HistorySource
    sid = str(uuid4())
    source = home / f"{sid}.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in [
        {"type": "user", "uuid": "original-user", "sessionId": sid,
         "message": {"role": "user", "content": "authorized fixture"}},
        {"type": "api-request-blob", "sessionId": sid, "payload": "ignored fixture" * 30000},
    ]))
    reader = HistorySource(source, sid)
    assert len(reader._records()) == 1
    assert len(reader._stream.complete) == 1
    assert reader.reading_work["parsed_records"] == 2
