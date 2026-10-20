"""连续换窗与原生恢复探针；只使用新建隔离终端和 localhost 脚本模型。"""
from __future__ import annotations

import argparse
from collections import Counter
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
# The baseline run uses a private copy of the previously observed source; hooks
# and the controller must resolve that same source, not an installed package.
PROBE_SRC = Path(os.environ.get("NATIVE_CONTINUITY_PROBE_SRC", str(ROOT / "src"))).resolve()
sys.path.insert(0, str(PROBE_SRC))

from claude_context_continuity import core, native_control, tmux_transport, tui_runtime
from claude_context_continuity.history import HistorySource, _content, _results, _texts
from native_runtime_probe import ScriptedModel, _handler, _json_values, _submit_fixture_input, isolated_environment

ROOT_INPUT = "NATIVE_CONTINUITY_ROOT: complete the isolated read sequence; recover existing history, never replay completed tools."
RESUME_INPUT = "NATIVE_CONTINUITY_RESUME: continue that same isolated read sequence."
IDLE_REPLY = "NATIVE_CONTINUITY_IDLE_READY"
FILE_EDIT_INPUT = "NATIVE_FILE_EDIT_BUDGET: read and edit only the isolated fixture files; keep prior tool results."
EDIT_BEFORE = "NATIVE_EDIT_BEFORE"
EDIT_AFTER = "NATIVE_EDIT_AFTER"
WRITE_AFTER = "NATIVE_WRITE_AFTER\n"


class ContinuousModel(ScriptedModel):
    def __init__(self, workspace, cycles, *, controller_restart=False, cwd_change=False, defer_cwd_rotation=False,
                 file_edit_budget=False, near_limit=False):
        super().__init__(workspace, batch_only=True)
        self.cycles = cycles
        self.controller_restart = controller_restart
        self.cwd_change = cwd_change
        self.defer_cwd_rotation = defer_cwd_rotation
        self.file_edit_budget = file_edit_budget
        self.near_limit = near_limit
        self.idle_sent = False
        self.continuations = set()
        self.read_response_ids = []
        self.tool_response_ids = []
        self.model_requests = []
        self.model_validation_requests = []
        self.provider_result_bytes = {}
        self.cwd_change_dispatched = False
        self.cwd_change_followup_dispatched = False
        self.cwd_change_response_id = None

    def _batch_only_response(self, body):
        self.model_requests.append({"response_id": f"msg_native_probe_{len(self.requests)}",
                                    "model": body.get("model")})
        if self.controller_restart and not self.idle_sent:
            self.idle_sent = True
            return None, IDLE_REPLY, 30000
        for message in body.get("messages", []):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            for text in _texts(message.get("content")):
                if text.startswith("<continuity-host-event>"):
                    self.continuations.add(core.digest(text))
        if self.file_edit_budget:
            for value in _json_values(body.get("messages", [])):
                if (value.get("type") == "tool_result" and isinstance(value.get("tool_use_id"), str)
                        and value["tool_use_id"].startswith("tool_native_probe_")):
                    text = "\n".join(_texts(value.get("content")))
                    if text:
                        self.provider_result_bytes.setdefault(value["tool_use_id"], len(text.encode("utf-8")))
            if self.continuations and (not self.near_limit or self.stage != 3):
                self.failed = "premature_rotation_before_file_mutations_complete"
                return None, "NATIVE_FILE_EDIT_PREMATURE_ROTATION", 100
            if self.stage == 3:
                if self.near_limit and not self.continuations:
                    self.failed = "near_limit_rotation_missing"
                    return None, "NATIVE_FILE_EDIT_ROTATION_MISSING", 100
                self.done = True
                self.completion_response_id = f"msg_native_probe_{len(self.requests)}"
                return None, "NATIVE_FILE_EDIT_DONE", 96933 if not self.near_limit else 100
            response_id = f"msg_native_probe_{len(self.requests)}"
            action = ("Read", "Edit", "Write")[self.stage]
            self.stage += 1
            self.tool_response_ids.append(response_id)
            self.batch_only_tool_calls.append(action)
            if action == "Read":
                self.read_response_ids.append(response_id)
                value = {"file_path": str(self.workspace / "large-edit.txt"), "limit": 40}
            elif action == "Edit":
                value = {"file_path": str(self.workspace / "large-edit.txt"),
                         "old_string": EDIT_BEFORE, "new_string": EDIT_AFTER}
            else:
                value = {"file_path": str(self.workspace / "large-write.txt"), "content": WRITE_AFTER}
            return action, value, 550000 if self.near_limit and action == "Write" else 96933
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


