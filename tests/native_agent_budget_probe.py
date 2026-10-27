"""Real native child hooks and tools against a localhost scripted model, without paid calls."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from uuid import uuid4

from native_runtime_probe import _handler, isolated_environment, _submit_fixture_input
from claude_context_continuity import core, native_control, tmux_transport, tui_runtime

ROOT = Path(__file__).resolve().parents[1]
FIRST = "NATIVE_CHILD_BUDGET_FIRST"
NEXT = "NATIVE_CHILD_BUDGET_NEXT"


class ChildModel:
    def __init__(self, workspace):
        self.workspace, self.runtime = workspace, None
        self.requests, self.parent_step, self.child_counts = [], 0, {FIRST: 0, NEXT: 0}
        self.done, self.failed, self.denied = False, None, False
        self.lock = threading.Lock()

    def wait_child(self, *, replacement=False):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if self.runtime is not None:
                states = [core.read_json(path) for path in (self.runtime.directory / "agent-budgets").glob("*.json")]
                found = [state for state in states if (
                    state.get("terminal_observed") or self.native_finished(state))
                    and bool(state.get("replacement_required")) != replacement]
                if found:
                    return found[-1]["agent_id"]
            time.sleep(.05)
        self.failed = "child_terminal_timeout"
        return "a00000000"

    @staticmethod
    def native_finished(state):
        source = state.get("source_path")
        if not source:
            return False
        try:
            return json.loads(Path(source).with_suffix(".meta.json").read_text()).get("finished") is True
        except (OSError, ValueError):
            return False

    def respond(self, body):
        names = [tool.get("name") for tool in body.get("tools", [])]
        if not names:
            return None, "Isolated child-budget probe", 100
        texts = [block.get("text", "") for message in body.get("messages", [])
                 if isinstance(message.get("content"), list)
                 for block in message["content"] if isinstance(block, dict) and block.get("type") == "text"]
        for message in body.get("messages", []):
            if isinstance(message.get("content"), str):
                texts.append(message["content"])
        marker = next((marker for marker in (FIRST, NEXT) if any(text.startswith(marker) for text in texts)), None)
        with self.lock:
            self.requests.append({"scope": marker or "parent", "model": body.get("model")})
            if len(self.requests) > 14:
                self.failed = "request_bound_exceeded"
                return None, "FIXTURE_FAILED", 100
            if marker:
                self.child_counts[marker] += 1
                number = self.child_counts[marker]
            else:
                self.parent_step += 1
                number = self.parent_step
        if marker == FIRST:
            if number > 1:
                self.failed = "oversized_child_requested_again"
                return None, "FIXTURE_FAILED", 100
            command = shlex.join([sys.executable, "-B", "-c",
                "from pathlib import Path; p=Path(__import__('sys').argv[1]); "
                "f=p.open('a'); f.write('once\\n'); f.close()", str(self.workspace / "producer.txt")])
            return "Bash", {"command": command, "description": "Produce the isolated fixture once"}, 80000
        if marker == NEXT:
            if number == 1:
                return "Read", {"file_path": str(self.workspace / "producer.txt")}, 100
            return None, "CHILD_HANDOFF_COMPLETE", 100
        if number == 1:
            return "Agent", {"description": "Run child budget fixture", "subagent_type": "budget-worker",
                             "prompt": FIRST + ": execute the fixture once; no other work."}, 100
        if number == 2:
            agent = self.wait_child()
            return "SendMessage", {"to": agent, "message": "Fixture: this oversized resume must be denied."}, 100
        if number == 3:
            rendered = json.dumps(body.get("messages", []), ensure_ascii=False)
            self.denied = "超限子代理" in rendered
            if not self.denied:
                self.failed = "resume_denial_not_delivered"
            return "Agent", {"description": "Continue from fixture artifact", "subagent_type": "budget-worker",
                             "prompt": NEXT + ": read the existing producer.txt; never rerun the producer."}, 100
        if number == 4:
            self.wait_child(replacement=True)
            return "Read", {"file_path": str(self.workspace / "producer.txt")}, 100
        self.done = True
        return None, "NATIVE_CHILD_BUDGET_DONE", 100


def main():
    home = Path.home() / ".claude" / ("ab-" + uuid4().hex[:8])
    home.mkdir(mode=0o700)
    workspace, config, temporary = home / "workspace", home / "native-config", home / "tmp"
    for path in (workspace, config, temporary):
        path.mkdir()
    (workspace / ".claude").mkdir()
    (workspace / ".claude/settings.local.json").write_text(json.dumps({"env": {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1"}}))
    (config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark",
        "projects": {str(workspace): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
    model = ChildModel(workspace)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(model))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    previous_home, runtime = core.HOME, None
    result = {"status": "failed", "scope": "real native CLI and child tools; scripted localhost model and token usage",
              "fixture_home": str(home)}
    env = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
           "ANTHROPIC_AUTH_TOKEN": "synthetic-local-fixture-token", "CLAUDE_CONTEXT_CONTINUITY_DIR": str(home),
           "CLAUDE_CONFIG_DIR": str(config), "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1",
           "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
           "TMPDIR": str(temporary), "CLAUDE_CODE_TMPDIR": str(temporary), "PYTHONPATH": str(ROOT / "src")}
    started = time.monotonic()
    try:
        with isolated_environment(env):
            core.HOME = home
            agents = {"budget-worker": {"description": "Isolated budget test worker", "prompt": "Perform only the fixed local fixture.",
                                         "tools": ["Bash", "Read"], "model": "sonnet"}}
            runtime = tui_runtime.create(workspace, width=140, height=40, native_args=(
                "--setting-sources", "local", "--model", "claude-sonnet-4-6", "--permission-mode", "default",
                "--allowedTools", "Agent,SendMessage,Bash,Read", "--agents", json.dumps(agents)))
            model.runtime = runtime
            submitted = False
            while time.monotonic() - started < 100:
                state = runtime._state()
                if state.get("initial_session_started") and native_control.ready(state["native_control"]) and not submitted:
                    _submit_fixture_input(runtime, "NATIVE_PARENT_BUDGET_FIXTURE: execute only the isolated child-budget test.")
                    submitted = True
                if model.done and state.get("at_turn_boundary") or model.failed:
                    break
                time.sleep(.05)
            state = runtime._state()
            children = [core.read_json(path) for path in (runtime.directory / "agent-budgets").glob("*.json")]
            producer = workspace / "producer.txt"
            produced_once = producer.is_file() and producer.read_text() == "once\n"
            metadata = [json.loads(path.read_text()) for path in config.glob("projects/*/*/subagents/agent-*.meta.json")]
            preserved = len(metadata) == 2 and all(row.get("agentType") == "budget-worker" for row in metadata)
            bounded_handoff = any(child.get("replacement_required") and child.get("terminal_observed")
                                  and child.get("stop_marker_observed") for child in children)
            result.update(native_version=state.get("native_cli_version"), context_id=runtime.conversation_id,
                          bounded_handoff_observed=bounded_handoff,
                          child_counts=model.child_counts, request_summaries=model.requests, fixture_failure=model.failed,
                          denied_oversized_resume=model.denied, produced_once=produced_once, role_preserved=preserved,
                          child_budget_count=len(children), parent_rotation_count=state.get("automatic_rotations", 0),
                          elapsed_seconds=round(time.monotonic() - started, 3))
            success = (model.done and not model.failed and produced_once and preserved and model.denied and bounded_handoff
                       and model.child_counts == {FIRST: 1, NEXT: 2} and len(children) == 2
                       and not state.get("automatic_rotations"))
            result["status"] = "passed" if success else "failed"
            if not success:
                (home / "probe-screen.txt").write_text(tmux_transport.capture(state["tmux"]))
    finally:
        if runtime:
            binding = runtime._state()["tmux"]
            try:
                tmux_transport.inspect(binding)
                subprocess.run(["tmux", "-S", binding["socket_path"], "kill-server"], capture_output=True, timeout=10)
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
