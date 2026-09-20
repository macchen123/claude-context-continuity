"""连续换窗与原生恢复探针；只使用新建隔离终端和 localhost 脚本模型。"""
from __future__ import annotations

import argparse
from collections import Counter
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from native_runtime_probe import ScriptedModel, _handler, _submit_fixture_input, isolated_environment
from claude_context_continuity import core, tmux_transport, tui_runtime
from claude_context_continuity.history import HistorySource, _content, _results, _texts

ROOT_INPUT = "NATIVE_CONTINUITY_ROOT: complete the isolated read sequence; recover existing history, never replay completed tools."
RESUME_INPUT = "NATIVE_CONTINUITY_RESUME: continue that same isolated read sequence."
IDLE_REPLY = "NATIVE_CONTINUITY_IDLE_READY"


class ContinuousModel(ScriptedModel):
    def __init__(self, workspace, cycles, *, controller_restart=False):
        super().__init__(workspace, batch_only=True)
        self.cycles = cycles
        self.controller_restart = controller_restart
        self.idle_sent = False
        self.continuations = set()
        self.read_response_ids = []

    def _batch_only_response(self, body):
        if self.controller_restart and not self.idle_sent:
            self.idle_sent = True
            return None, IDLE_REPLY, 30000
        for message in body.get("messages", []):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            for text in _texts(message.get("content")):
                if text.startswith("<continuity-host-event>"):
                    self.continuations.add(core.digest(text))
        if len(self.continuations) >= self.cycles:
            self.done = True
            self.completion_response_id = f"msg_native_probe_{len(self.requests)}"
            return None, "NATIVE_CONTINUITY_DONE", 100
        self.batch_only_tool_calls.append("Read")
        response_id = f"msg_native_probe_{len(self.requests)}"
        self.read_response_ids.append(response_id)
        self.stage += 1
        return "Read", {"file_path": str(self.workspace / "batch-only.txt")}, 95000


def _alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _stop_fixture(runtime):
    state = runtime._state()
    binding = state["tmux"]
    tmux_transport.inspect(binding)
    subprocess.run(["tmux", "-S", binding["socket_path"], "kill-server"],
                   capture_output=True, check=True, timeout=10)
    deadline = time.monotonic() + 5
    while (_alive(binding["pane_pid"]) or _alive(state.get("controller_pid"))) and time.monotonic() < deadline:
        time.sleep(.05)
    return {"context_id": runtime.conversation_id, "pane_exited": not _alive(binding["pane_pid"]),
            "controller_exited": not _alive(state.get("controller_pid"))}


def _readable_boundary(runtime, state):
    path = state.get("source_path")
    if not path or not Path(path).is_file() or state["pending_tool_ids"]:
        return False
    source = HistorySource(Path(path), state["session_id"])
    records, incomplete = source._records(defer_incomplete_tail=True, _report_deferred_tail=True)
    return bool(records) and not incomplete and not source._activity_from_records(records)["pending_tools"]


