"""Real native /clear + SendMessage; localhost scripted transport, private repository profile."""
from __future__ import annotations

import argparse
import fcntl
from http.server import ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from claude_context_continuity import core
from claude_context_continuity.tui_runtime import TuiRuntime, _write_plugin
from native_runtime_probe import _handler, isolated_environment

MARKER = "NATIVE_PRESERVED_CHILD_HISTORY"


class ResumeModel:
    def __init__(self, workspace, config, running_crossing=False):
        self.workspace, self.config = workspace, config
        self.running_crossing = running_crossing
        self.requests, self.child_count, self.parent_count = [], 0, 0
        self.phase, self.done_phase, self.failed, self.agent = 0, -1, None, None
        self.history_checks = []

    def wait(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.03)
        raise TimeoutError("native child did not finish")

    def finished(self):
        for path in self.config.glob("projects/*/*/subagents/agent-*.meta.json"):
            value = json.loads(path.read_text())
            if value.get("agentType") == "resume-worker" and value.get("finished"):
                self.agent = path.name.removeprefix("agent-").removesuffix(".meta.json")
                return True
        return False

    def respond(self, body):
        if not body.get("tools"):
            return None, "Isolated resume fixture", 100
        rendered = json.dumps(body.get("messages", []), ensure_ascii=False)
        texts = [message["content"] for message in body.get("messages", []) if isinstance(message.get("content"), str)]
        texts += [block.get("text", "") for message in body.get("messages", [])
                  if isinstance(message.get("content"), list) for block in message["content"]
                  if isinstance(block, dict) and block.get("type") == "text"]
        child = any(text.startswith(MARKER) for text in texts)
        self.requests.append({"scope": "child" if child else "parent", "phase": self.phase,
                              "model": body.get("model")})
        if len(self.requests) > 35:
            self.failed = "request_bound_exceeded"
            return None, "FIXTURE_FAILED", 100
        if child:
            self.child_count += 1
            if self.child_count == 1:
                command = shlex.join([sys.executable, "-B", "-c",
                    "from pathlib import Path; p=Path(__import__('sys').argv[1]); "
                    "f=p.open('a'); f.write('once\\n'); f.close(); print('ORIGINAL_PRODUCER_RECEIPT')", str(self.workspace / "producer.txt")])
                return "Bash", {"command": command, "description": "Produce isolated artifact exactly once"}, 100
            if self.running_crossing and self.child_count == 4:
                command = shlex.join([sys.executable, "-B", "-c",
                    "import time; from pathlib import Path\np=Path(__import__('sys').argv[1])\n"
                    "for _ in range(600):\n if p.exists(): break\n time.sleep(.05)\n"
                    "else: raise RuntimeError('private gate timeout')\nprint('RUNNING_CROSSING_RECEIPT')",
                    str(self.workspace / "release-child")])
                return "Bash", {"command": command, "description": "Wait for the private parent rotation gate"}, 100
            expected = f"RESUME_PHASE_{self.phase}"
            self.history_checks.append({"phase": self.phase, "original_prompt": MARKER in rendered,
                                        "original_tool_result": "ORIGINAL_PRODUCER_RECEIPT" in rendered,
                                        "resume_message": expected in rendered})
            return None, f"CHILD_PHASE_{self.phase}_COMPLETE", 100
        self.parent_count += 1
        if self.phase == 0 and self.parent_count == 1:
            return "Agent", {"description": "Create bounded resume fixture", "subagent_type": "resume-worker",
                             "prompt": MARKER + ": produce producer.txt once and return. No other work."}, 100
        send_step = 2 if self.phase == 0 else 1
        if self.parent_count == send_step:
            self.wait(self.finished)
            return "SendMessage", {"to": self.agent, "message": f"RESUME_PHASE_{self.phase}: return a short acknowledgement; do not rerun tools."}, 100
        if self.parent_count == send_step + 1:
            expected = self.phase + 3 + int(self.running_crossing and self.phase == 2)
            self.wait(lambda: self.child_count >= expected)
            if "No transcript found" in rendered or "无法确认原子代理身份" in rendered:
                self.failed = "native_resume_failed"
            self.done_phase = self.phase
            return None, f"PARENT_PHASE_{self.phase}_DONE", 100
        # Native background completion notifications can create another parent turn.
        self.done_phase = self.phase
        return None, f"PARENT_PHASE_{self.phase}_DONE", 100


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--running-crossing", action="store_true", help="Keep a native child tool running across the second clear")
    parser.add_argument("--fragmented-history", action="store_true", help="Split the private native child stream before its first cross-window resume")
    args = parser.parse_args()
    home = ROOT / ".cache" / "subagent-resume-repair-20261027" / ("native-" + uuid4().hex[:8])
    home.mkdir(parents=True, mode=0o700)
    workspace, config, temporary = home / "workspace", home / "native-config", home / "tmp"
    for path in (workspace, config, temporary):
        path.mkdir()
    (workspace / ".claude").mkdir()
    (workspace / ".claude/settings.local.json").write_text(json.dumps({"env": {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1"}}))
    (config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark",
        "projects": {str(workspace): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
    model = ResumeModel(workspace, config, running_crossing=args.running_crossing)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(model))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    previous_home, process, master = core.HOME, None, None
    result = {"status": "failed", "scope": "real native CLI /clear and SendMessage; scripted localhost model, no paid/external requests",
              "fixture_home": str(home), "native_version": subprocess.check_output([shutil.which("claude"), "--version"], text=True).strip()}
    env = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}", "ANTHROPIC_AUTH_TOKEN": "synthetic-local-fixture-token",
           "CLAUDE_CONTEXT_CONTINUITY_DIR": str(home), "CLAUDE_CONFIG_DIR": str(config),
           "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
           "TMPDIR": str(temporary), "CLAUDE_CODE_TMPDIR": str(temporary), "PYTHONPATH": str(ROOT / "src"),
           "PYTHONDONTWRITEBYTECODE": "1", "TERM": "xterm-256color", "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE": "1"}
    screen, started = bytearray(), time.monotonic()
    try:
        with isolated_environment(env):
            # Do not inherit the user's hook/controller identity or native auth channels.
            for key in ("CLAUDE_CONTINUITY_ID", "CCLAUDE_CONTROL_AUTH_TOKEN", "CCLAUDE_CONTROL_SOCKET", "CLAUDECODE",
                        "CLAUDE_BG_RV_AUTH", "CLAUDE_BG_RENDEZVOUS_SOCK", "CLAUDE_CODE_CHILD_SESSION", "TMUX", "TMUX_PANE"):
                os.environ.pop(key, None)
            core.HOME = home
            sid = str(uuid4())
            configuration = core.configuration(workspace)
            runtime = TuiRuntime.create(cwd=workspace, session_id=sid, configuration=configuration)
            plugin = home / "plugin"
            _write_plugin(plugin, runtime.conversation_id)
            os.environ["CLAUDE_CONTINUITY_ID"] = runtime.conversation_id
            agents = {"resume-worker": {"description": "Isolated native continuation worker", "prompt": "Only perform the bounded local fixture.",
                                        "tools": ["Bash", "Read"], "model": "sonnet"}}
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
            process = subprocess.Popen([shutil.which("claude"), "--session-id", sid, "--setting-sources", "local",
                "--plugin-dir", str(plugin), "--model", "claude-opus-4-7", "--permission-mode", "default",
                "--allowedTools", "Agent,SendMessage,Bash,Read", "--agents", json.dumps(agents)],
                cwd=workspace, stdin=slave, stdout=slave, stderr=slave, env=dict(os.environ), start_new_session=True)
            os.close(slave)

            def drain():
                while process.poll() is None:
                    try:
                        if select.select([master], [], [], .1)[0]:
                            screen.extend(os.read(master, 65536))
                    except OSError:
                        break
            threading.Thread(target=drain, daemon=True).start()

            def wait(predicate, label):
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"native exited during {label}: {process.returncode}")
                    if predicate():
                        return
                    time.sleep(.05)
                raise TimeoutError(label)

            def submit(text):
                os.write(master, text.encode())
                time.sleep(.15)
                os.write(master, b"\r")

            wait(lambda: runtime._state().get("initial_session_started"), "initial SessionStart")
            time.sleep(1)
            sessions = [sid]
            for phase in range(3):
                model.phase, model.parent_count = phase, 0
                if phase:
                    previous = runtime._state()["session_id"]
                    if args.running_crossing and phase == 2:
                        result["running_at_rotation"] = model.agent in runtime._state().get("active_child_handles", [])
                    submit("/clear")
                    wait(lambda: runtime._state()["session_id"] != previous, "real native /clear")
                    sessions.append(runtime._state()["session_id"])
                    if args.running_crossing and phase == 2:
                        (workspace / "release-child").write_text("parent clear observed\n")
                    time.sleep(.4)
                    if args.fragmented_history and phase == 1:
                        original = next(config.glob(f"projects/*/{sessions[0]}/subagents/agent-{model.agent}.jsonl"))
                        project = original.parents[2]
                        current = project / sessions[1] / "subagents" / original.name
                        lines = original.read_bytes().splitlines(keepends=True)
                        rows = [json.loads(line) for line in lines]
                        cut = next(index for index, row in enumerate(rows)
                                   if row.get("type") == "user" and "RESUME_PHASE_0" in json.dumps(row))
                        bridge = rows[cut]["parentUuid"]
                        while cut and rows[cut - 1].get("type") == "attachment" and rows[cut - 1].get("parentUuid") == bridge:
                            cut -= 1
                        prefix, suffix = b"".join(lines[:cut]), b"".join(lines[cut:])
                        assert MARKER.encode() in prefix and b"ORIGINAL_PRODUCER_RECEIPT" in prefix
                        temporary_history = current.with_suffix(".fixture")
                        temporary_history.write_bytes(suffix)
                        os.replace(temporary_history, current)
                        original.write_bytes(prefix)
                        result["fragment_fixture"] = {"original_path": str(original), "bridge_uuid": bridge,
                            "original_sha256": hashlib.sha256(prefix).hexdigest(),
                            "suffix_sha256": hashlib.sha256(suffix).hexdigest(),
                            "prefix_rows": cut, "suffix_rows": len(rows) - cut}
                submit(f"PARENT_RESUME_FIXTURE_PHASE_{phase}: perform only the scripted local fixture.")
                wait(lambda: model.done_phase == phase or model.failed, f"phase {phase} native SendMessage")
                if model.failed:
                    raise AssertionError(model.failed)
                wait(lambda: runtime._state().get("at_turn_boundary"), "parent terminal boundary")
                time.sleep(1)
            histories = list(config.glob(f"projects/*/*/subagents/agent-{model.agent}.jsonl"))
            metadata = [json.loads(path.read_text()) for path in config.glob(f"projects/*/*/subagents/agent-{model.agent}.meta.json")]
            identities = {(path.stat().st_dev, path.stat().st_ino) for path in histories}
            producer_once = (workspace / "producer.txt").read_text() == "once\n"
            result.update(context_id=runtime.conversation_id, sessions=sessions, agent_id=model.agent,
                child_requests=model.child_count, request_summaries=model.requests, history_checks=model.history_checks,
                same_history_inode=len(identities) == 1, history_count=len(histories), produced_once=producer_once,
                preserved_role=all(row.get("agentType") == "resume-worker" for row in metadata),
                preserved_model_slot=(len({row["model"] for row in model.requests if row["scope"] == "child"}) == 1
                    and all(row["model"].startswith("claude-sonnet-") for row in model.requests if row["scope"] == "child")
                    and all(row["model"] == "claude-opus-4-7" for row in model.requests if row["scope"] == "parent")),
                preserved_metadata_profile=len({core.digest({key: value for key, value in row.items() if key != "finished"})
                                                for row in metadata}) == 1,
                permission_mode="default", parent_rotations=len(sessions) - 1)
            if args.fragmented_history:
                fixture = result["fragment_fixture"]
                receipts = list(home.rglob("agent_history/*/applied.json"))
                reconstruction = [path for path in receipts if core.read_json(path).get("method") == "native_history_continuation"]
                preserved = {hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in home.rglob("agent_history/*/source-*.jsonl")}
                result["fragment_preservation"] = {
                    "original_unchanged": hashlib.sha256(Path(fixture["original_path"]).read_bytes()).hexdigest() == fixture["original_sha256"],
                    "inputs_preserved": {fixture["original_sha256"], fixture["suffix_sha256"]} <= preserved,
                    "receipts": [str(path) for path in reconstruction]}
                history_identity_ok = (len(identities) == 2 and len(reconstruction) >= 1
                                       and all(value for key, value in result["fragment_preservation"].items() if key != "receipts"))
            else:
                history_identity_ok = len(identities) == 1
            success = (producer_once and len(histories) == 3 and history_identity_ok
                       and model.child_count == 5 + int(args.running_crossing)
                       and all(row["original_prompt"] and row["original_tool_result"] for row in model.history_checks)
                       and model.history_checks[-1]["resume_message"]
                       and (not args.running_crossing or result.get("running_at_rotation"))
                       and result["preserved_role"] and result["preserved_model_slot"] and result["preserved_metadata_profile"])
            result["status"] = "passed" if success else "failed"
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        # Ask only this private CLI to exit through its normal command path.
        if process is not None and process.poll() is None:
            os.write(master, b"\x03")
            time.sleep(.2)
            os.write(master, b"/exit\r")
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                result["cleanup_error"] = f"private native process still running: {process.pid}"
        if master is not None:
            os.close(master)
        server.shutdown()
        server.server_close()
        core.HOME = previous_home
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (home / "screen.txt").write_bytes(screen)
        result["receipt_path"] = str(home / "receipt.json")
        (home / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