def _model_switch_handler(model):
    streamed = _handler(model)

    class Handler(streamed):
        def do_POST(self):
            if "count_tokens" in self.path:
                return super().do_POST()
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw)
            if body.get("stream") is True:
                # Reuse the existing streaming script unchanged. This wrapper
                # only handles the native /model command's non-streaming probe.
                original = self.rfile
                self.rfile = io.BytesIO(raw)
                try:
                    return super().do_POST()
                finally:
                    self.rfile = original
            model.model_validation_requests.append({"model": body.get("model"), "stream": body.get("stream")})
            response = {"id": "msg_native_model_validation", "type": "message", "role": "assistant",
                        "model": body.get("model"), "content": [{"type": "text", "text": "Isolated model ID validation"}],
                        "stop_reason": "end_turn", "stop_sequence": None,
                        "usage": {"input_tokens": 100, "output_tokens": 100,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


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


def _collect(runtime, runtimes, completion_id, *, root_input=ROOT_INPUT):
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
    original_user_texts = []
    replies = Counter()
    responses = {}
    response_models = {}
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
            if row.kind == "original_user":
                text = "\n".join(_texts(_content(row.data)))
                original_user_texts.append(text)
                if text == root_input:
                    root.append(source.locator(row.message_id))
            if row.data.get("type") == "user" and any(
                    text.startswith("<continuity-host-event>") for text in _texts(_content(row.data))):
                continuation_sessions.add(sid)
            if row.kind == "assistant":
                response_id = row.data.get("message", {}).get("id")
                if isinstance(response_id, str) and response_id.startswith("msg_native_probe_"):
                    responses.setdefault(response_id, set()).add(sid)
                    response_models.setdefault(response_id, set()).add(row.data["message"].get("model"))
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
    return {"root_locators": root, "original_user_texts": original_user_texts,
            "tool_result_counts": dict(replies),
            "response_sessions": {key: sorted(value) for key, value in responses.items()},
            "response_models": {key: sorted(value, key=str) for key, value in response_models.items()},
            "completion_sessions": sorted(responses.get(completion_id, set())),
            "batch_stop_sessions": stop_events, "session_start_sessions": sorted(session_start_sessions),
            "continuation_sessions": sorted(continuation_sessions),
            "post_tool_use_success_sessions": sorted(post_tool_use_success_sessions),
            "source_bindings": source_bindings, "exact_source_bindings": exact_source_bindings,
            "native_jsonl_pending_tools": native_pending_tools,
            "session_ids": sorted({sid for sid, _ in references}),
            "required_task_session_ids": sorted({sid for sid, _ in material_references}),
            "final_catalogue_contains_prior_sources": material_references <= final_references}


def _confirm_native_model_dialog(runtime, model):
    state = runtime._state()
    screen = tmux_transport.capture(state["tmux"])
    if "Switch model?" not in screen or f"Yes, switch to {model}" not in screen:
        return False
    # Native /model may require a cache-switch confirmation. Press the selected
    # Yes button only in this newly created fixture pane; never change settings
    # or bypass the native validation request.
    binding = state["tmux"]
    subprocess.run(["tmux", "-S", binding["socket_path"], "send-keys", "-t", binding["pane_id"], "Enter"],
                   capture_output=True, check=True, timeout=5)
    return True


def _approve_fixture_mutation(runtime, action):
    """Accept only this fixture's exact one-time native Edit/Write prompt (option 1)."""
    basename = "large-edit.txt" if action == "Edit" else "large-write.txt"
    state = runtime._state()
    if f"tool_native_probe_{2 if action == 'Edit' else 3}" not in state["pending_tool_ids"]:
        return False
    screen = tmux_transport.capture(state["tmux"])
    if (basename not in screen or "❯ 1. Yes" not in screen
            or not any(label in screen for label in ("Edit file", "Write file", "Overwrite file", "Create file"))):
        return False
    if action == "Edit" and f"Do you want to make this edit to {basename}?" not in screen:
        return False
    # Never choose the persistent 'Yes, and allow ...' option; only authorize the
    # exact scripted mutation in the pane and workspace just created by this run.
    binding = state["tmux"]
    tmux_transport.inspect(binding)
    subprocess.run(["tmux", "-S", binding["socket_path"], "send-keys", "-t", binding["pane_id"], "Enter"],
                   capture_output=True, check=True, timeout=5)
    return True


def _only_fixture_input(texts, model):
    command = re.compile(r"<command-message>model</command-message>\s*"
                         r"<command-name>/model</command-name>\s*"
                         r"<command-args>" + re.escape(model) + r"</command-args>\s*")
    return texts.count(ROOT_INPUT) == 1 and all(
        text == ROOT_INPUT or text == f"/model {model}" or command.fullmatch(text)
        for text in texts)


def _file_edit_evidence(runtime, runtimes, model, initial_sid, original, state, evidence, *, near_limit,
                        premature_rotation, edit_usage_snapshot, rotation_details, approved_mutations):
    """Summarize native tool content without copying the 550KB hook metadata to a receipt."""
    workspace = model.workspace
    edit_path, write_path = workspace / "large-edit.txt", workspace / "large-write.txt"
    tool_ids = {name: response.replace("msg_native_probe_", "tool_native_probe_", 1)
                for name, response in zip(model.batch_only_tool_calls, model.tool_response_ids)}
    receipts, persisted_original_bytes, response_sessions = {}, {}, {}
    for owner in runtimes:
        for binding_path in (owner.directory / "history").glob("*.json"):
            binding = core.read_json(binding_path)
            source = HistorySource(Path(binding["source_path"]), binding["session_id"])
            for row in source._records(defer_incomplete_tail=True):
                if row.kind != "tool_result":
                    continue
                for block in _results(row.data):
                    for name, tool_id in tool_ids.items():
                        if block["tool_use_id"] != tool_id:
                            continue
                        content = block.get("content")
                        text = "\n".join(_texts(content))
                        receipts[name] = {"model_text_bytes": len(text.encode("utf-8")),
                                          "native_result_present": bool(text),
                                          "read_marker_present": EDIT_BEFORE in text if name == "Read" else None,
                                          "is_error": block.get("is_error", False)}
                        metadata = row.data.get("toolUseResult")
                        original_file = metadata.get("originalFile") if isinstance(metadata, dict) else None
                        persisted_original_bytes[name] = (len(original_file.encode("utf-8"))
                                                          if isinstance(original_file, str) else None)
                        receipts[name]["native_original_excluded_from_model"] = (
                            metadata.get("contentNotInModelContext") is True if isinstance(metadata, dict) else False)
                        response_sessions.setdefault(name, set()).add(binding["session_id"])
    result_counts = evidence["tool_result_counts"]
    tools_once = (model.batch_only_tool_calls == ["Read", "Edit", "Write"]
                  and len(tool_ids) == 3
                  and all(result_counts.get(tool_id) == 1 for tool_id in tool_ids.values())
                  and set(result_counts) == set(tool_ids.values())
                  and all(len(response_sessions.get(name, set())) == 1 for name in tool_ids))
    edit_contents_ok = (edit_path.is_file() and edit_path.read_text() == original.replace(EDIT_BEFORE, EDIT_AFTER, 1)
                        and original.count(EDIT_BEFORE) == 1)
    write_contents_ok = write_path.is_file() and write_path.read_text() == WRITE_AFTER
    native_outputs_kept = (evidence["final_catalogue_contains_prior_sources"]
                           and all(receipts.get(name, {}).get("native_result_present")
                                   and not receipts[name]["is_error"] for name in tool_ids)
                           and receipts.get("Read", {}).get("read_marker_present") is True)
    provider_edit_bytes = model.provider_result_bytes.get(tool_ids.get("Edit"))
    receipt_small = (0 < receipts.get("Edit", {}).get("model_text_bytes", 0) < 4096
                     and 0 < receipts.get("Write", {}).get("model_text_bytes", 0) < 4096
                     and provider_edit_bytes is not None and 0 < provider_edit_bytes < 4096)
    expected_sessions = 2 if near_limit else 1
    rotated_normally = (state["session_id"] != initial_sid and state.get("continuation_observed")
                        and state.get("native_clear_confirmations") == 1
                        and len(model.continuations) == 1 and not state["rotation"]["request"]
                        and rotation_details is not None
                        and rotation_details.get("input_and_cache_tokens") == 550000)
    no_rotation = (state["session_id"] == initial_sid and not state["rotation"]["request"]
                   and state.get("native_clear_confirmations", 0) == 0
                   and not model.continuations and state.get("automatic_rotations", 0) == 0)
    final_usage = state.get("usage") or {}
    final_usage_bound = final_usage.get("request_id") == model.completion_response_id
    passed = (model.done and model.failed is None and not premature_rotation and tools_once and final_usage_bound
              and approved_mutations == {"Edit", "Write"}
              and native_outputs_kept and edit_contents_ok and write_contents_ok and receipt_small
              and (receipts.get("Edit", {}).get("native_original_excluded_from_model") is True
                   or persisted_original_bytes.get("Edit") is not None
                   and persisted_original_bytes["Edit"] > 500_000)
              and len(evidence["session_ids"]) == expected_sessions
              and (rotated_normally if near_limit else no_rotation))
    return {"status": "passed" if passed else "failed", "file_edit_budget_verified": passed,
            "fixture_failure": model.failed, "premature_rotation_before_write": premature_rotation,
            "one_time_native_file_approvals": sorted(approved_mutations),
            "rotation_details": rotation_details, "edit_usage_snapshot": edit_usage_snapshot,
            "scripted_usage_input_tokens": [96933, 96933, 550000 if near_limit else 96933],
            "configured_window_tokens": 600000, "effective_window_tokens": final_usage.get("native_context_window"),
            "final_native_usage_bound": final_usage_bound,
            "guard_tokens": state.get("budget", {}).get("guard_tokens"),
            "initial_file_bytes": len(original.encode("utf-8")), "native_tool_result_receipts": receipts,
            "provider_seen_tool_result_bytes": {name: model.provider_result_bytes.get(tool_id)
                                                for name, tool_id in tool_ids.items()},
            "persisted_original_file_bytes": persisted_original_bytes, "tool_result_counts": result_counts,
            "tools_settled_once": tools_once, "edit_contents_correct": edit_contents_ok,
            "write_contents_correct": write_contents_ok, "native_outputs_kept": native_outputs_kept,
            "receipt_small": receipt_small, "rotation_expected": near_limit,
            "rotation_observed": rotated_normally if near_limit else not no_rotation,
            "native_clear_confirmations": state.get("native_clear_confirmations", 0),
            "continuation_observed": state.get("continuation_observed", False),
            "session_count": len(evidence["session_ids"]),
            "source_sha256": core.sha(Path(tui_runtime.__file__)),
            "source_path": str(Path(tui_runtime.__file__).resolve())}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Probe repeated native rollover and recovery without external LLM calls.")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--restart-at", choices=("before-clear", "after-clear", "after-handoff", "controller"))
    parser.add_argument("--cwd-change", action="store_true",
                        help="Exercise one real Bash cd before automatic native rotation.")
    parser.add_argument("--initial-model", default="claude-sonnet-4-6",
                        help="Native CLI --model name (localhost scripted responses only).")
    parser.add_argument("--switch-model", help="Send /model once after the first budget-stopped Read, before renewal.")
    parser.add_argument("--file-edit-budget", action="store_true",
                        help="Run one native 550KB Read -> Edit -> Write output-accounting regression.")
    parser.add_argument("--near-limit", action="store_true",
                        help="In the file-edit fixture, raise native usage after Write to trigger one normal rotation.")
    args = parser.parse_args(argv)
    if args.near_limit and not args.file_edit_budget:
        parser.error("--near-limit requires --file-edit-budget")
    if args.file_edit_budget and (args.switch_model or args.restart_at or args.cwd_change or args.cycles != 3):
        parser.error("--file-edit-budget is a separate bounded scenario with no cycles/restart/model switch")
    for name in (args.initial_model, args.switch_model):
        if name is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name):
            parser.error("model names must be bounded single tokens, not native commands")
    if args.switch_model and args.switch_model == args.initial_model:
        parser.error("--switch-model must differ from --initial-model")
    if args.switch_model and (args.restart_at or args.cwd_change):
        parser.error("--switch-model is a separate bounded scenario from recovery and cwd-change")
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
    large_content = None
    if args.file_edit_budget:
        large_content = EDIT_BEFORE + "\n" + ("NATIVE_FILE_PADDING:" + "x" * 240 + "\n") * 2110
        for name in ("large-edit.txt", "large-write.txt"):
            (workspace / name).write_text(large_content)
    window = "600000" if args.file_edit_budget else "100000"
    (workspace / ".claude/settings.local.json").write_text(json.dumps({"env": {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": window, "DISABLE_COMPACT": "1"}}))
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
                            defer_cwd_rotation=args.cwd_change and args.restart_at == "before-clear",
                            file_edit_budget=args.file_edit_budget, near_limit=args.near_limit)
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 _model_switch_handler(model) if args.switch_model else _handler(model))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    values = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "ANTHROPIC_AUTH_TOKEN": "synthetic-local-fixture-token",
        "CLAUDE_CONTEXT_CONTINUITY_DIR": str(home), "CLAUDE_CONFIG_DIR": str(native_config),
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": window, "DISABLE_COMPACT": "1",
        "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "TMPDIR": str(temporary), "CLAUDE_CODE_TMPDIR": str(temporary),
        "PYTHONPATH": str(PROBE_SRC)}
    allowed_tools = "Read,Edit,Write" if args.file_edit_budget else "Bash,Read" if args.cwd_change else "Read"
    native_args = ("--setting-sources", "local", "--model", args.initial_model,
                   "--permission-mode", "default", "--allowedTools", allowed_tools)
    previous_home = core.HOME
    runtimes, stopped = [], []
    runtime = None
    started = time.monotonic()
    report = {"status": "failed", "scope": "real native TUI/hooks/history with scripted localhost model and usage",
              "fixture_home": str(home), "cycles_requested": int(args.near_limit) if args.file_edit_budget else args.cycles,
              "restart_at": args.restart_at,
              "cwd_change": args.cwd_change, "changed_cwd": str(changed_cwd) if args.cwd_change else None,
              "marker_path": str(marker_path) if args.cwd_change else None,
              "initial_model": args.initial_model, "switch_model": args.switch_model,
              "file_edit_budget": args.file_edit_budget, "near_limit": args.near_limit}
    initial_input = FILE_EDIT_INPUT if args.file_edit_budget else ROOT_INPUT
    try:
        with isolated_environment(values):
            core.HOME = home
            if managed:
                launched = tui_runtime.run(workspace, prompt=initial_input, detached=True, native_args=(*native_args, "--"))
                runtime = tui_runtime.TuiRuntime.load(launched["context_id"])
            else:
                runtime = tui_runtime.create(workspace, prompt=initial_input, width=140, height=40,
                                             native_args=(*native_args, "--"))
            runtimes.append(runtime)
            submitted = restarted = resume_command_sent = continued = False
            switch_sent = switch_confirmed = False
            restart_target = old_controller = None
            initial_session_id = None
            observed_waits = set()
            clear_pending_snapshots, observed_clear_sessions = [], set()
            changed_cwd_observations, observed_changed_cwd_states = [], set()
            premature_rotation = False
            edit_usage_snapshot = None
            rotation_details = None
            approved_fixture_mutations = set()

            def _observe_edit_usage(snapshot):
                nonlocal premature_rotation, edit_usage_snapshot, rotation_details
                if not args.file_edit_budget:
                    return
                usage = snapshot.get("usage") or {}
                if edit_usage_snapshot is None and usage.get("request_id") == "msg_native_probe_2":
                    edit_usage_snapshot = {"input_and_cache_tokens": usage.get("total_input_and_cache_tokens"),
                                           "output_tokens": usage.get("output_tokens"),
                                           "effective_window_tokens": usage.get("native_context_window")}
                request = snapshot["rotation"]["request"]
                if request and rotation_details is None:
                    rotation_details = json.loads(request["notes"])
                if request and len(model.tool_response_ids) < 3:
                    premature_rotation = True

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

            while time.monotonic() - started < (45 if args.switch_model else 90):
                state = runtime._state()
                if state.get("initial_session_started"):
                    submitted = True
                    initial_session_id = initial_session_id or state["session_id"]
                _observe_cwd_epochs(state)
                _observe_edit_usage(state)
                if runtime._waiting(state):
                    observed_waits.add(state["reconciliation"]["reason"])
                if model.failed or premature_rotation or (args.file_edit_budget and not args.near_limit
                                                            and state["rotation"]["request"]):
                    break
                if args.file_edit_budget:
                    for action in ("Edit", "Write"):
                        if action not in approved_fixture_mutations and _approve_fixture_mutation(runtime, action):
                            approved_fixture_mutations.add(action)
                if args.switch_model and not switch_sent:
                    # The first real Read is settled and PostToolBatch has stopped
                    # the next model request. Hold this fixture's advance only;
                    # the native /model command goes through the same authenticated
                    # background channel as the existing /clear path.
                    if (submitted and state["session_id"] == initial_session_id
                            and len(model.read_response_ids) == 1
                            and state["rotation"]["request"] and state.get("at_turn_boundary")
                            and _readable_boundary(runtime, state)):
                        native_control._send(state["native_control"], runtime._control_auth,
                                             "/model " + args.switch_model)
                        switch_sent = True
                    else:
                        time.sleep(.05)
                        continue
                if args.switch_model and switch_sent and not switch_confirmed:
                    if _confirm_native_model_dialog(runtime, args.switch_model):
                        switch_confirmed = True
                    else:
                        time.sleep(.1)
                        continue
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
                _observe_edit_usage(state)
                if args.file_edit_budget and (premature_rotation or not args.near_limit
                                              and state["rotation"]["request"]):
                    break
                if model.done and (state.get("continuation_observed") or
                                   args.file_edit_budget and not args.near_limit and _readable_boundary(runtime, state)):
                    evidence = _collect(runtime, runtimes, model.completion_response_id, root_input=initial_input)
                    if (evidence["completion_sessions"] == [state["session_id"]]
                            and (not args.file_edit_budget or
                                 (state.get("usage") or {}).get("request_id") == model.completion_response_id)):
                        break
                time.sleep(.05)
            state = runtime._state()
            evidence = _collect(runtime, runtimes, model.completion_response_id, root_input=initial_input)
            roots = evidence["root_locators"]
            readback = None
            if len(roots) == 1:
                loc = roots[0]
                command = core.module_argv("history") + ["--source", loc["source_path"], "--session-id", loc["session_id"],
                    "--message-id", loc["message_id"], "--expected-sha256", loc["sha256"], "--limit", "500"]
                readback = json.loads(subprocess.check_output(command, text=True))
            source_bound = bool(readback and readback.get("source_kind") == "original_user" and readback.get("text") == initial_input)
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
            model_transport_verified = model_history_verified = no_registration_input = False
            model_switch_verified = True
            if args.switch_model:
                observed = model.model_requests
                model_transport_verified = (switch_sent and switch_confirmed
                    and len(model.model_validation_requests) == 1
                    and model.model_validation_requests[0]["model"] == args.switch_model
                    and model.model_validation_requests[0]["stream"] is not True
                    and len(observed) == args.cycles + 1
                    and [item["model"] for item in observed]
                        == [args.initial_model] + [args.switch_model] * args.cycles)
                model_history_verified = (model_transport_verified and all(
                    evidence["response_models"].get(item["response_id"]) == [item["model"]]
                    and (evidence["response_sessions"].get(item["response_id"]) == [initial_session_id]
                         if index == 0 else len(evidence["response_sessions"].get(item["response_id"], [])) == 1
                         and evidence["response_sessions"][item["response_id"]][0] in clear_sessions)
                    for index, item in enumerate(observed)))
                no_registration_input = _only_fixture_input(evidence["original_user_texts"], args.switch_model)
                model_switch_verified = (model_history_verified and no_registration_input
                    and clear_history_confirmed and source_identity_bound and native_pending_empty
                    and len(session_ids) == args.cycles + 1
                    and len(model.read_response_ids) == args.cycles)
            success = (model.done and model.failed is None and state["phase"] == "running"
                       and not runtime._waiting(state)
                       and len(model.continuations) == args.cycles
                       and len(evidence["session_ids"]) >= args.cycles + 1
                       and len(evidence["batch_stop_sessions"]) >= args.cycles
                       and evidence["completion_sessions"] == [state["session_id"]]
                       and evidence["final_catalogue_contains_prior_sources"] and source_bound
                       and tools_settled_once and resumed and controller_restored and cwd_change_verified
                       and model_switch_verified)
            report.update(status="passed" if success else "failed", native_version=state.get("native_cli_version"),
                phase=state["phase"], reconciliation=state.get("reconciliation"),
                diagnostic=state.get("diagnostic"), cycles_observed=len(model.continuations),
                observed_wait_reasons=sorted(observed_waits),
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
            if args.switch_model:
                report.update(model_command_sent=switch_sent, native_dialog_confirmed=switch_confirmed,
                              model_requests=model.model_requests,
                              model_validation_requests=model.model_validation_requests,
                              model_transport_verified=model_transport_verified,
                              model_history_verified=model_history_verified,
                              no_registration_input=no_registration_input,
                              model_switch_verified=model_switch_verified)
            if args.file_edit_budget:
                report.update(_file_edit_evidence(runtime, runtimes, model, initial_session_id, large_content,
                                                  state, evidence, near_limit=args.near_limit,
                                                  premature_rotation=premature_rotation,
                                                  edit_usage_snapshot=edit_usage_snapshot,
                                                  rotation_details=rotation_details,
                                                  approved_mutations=approved_fixture_mutations))
                report["original_input_exact_readback"] = source_bound
                report["actual_clear_and_continuation_confirmed"] = (
                    args.near_limit and report["rotation_observed"] and report["continuation_observed"])
                if not source_bound:
                    report["status"] = "failed"
                success = report["status"] == "passed"
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