def _collect(runtime, runtimes, completion_id):
    references = {}
    final_references = set()
    for owner in runtimes:
        for path in (owner.directory / "history").glob("*.json"):
            binding = core.read_json(path)
            key = (binding["session_id"], binding["source_path"])
            references[key] = binding
            if owner is runtime:
                final_references.add(key)
    root = []
    replies = Counter()
    responses = {}
    stop_events = []
    material_references = set()
    for (sid, path), binding in references.items():
        if not Path(path).is_file():
            continue
        source = HistorySource(Path(path), sid)
        records = source._records(defer_incomplete_tail=True)
        # An abandoned clear-only window contains native command/UI metadata,
        # not task history. Human, model, tool, and readable peer/task records matter.
        if any(row.kind in {"original_user", "assistant", "tool_result"}
               or row.kind == "meta" and (
                   row.data.get("type") == "user" and not row.data.get("isMeta")
                   or source.read(source.locator(row.message_id)).get("text"))
               for row in records):
            material_references.add((sid, path))
        for row in records:
            if row.kind == "original_user" and "\n".join(_texts(_content(row.data))) == ROOT_INPUT:
                root.append(source.locator(row.message_id))
            if row.kind == "assistant":
                response_id = row.data.get("message", {}).get("id")
                if isinstance(response_id, str) and response_id.startswith("msg_native_probe_"):
                    responses.setdefault(response_id, set()).add(sid)
            elif row.kind == "tool_result":
                replies.update(block["tool_use_id"] for block in _results(row.data)
                               if block["tool_use_id"].startswith("tool_native_probe_"))
        with Path(path).open() as handle:
            for line in handle:
                if not line.endswith("\n"):
                    continue
                item = json.loads(line).get("attachment", {})
                if item.get("type") == "hook_stopped_continuation" and item.get("hookEvent") == "PostToolBatch":
                    stop_events.append(sid)
    return {"root_locators": root, "tool_result_counts": dict(replies),
            "response_sessions": {key: sorted(value) for key, value in responses.items()},
            "completion_sessions": sorted(responses.get(completion_id, set())),
            "batch_stop_sessions": stop_events, "session_ids": sorted({sid for sid, _ in references}),
            "required_task_session_ids": sorted({sid for sid, _ in material_references}),
            "final_catalogue_contains_prior_sources": material_references <= final_references}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Probe repeated native rollover and recovery without external LLM calls.")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--restart-at", choices=("before-clear", "after-clear", "after-handoff", "controller"))
    args = parser.parse_args(argv)
    if not 2 <= args.cycles <= 5:
        parser.error("--cycles must be between 2 and 5 for this bounded fixture")
    home = Path.home() / ".claude" / ("ct-" + uuid4().hex[:8])
    home.mkdir(mode=0o700)
    workspace, temporary, native_config = (home / name for name in ("workspace", "tmp", "native-config"))
    for path in (workspace, temporary, native_config, workspace / ".claude"):
        path.mkdir()
    (workspace / "batch-only.txt").write_text("NATIVE_CONTINUITY_READ_RESULT\n")
    (workspace / ".claude/settings.local.json").write_text(json.dumps({"env": {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1"}}))
    (native_config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark",
        "projects": {str(workspace): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
    managed = args.restart_at == "controller"
    model = ContinuousModel(workspace, args.cycles, controller_restart=managed)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(model))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    values = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "ANTHROPIC_AUTH_TOKEN": "synthetic-local-fixture-token",
        "CLAUDE_CONTEXT_CONTINUITY_DIR": str(home), "CLAUDE_CONFIG_DIR": str(native_config),
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1",
        "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "TMPDIR": str(temporary), "CLAUDE_CODE_TMPDIR": str(temporary),
        "PYTHONPATH": str(ROOT / "src")}
    native_args = ("--setting-sources", "local", "--model", "claude-sonnet-4-6",
                   "--permission-mode", "default", "--allowedTools", "Read")
    previous_home = core.HOME
    runtimes, stopped = [], []
    runtime = None
    started = time.monotonic()
    report = {"status": "failed", "scope": "real native TUI/hooks/history with scripted localhost model and usage",
              "fixture_home": str(home), "cycles_requested": args.cycles, "restart_at": args.restart_at}
    try:
        with isolated_environment(values):
            core.HOME = home
            if managed:
                launched = tui_runtime.run(workspace, prompt=ROOT_INPUT, detached=True, native_args=(*native_args, "--"))
                runtime = tui_runtime.TuiRuntime.load(launched["context_id"])
            else:
                runtime = tui_runtime.create(workspace, prompt=ROOT_INPUT, width=140, height=40,
                                             native_args=(*native_args, "--"))
            runtimes.append(runtime)
            submitted = restarted = resume_command_sent = continued = False
            restart_target = old_controller = None
            observed_pauses = set()
            while time.monotonic() - started < 90:
                state = runtime._state()
                if state.get("initial_session_started"):
                    submitted = True
                if state["phase"] == "paused":
                    observed_pauses.add(state.get("diagnostic") or state.get("pause_reason") or "unknown")
                if model.failed:
                    break
                if submitted and not restarted and args.restart_at:
                    if managed:
                        if (state.get("stop_text_hash") == core.digest(IDLE_REPLY)
                                and _alive(state.get("controller_pid"))):
                            # 仅终止本探针已登记的控制器，保持原生终端和会话不变。
                            old_controller = state["controller_pid"]
                            os.kill(old_controller, signal.SIGTERM)
                            restart_target = state["session_id"]
                            restarted = True
                    else:
                        request, clear = state["rotation"]["request"], state["rotation"]["clear"]
                        cut = ((args.restart_at == "before-clear" and request and clear is None)
                               or (args.restart_at == "after-clear" and clear and clear.get("reset_seen")
                                   and not state.get("continuation_hash"))
                               or (args.restart_at == "after-handoff" and state.get("continuation_observed")
                                   and len(model.continuations) >= 1 and request and clear is None))
                        checkpoint = state
                        if args.restart_at == "after-clear" and request:
                            # 新空窗尚无工作记录：原生恢复选择最后有完整任务记录的旧窗口。
                            checkpoint = {"source_path": request["source_path"], "session_id": request["session_id"],
                                          "pending_tool_ids": state["pending_tool_ids"]}
                        if cut and _readable_boundary(runtime, checkpoint):
                            restart_target = checkpoint["session_id"]
                            stopped.append(_stop_fixture(runtime))
                            runtime = tui_runtime.create(workspace, width=140, height=40,
                                                         native_args=(*native_args, "--resume", restart_target))
                            runtimes.append(runtime)
                            restarted = True
                            continue
                if managed and restarted and not _alive(old_controller) and not resume_command_sent:
                    _submit_fixture_input(runtime, "/resume " + restart_target, keyboard=True)
                    resume_command_sent = True
                if (managed and resume_command_sent and not continued
                        and state.get("native_resume_confirmations", 0) >= 1
                        and state.get("controller_pid") != old_controller and _alive(state.get("controller_pid"))):
                    _submit_fixture_input(runtime, RESUME_INPUT, keyboard=True)
                    continued = True
                if not managed:
                    runtime.advance()
                state = runtime._state()
                if model.done and state.get("continuation_observed"):
                    evidence = _collect(runtime, runtimes, model.completion_response_id)
                    if evidence["completion_sessions"] == [state["session_id"]]:
                        break
                time.sleep(.05)
            state = runtime._state()
            evidence = _collect(runtime, runtimes, model.completion_response_id)
            roots = evidence["root_locators"]
            readback = None
            if len(roots) == 1:
                loc = roots[0]
                command = core.module_argv("history") + ["--source", loc["source_path"], "--session-id", loc["session_id"],
                    "--message-id", loc["message_id"], "--expected-sha256", loc["sha256"], "--limit", "500"]
                readback = json.loads(subprocess.check_output(command, text=True))
            source_bound = bool(readback and readback.get("source_kind") == "original_user" and readback.get("text") == ROOT_INPUT)
            controller_restored = (not managed or (continued and state.get("controller_pid") != old_controller
                                                   and _alive(state.get("controller_pid"))))
            resumed = (not args.restart_at or (restarted and state.get("native_resume_confirmations", 0) >= 1))
            expected_results = {response.replace("msg_native_probe_", "tool_native_probe_", 1): 1
                                for response in model.read_response_ids}
            reads_settled_once = (bool(expected_results) and evidence["tool_result_counts"] == expected_results
                                  and all(len(evidence["response_sessions"].get(response, [])) == 1
                                          for response in model.read_response_ids))
            success = (model.done and model.failed is None and state["phase"] != "paused"
                       and len(model.continuations) == args.cycles
                       and len(evidence["session_ids"]) >= args.cycles + 1
                       and len(evidence["batch_stop_sessions"]) >= args.cycles
                       and evidence["completion_sessions"] == [state["session_id"]]
                       and evidence["final_catalogue_contains_prior_sources"] and source_bound
                       and reads_settled_once and resumed and controller_restored)
            report.update(status="passed" if success else "failed", native_version=state.get("native_cli_version"),
                phase=state["phase"], waiting_reason=state.get("waiting_reason"), pause_reason=state.get("pause_reason"),
                diagnostic=state.get("diagnostic"), cycles_observed=len(model.continuations),
                observed_pause_reasons=sorted(observed_pauses),
                main_request_count=len(model.requests), completed_read_requests=len(model.batch_only_tool_calls),
                native_resume_confirmations=state.get("native_resume_confirmations", 0), restarted=restarted,
                restart_target=restart_target, controller_restored=controller_restored, old_controller_pid=old_controller,
                current_controller_pid=state.get("controller_pid"), original_input_exact_readback=source_bound,
                all_read_results_settled_once=reads_settled_once,
                evidence=evidence, fixture_failure=model.failed)
            if not success:
                (home / "probe-screen.txt").write_text(tmux_transport.capture(state["tmux"]))
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        if runtime is not None:
            try:
                stopped.append(_stop_fixture(runtime))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                report["cleanup_error"] = type(exc).__name__
        server.shutdown()
        server.server_close()
        core.HOME = previous_home
        report["fixture_processes"] = stopped
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["receipt_path"] = str(home / "receipt.json")
        (home / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" and all(item["pane_exited"] and item["controller_exited"] for item in stopped) else 1


if __name__ == "__main__":
    raise SystemExit(main())
