"""连续换窗与原生恢复探针；只使用新建隔离终端和 localhost 脚本模型。"""
from __future__ import annotations

import argparse
from collections import Counter
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
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
    def __init__(self, workspace, cycles, *, controller_restart=False, cwd_change=False, defer_cwd_rotation=False):
        super().__init__(workspace, batch_only=True)
        self.cycles = cycles
        self.controller_restart = controller_restart
        self.cwd_change = cwd_change
        self.defer_cwd_rotation = defer_cwd_rotation
        self.idle_sent = False
        self.continuations = set()
        self.read_response_ids = []
        self.tool_response_ids = []
        self.cwd_change_dispatched = False
        self.cwd_change_followup_dispatched = False
        self.cwd_change_response_id = None

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
        response_id = f"msg_native_probe_{len(self.requests)}"
        if self.cwd_change and not self.cwd_change_dispatched:
            # This ordinary relative cd is deliberately the first native tool.
            # The marker lives under the isolated workspace and proves that a
            # completed Bash tool was not replayed after the clear boundary.
            self.cwd_change_dispatched = True
            self.cwd_change_response_id = response_id
            self.tool_response_ids.append(response_id)
            self.batch_only_tool_calls.append("Bash")
            self.stage += 1
            return "Bash", {
                "command": "cd webui/frontend && " + shlex.join([sys.executable, "cwd-change-producer.py"]),
                "description": "Change to isolated frontend and write one marker",
            }, 30000 if self.defer_cwd_rotation else 95000
        # If the changed-cwd Bash did not cause a rotation, make one later real
        # batch boundary observable, then stop the local model rather than
        # generating an unbounded stream of unrelated tools.
        if self.cwd_change and not self.continuations and self.cwd_change_followup_dispatched:
            self.failed = "cwd_change_rotation_not_received"
            return None, "NATIVE_CWD_CHANGE_ROTATION_UNAVAILABLE", 100
        self.batch_only_tool_calls.append("Read")
        self.read_response_ids.append(response_id)
        self.tool_response_ids.append(response_id)
        if self.cwd_change and not self.continuations:
            self.cwd_change_followup_dispatched = True
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
    session_start_sessions = set()
    continuation_sessions = set()
    post_tool_use_success_sessions = set()
    material_references = set()
    source_bindings, native_pending_tools = [], []
    for (sid, path), binding in references.items():
        if not Path(path).is_file():
            continue
        source = HistorySource(Path(path), sid)
        records = source._records(defer_incomplete_tail=True)
        source_bindings.append({"session_id": sid, "source_path": str(source.path)})
        native_pending_tools.append({
            "session_id": sid, "source_path": str(source.path),
            "tool_ids": sorted(source._activity_from_records(records)["pending_tools"]),
        })
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
            if row.data.get("type") == "user" and any(
                    text.startswith("<continuity-host-event>") for text in _texts(_content(row.data))):
                continuation_sessions.add(sid)
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
                if item.get("type") == "hook_success" and item.get("hookEvent") == "SessionStart":
                    session_start_sessions.add(sid)
                if item.get("type") == "hook_success" and item.get("hookEvent") == "PostToolUse":
                    post_tool_use_success_sessions.add(sid)
    source_bindings.sort(key=lambda item: (item["session_id"], item["source_path"]))
    native_pending_tools.sort(key=lambda item: (item["session_id"], item["source_path"]))
    exact_source_bindings = (len(source_bindings) == len({
        (item["session_id"], item["source_path"]) for item in source_bindings
    }) and all(Path(item["source_path"]).name == f'{item["session_id"]}.jsonl' for item in source_bindings))
    return {"root_locators": root, "tool_result_counts": dict(replies),
            "response_sessions": {key: sorted(value) for key, value in responses.items()},
            "completion_sessions": sorted(responses.get(completion_id, set())),
            "batch_stop_sessions": stop_events, "session_start_sessions": sorted(session_start_sessions),
            "continuation_sessions": sorted(continuation_sessions),
            "post_tool_use_success_sessions": sorted(post_tool_use_success_sessions),
            "source_bindings": source_bindings, "exact_source_bindings": exact_source_bindings,
            "native_jsonl_pending_tools": native_pending_tools,
            "session_ids": sorted({sid for sid, _ in references}),
            "required_task_session_ids": sorted({sid for sid, _ in material_references}),
            "final_catalogue_contains_prior_sources": material_references <= final_references}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Probe repeated native rollover and recovery without external LLM calls.")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--restart-at", choices=("before-clear", "after-clear", "after-handoff", "controller"))
    parser.add_argument("--cwd-change", action="store_true",
                        help="Exercise one real Bash cd before automatic native rotation.")
    args = parser.parse_args(argv)
    if not 2 <= args.cycles <= 5:
        parser.error("--cycles must be between 2 and 5 for this bounded fixture")
    if args.cwd_change and args.restart_at not in {None, "before-clear"}:
        parser.error("--cwd-change supports only --restart-at before-clear")
    home = Path.home() / ".claude" / ("ct-" + uuid4().hex[:8])
    home.mkdir(mode=0o700)
    workspace, temporary, native_config = (home / name for name in ("workspace", "tmp", "native-config"))
    for path in (workspace, temporary, native_config, workspace / ".claude"):
        path.mkdir()
    (workspace / "batch-only.txt").write_text("NATIVE_CONTINUITY_READ_RESULT\n")
    (workspace / ".claude/settings.local.json").write_text(json.dumps({"env": {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1"}}))
    changed_cwd = workspace / "webui" / "frontend"
    marker_path = workspace / "cwd-change-runs.txt"
    if args.cwd_change:
        changed_cwd.mkdir(parents=True)
        (changed_cwd / ".claude").mkdir()
        # A child-local setting changes the observed configuration identity while
        # retaining the same safe native context window.
        (changed_cwd / ".claude/settings.local.json").write_text(json.dumps({
            "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "100000", "DISABLE_COMPACT": "1"},
            "autoCompactEnabled": False,
        }))
        (changed_cwd / "cwd-change-producer.py").write_text(
            "from pathlib import Path\n"
            "root = Path(__file__).resolve().parents[2]\n"
            "with (root / 'cwd-change-runs.txt').open('a', encoding='utf-8') as handle:\n"
            "    handle.write('1\\n')\n")
    (native_config / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark",
        "projects": {str(workspace): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
    managed = args.restart_at == "controller"
    model = ContinuousModel(workspace, args.cycles, controller_restart=managed, cwd_change=args.cwd_change,
                            defer_cwd_rotation=args.cwd_change and args.restart_at == "before-clear")
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
                   "--permission-mode", "default", "--allowedTools", "Bash,Read" if args.cwd_change else "Read")
    previous_home = core.HOME
    runtimes, stopped = [], []
    runtime = None
    started = time.monotonic()
    report = {"status": "failed", "scope": "real native TUI/hooks/history with scripted localhost model and usage",
              "fixture_home": str(home), "cycles_requested": args.cycles, "restart_at": args.restart_at,
              "cwd_change": args.cwd_change, "changed_cwd": str(changed_cwd) if args.cwd_change else None,
              "marker_path": str(marker_path) if args.cwd_change else None}
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
            initial_session_id = None
            observed_pauses = set()
            clear_pending_snapshots, observed_clear_sessions = [], set()
            changed_cwd_observations, observed_changed_cwd_states = [], set()

            def _observe_cwd_epochs(snapshot):
                if not args.cwd_change or not initial_session_id:
                    return
                sid = snapshot["session_id"]
                configuration = snapshot.get("configuration")
                copied_configuration = dict(configuration) if isinstance(configuration, dict) else configuration
                if snapshot.get("cwd") == str(changed_cwd):
                    key = (sid, snapshot.get("source_path"), snapshot.get("phase"),
                           copied_configuration.get("hash") if isinstance(copied_configuration, dict) else None)
                    if key not in observed_changed_cwd_states:
                        observed_changed_cwd_states.add(key)
                        changed_cwd_observations.append({
                            "session_id": sid, "source_path": snapshot.get("source_path"),
                            "phase": snapshot.get("phase"), "cwd": snapshot.get("cwd"),
                            "pending_tool_ids": list(snapshot["pending_tool_ids"]),
                            "configuration": copied_configuration,
                            "settings_changed_since_start": snapshot.get("settings_changed_since_start"),
                        })
                if (snapshot.get("native_clear_confirmations", 0) > 0 and sid != initial_session_id
                        and sid not in observed_clear_sessions):
                    observed_clear_sessions.add(sid)
                    clear_pending_snapshots.append({
                        "session_id": sid, "source_path": snapshot.get("source_path"),
                        "cwd": snapshot.get("cwd"), "pending_tool_ids": list(snapshot["pending_tool_ids"]),
                        "configuration": copied_configuration,
                        "settings_changed_since_start": snapshot.get("settings_changed_since_start"),
                    })

            while time.monotonic() - started < 90:
                state = runtime._state()
                if state.get("initial_session_started"):
                    submitted = True
                    initial_session_id = initial_session_id or state["session_id"]
                _observe_cwd_epochs(state)
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
                        if model.defer_cwd_rotation and state["session_id"] == initial_session_id:
                            # Hold only this fixture's driver until the post-cd
                            # response and its tool result are durably settled.
                            cut = bool(model.cwd_change_followup_dispatched and _readable_boundary(runtime, state)
                                       and HistorySource(Path(state["source_path"]), state["session_id"])
                                       .latest_usage()["cwd"] == str(changed_cwd))
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
                if not managed and not (model.defer_cwd_rotation and not restarted and model.cwd_change_dispatched):
                    runtime.advance()
                state = runtime._state()
                _observe_cwd_epochs(state)
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
                                for response in model.tool_response_ids}
            tools_settled_once = (bool(expected_results) and evidence["tool_result_counts"] == expected_results
                                  and all(len(evidence["response_sessions"].get(response, [])) == 1
                                          for response in model.tool_response_ids))
            session_ids = set(evidence["session_ids"])
            clear_sessions = set(evidence["session_start_sessions"]) - {initial_session_id}
            clear_snapshot_sessions = {item["session_id"] for item in clear_pending_snapshots}
            marker_runs = marker_path.read_text().splitlines() if marker_path.is_file() else []
            cwd_tool_id = (model.cwd_change_response_id.replace("msg_native_probe_", "tool_native_probe_", 1)
                           if model.cwd_change_response_id else None)
            changed_configuration = core.configuration(changed_cwd, live_window=True) if args.cwd_change else None
            changed_state_configuration = ({"hash": changed_configuration["hash"],
                                            "configured_window": core.configured_window(changed_configuration)}
                                           if changed_configuration else None)
            root_configuration = core.configuration(workspace, live_window=True) if args.cwd_change else None
            root_state_configuration = ({"hash": root_configuration["hash"],
                                         "configured_window": core.configured_window(root_configuration)}
                                        if root_configuration else None)
            initial_source_path = next((item["source_path"] for item in evidence["source_bindings"]
                                        if item["session_id"] == initial_session_id), None)
            source_identity_bound = (evidence["exact_source_bindings"]
                and {item["session_id"] for item in evidence["source_bindings"]} == session_ids
                and set(evidence["session_start_sessions"]) == session_ids
                and isinstance(state.get("source_path"), str) and Path(state["source_path"]).is_file()
                and any(item == {"session_id": state["session_id"], "source_path": state["source_path"]}
                        for item in evidence["source_bindings"]))
            native_pending_empty = all(not item["tool_ids"] for item in evidence["native_jsonl_pending_tools"])
            clear_history_confirmed = (initial_session_id in session_ids
                and len(clear_sessions) == args.cycles
                and state.get("native_clear_confirmations", 0) == args.cycles
                and set(evidence["continuation_sessions"]) == clear_sessions
                and clear_sessions <= session_ids)
            frontend_binding_observed = (not args.cwd_change or any(
                item["session_id"] == initial_session_id and item["source_path"] == initial_source_path
                and item["configuration"] == changed_state_configuration
                and item["settings_changed_since_start"] is True and not item["pending_tool_ids"]
                for item in changed_cwd_observations))
            clear_transition_pending_empty = (clear_snapshot_sessions == clear_sessions
                and all(not item["pending_tool_ids"] and isinstance(item["source_path"], str)
                        and Path(item["source_path"]).name == f'{item["session_id"]}.jsonl'
                        for item in clear_pending_snapshots))
            clear_session_binding_coherent = (not args.cwd_change or all(
                item["cwd"] == str(workspace) and item["configuration"] == root_state_configuration
                and item["settings_changed_since_start"] is False
                for item in clear_pending_snapshots))
            cwd_binding_config_coherent = (not args.cwd_change or (
                frontend_binding_observed and state.get("cwd") == str(workspace)
                and state.get("configuration") == root_state_configuration
                and state.get("startup_configuration", {}).get("hash") == root_configuration["hash"]
                and state.get("settings_changed_since_start") is False
                and clear_session_binding_coherent))
            stale_foreground_pending_absent = (not args.cwd_change or (
                not state["pending_tool_ids"] and native_pending_empty
                and (not cwd_tool_id or cwd_tool_id not in state["pending_tool_ids"])
                and clear_transition_pending_empty))
            resumed_changed_usage = (not model.defer_cwd_rotation or (
                restart_target == initial_session_id and initial_source_path is not None
                and HistorySource(Path(initial_source_path), initial_session_id).latest_usage()["cwd"] == str(changed_cwd)))
            cwd_change_verified = (not args.cwd_change or (
                model.cwd_change_dispatched and cwd_tool_id is not None and marker_runs == ["1"]
                and evidence["tool_result_counts"].get(cwd_tool_id) == 1
                and initial_session_id in set(evidence["post_tool_use_success_sessions"])
                and clear_history_confirmed and stale_foreground_pending_absent
                and cwd_binding_config_coherent and source_identity_bound and resumed_changed_usage))
            success = (model.done and model.failed is None and state["phase"] != "paused"
                       and len(model.continuations) == args.cycles
                       and len(evidence["session_ids"]) >= args.cycles + 1
                       and len(evidence["batch_stop_sessions"]) >= args.cycles
                       and evidence["completion_sessions"] == [state["session_id"]]
                       and evidence["final_catalogue_contains_prior_sources"] and source_bound
                       and tools_settled_once and resumed and controller_restored and cwd_change_verified)
            report.update(status="passed" if success else "failed", native_version=state.get("native_cli_version"),
                phase=state["phase"], waiting_reason=state.get("waiting_reason"), pause_reason=state.get("pause_reason"),
                diagnostic=state.get("diagnostic"), cycles_observed=len(model.continuations),
                observed_pause_reasons=sorted(observed_pauses),
                main_request_count=len(model.requests), completed_read_requests=len(model.read_response_ids),
                completed_tool_requests=len(model.batch_only_tool_calls),
                native_resume_confirmations=state.get("native_resume_confirmations", 0),
                native_clear_confirmations=state.get("native_clear_confirmations", 0), restarted=restarted,
                restart_target=restart_target, controller_restored=controller_restored, old_controller_pid=old_controller,
                current_controller_pid=state.get("controller_pid"), original_input_exact_readback=source_bound,
                all_read_results_settled_once=tools_settled_once,
                all_tool_results_settled_once=tools_settled_once,
                initial_session_id=initial_session_id, clear_pending_snapshots=clear_pending_snapshots,
                changed_cwd_observations=changed_cwd_observations,
                marker_runs=marker_runs, cwd_change_tool_id=cwd_tool_id,
                source_identity_bound=source_identity_bound,
                actual_clear_and_continuation_confirmed=clear_history_confirmed,
                frontend_binding_observed=frontend_binding_observed,
                clear_session_binding_coherent=clear_session_binding_coherent,
                stale_foreground_pending_absent=stale_foreground_pending_absent,
                cwd_binding_config_coherent=cwd_binding_config_coherent,
                cwd_change_verified=cwd_change_verified, resumed_changed_usage_verified=resumed_changed_usage,
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
