"""Generated hook CLI plus the real local socket transport, against an isolated host fixture."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = Path(os.environ.get("CCLAUDE_TEST_PACKAGE_ROOT", str(ROOT / "src"))).resolve()
sys.path.insert(0, str(PACKAGE_ROOT))

import claude_context_continuity as package
from claude_context_continuity import core, native_control, tmux_transport
from claude_context_continuity.tui_runtime import TuiRuntime, _write_plugin


def test_generated_failure_hook_rotates_and_confirms_through_an_isolated_socket(monkeypatch):
    assert Path(package.__file__).is_relative_to(PACKAGE_ROOT)
    with tempfile.TemporaryDirectory(prefix="rr-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        state_root, workspace, config = root / "s", root / "w", root / "c"
        workspace.mkdir()
        config.mkdir()
        monkeypatch.setattr(core, "HOME", state_root)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        monkeypatch.setenv("CLAUDE_CONTEXT_CONTINUITY_DIR", str(state_root))
        monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "1000")
        sid, next_sid = str(uuid4()), str(uuid4())
        source, next_source = root / f"{sid}.jsonl", root / f"{next_sid}.jsonl"
        records = [
            {"type": "user", "uuid": str(uuid4()), "sessionId": sid,
             "message": {"role": "user", "content": "Continue the authorized fixture without rerunning tools."}},
            {"type": "assistant", "uuid": str(uuid4()), "sessionId": sid, "cwd": str(workspace),
             "message": {"id": "native-success", "role": "assistant", "model": "fixture-native-model",
                         "content": [{"type": "text", "text": "Completed fixture work."}],
                         "usage": {"input_tokens": 125, "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0, "output_tokens": 0}}},
            {"type": "assistant", "uuid": str(uuid4()), "sessionId": sid,
             "isApiErrorMessage": True, "error": "invalid_request",
             "message": {"role": "assistant", "model": "<synthetic>",
                         "content": [{"type": "text", "text": "Prompt is too long"}],
                         "usage": {"input_tokens": 0, "output_tokens": 0}}},
        ]
        source.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        next_source.write_text("", encoding="utf-8")
        configuration = {"hash": "socket-fixture", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        runtime = TuiRuntime.create(cwd=workspace, session_id=sid, configuration=configuration)
        runtime.on_hook({"hook_event_name": "SessionStart", "source": "startup", "session_id": sid,
                         "cwd": str(workspace), "transcript_path": str(source), "permission_mode": "default"})
        control = native_control.binding(runtime.conversation_id)
        native_control.prepare(control)
        binding = {"pane_pid": os.getpid()}
        monkeypatch.setattr(tmux_transport, "inspect", lambda _: binding)
        with core.lock(runtime.lock_path):
            state = runtime._state()
            state.update(transport="tmux_tui", tmux=binding, native_control=control,
                         active_child_handles=["a1234567890abcdef"])
            runtime._save(state)
        plugin = runtime.directory / "plugin"
        _write_plugin(plugin, runtime.conversation_id)
        manifest = core.read_json(plugin / ".claude-plugin/plugin.json")
        assert manifest["version"] == package.__version__
        runtime._control_auth = "isolated-socket-fixture-authentication-123456"
        environment = {
            **os.environ,
            "CLAUDE_CONFIG_DIR": str(config),
            "CLAUDE_CONTEXT_CONTINUITY_DIR": str(state_root),
            "CLAUDE_CONTINUITY_ID": runtime.conversation_id,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000",
            "DISABLE_COMPACT": "1",
            "PYTHONPATH": str(PACKAGE_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        def hook(event):
            command = manifest["hooks"][event["hook_event_name"]][0]["hooks"][0]
            result = subprocess.run([command["command"], *command["args"]], input=json.dumps(event),
                                    cwd=workspace, env=environment, capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        hook({"hook_event_name": "StopFailure", "session_id": sid, "cwd": str(workspace),
              "transcript_path": str(source), "error": "invalid_request", "last_assistant_message": "Prompt is too long"})
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        received, errors = [], []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(control["socket_path"])
            listener.listen(2)
            listener.settimeout(5)

            def native_fixture():
                try:
                    for index in range(2):
                        client, _ = listener.accept()
                        with client:
                            client.settimeout(5)
                            pending = b""
                            while pending.count(b"\n") < 3:
                                chunk = client.recv(8192)
                                if not chunk:
                                    raise ValueError("fixture connection closed before the transport barrier")
                                pending += chunk
                            frames = [json.loads(line) for line in pending.splitlines()]
                            assert frames[0] == {"role": "attacher", "auth": runtime._control_auth}
                            assert frames[2] == {"type": "repaint"}
                            text = frames[1]["text"]
                            received.append(text)
                            if index == 0:
                                assert text == "/clear"
                            else:
                                assert text.startswith("<continuity-host-event>\n")
                                next_source.write_text(json.dumps({
                                    "type": "user", "uuid": str(uuid4()), "sessionId": next_sid,
                                    "message": {"role": "user", "content": text},
                                }) + "\n", encoding="utf-8")
                            client.sendall(b'{"type":"repaint-done"}\n')
                except Exception as error:
                    errors.append(error)

            server = threading.Thread(target=native_fixture, name="isolated-rollover-host", daemon=True)
            server.start()
            try:
                assert runtime.advance()["phase"] == "clear_sent"
                assert runtime.advance()["phase"] == "clear_sent"
                hook({"hook_event_name": "SessionStart", "source": "clear", "session_id": next_sid,
                      "cwd": str(workspace), "transcript_path": str(next_source), "permission_mode": "default"})
                assert runtime.advance()["phase"] == "awaiting_continuation"
                result = runtime.advance()
                assert result["phase"] == "running"
                assert result["continuation_observed"]
                assert result["native_clear_confirmations"] == 1
                assert result["active_child_handles"] == ["a1234567890abcdef"]
                assert result["authorization"]["root_instruction_locator"]["session_id"] == sid
                assert hashlib.sha256(source.read_bytes()).hexdigest() == before
                assert runtime._state()["native_permission_mode"] == "default"
            finally:
                server.join(timeout=6)
            assert not server.is_alive()
            assert not errors
        assert len(received) == 2
        assert received[0] == "/clear"
        assert runtime._control_auth not in runtime.state_path.read_text(encoding="utf-8")
