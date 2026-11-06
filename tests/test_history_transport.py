"""Keep SQL maintenance out of the live hook process without duplicating parsing."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import continuity, core, history, history_transport, hook_transport, native_control
from claude_context_continuity.history import HistoryError, HistorySource
from claude_context_continuity.history_index import HistoryIndex, HistoryIndexStaleError
from claude_context_continuity.tui_runtime import TuiRuntime

AUTH = "isolated-projection-credential-123456"


@pytest.fixture
def runtime(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ccc-project-", dir="/tmp") as directory:
        home = Path(directory).resolve()
        monkeypatch.setattr(core, "HOME", home)
        monkeypatch.setenv(core.STATE_DIRECTORY_ENV, str(home))
        monkeypatch.setenv(native_control.HOOK_AUTH_ENV, AUTH)
        sid = str(uuid4())
        config = {"hash": "fixture-config", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        runtime = TuiRuntime.create(cwd=home, session_id=sid, configuration=config,
                                    configuration_reader=lambda _: config)
        source = home / f"{sid}.jsonl"
        source.write_text(json.dumps({"type": "user", "uuid": "user-1", "sessionId": sid,
            "message": {"role": "user", "content": "needle authorized fixture"}}) + "\n")
        runtime.on_hook({"hook_event_name": "SessionStart", "source": "startup", "session_id": sid,
                         "cwd": str(home), "transcript_path": str(source)})
        monkeypatch.setenv("CLAUDE_CONTINUITY_ID", runtime.conversation_id)
        yield runtime


def test_slow_sql_maintenance_does_not_block_native_hooks(runtime):
    state = runtime._state()
    binding = {"source_path": state["source_path"], "session_id": state["session_id"], "generation": 0}
    cache = runtime.directory / ".cache" / "history.sqlite3"
    with HistoryIndex(cache) as index:
        index.search([binding], "needle", secrets=("earlier-fixture-secret",))
    actions = []

    def handle(event):
        actions.append(event.get("_cclaude_action", event.get("hook_event_name")))
        return continuity._dispatch_controller_event(runtime, event)

    code = """import json,sys
sys.path.insert(0,sys.argv[1])
from claude_context_continuity import continuity
from claude_context_continuity.history_index import HistoryIndex
original=HistoryIndex._vacuum_after_redaction_change
def paused_vacuum(self):
    print('sql-maintenance-paused',flush=True)
    assert sys.stdin.readline().strip()=='resume'
    original(self)
HistoryIndex._vacuum_after_redaction_change=paused_vacuum
args=continuity.parser().parse_args(['history-search','--query','needle'])
print(json.dumps(continuity.dispatch(args)))
"""
    with hook_transport.HookServer(runtime.conversation_id, AUTH, handle) as server:
        stopped = threading.Event()

        def serve():
            while not stopped.is_set():
                server.serve_one(0.05)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=os.environ.copy())
        try:
            ready, _, _ = select.select([process.stdout], [], [], 5)
            assert ready, "search never reached client-side SQL maintenance"
            assert process.stdout.readline().strip() == "sql-maintenance-paused"
            hook = {"hook_event_name": "PreToolUse", "session_id": state["session_id"],
                    "cwd": state["cwd"], "transcript_path": state["source_path"],
                    "tool_name": "Read", "tool_use_id": "unrelated-hook"}
            result = hook_transport.request(runtime.conversation_id, hook)
            assert isinstance(result, dict)
            assert process.poll() is None
            assert actions == ["history-projection", "PreToolUse"]
        finally:
            output, error = process.communicate("resume\n", timeout=10)
            stopped.set()
            thread.join(5)
        assert not thread.is_alive()
        assert process.returncode == 0, error
        assert len(json.loads(output)["entries"]) == 1
        assert not hasattr(runtime, "_history_index")


@pytest.mark.parametrize("changed", ["source_path", "session_id"])
def test_projection_rejects_a_changed_current_binding(runtime, changed):
    state = runtime._state()
    event = {"source_path": state["source_path"], "session_id": state["session_id"], "known_secrets": []}
    event[changed] = str(uuid4())
    with pytest.raises(HistoryError, match="binding changed"):
        history_transport.project_current(runtime, event)


def test_projector_uses_local_history_for_old_windows_and_propagates_transport_failure(runtime, monkeypatch):
    state = runtime._state()
    with hook_transport.HookServer(runtime.conversation_id, AUTH, lambda _: {}) as server:
        projector = history_transport.current_projector(runtime.conversation_id)
        assert projector is not None
        other_id = str(uuid4())
        other_path = core.HOME / f"{other_id}.jsonl"
        other_path.write_text("")
        monkeypatch.setattr(hook_transport, "request", lambda *_: pytest.fail("old source used controller"))
        assert projector(HistorySource(other_path, other_id), ()) is None

        def fail(*_):
            raise hook_transport.HookExecutionError("HistoryError", "fixture parsing failure")

        monkeypatch.setattr(hook_transport, "request", fail)
        source = HistorySource(Path(state["source_path"]), state["session_id"])
        with pytest.raises(hook_transport.HookExecutionError, match="fixture parsing failure"):
            projector(source, ())


def test_projected_source_must_still_match_its_file_signature(runtime):
    state = runtime._state()
    path = Path(state["source_path"])

    def mutate(source, secrets):
        projection = source.index_projection(secrets=secrets)
        with path.open("a") as stream:
            stream.write(json.dumps({"type": "user", "uuid": "user-2", "sessionId": state["session_id"],
                                    "message": {"role": "user", "content": "new record"}}) + "\n")
        return {**projection, "reading_work": source.reading_work}

    with HistoryIndex(runtime.directory / ".cache" / "mutated.sqlite3", projector=mutate) as index:
        with pytest.raises(HistoryIndexStaleError, match="changed while"):
            index.search([{"source_path": str(path), "session_id": state["session_id"], "generation": 0}], "needle")


def _row(runtime, message_id, content, **extra):
    return {"type": "user", "uuid": message_id, "sessionId": runtime._state()["session_id"],
            "message": {"role": "user", "content": content}, **extra}


def _append(runtime, *rows):
    with Path(runtime._state()["source_path"]).open("a") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)


def _cli_search(runtime, *, env=None, setup="", success=True):
    code = """import runpy,sys
sys.path.insert(0,sys.argv[1])
exec(sys.argv[2])
sys.argv=['cclaude','history-search','--query','needle','--page-size','20']
runpy.run_module('claude_context_continuity',run_name='__main__')
"""
    process = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src"), setup],
                             env=os.environ.copy() if env is None else env,
                             capture_output=True, text=True, timeout=15)
    assert process.returncode == (0 if success else 2), process.stderr + process.stdout
    return json.loads(process.stdout)


@contextmanager
def _serving(runtime, *, forbidden=()):
    replies = []

    def handle(event):
        result = continuity._dispatch_controller_event(runtime, event)
        if event.get("_cclaude_action") == "history-projection":
            wire = (json.dumps({"result": result}, ensure_ascii=False) + "\n").encode("utf-8")
            assert all(value.encode("utf-8") not in wire for value in forbidden)
            replies.append({"entries": len(result["entries"]), "removed": len(result["removed_start_bytes"]),
                            "reset": result["reset"], "revision": result["revision"], "bytes": len(wire)})
        return result

    with hook_transport.HookServer(runtime.conversation_id, AUTH, handle) as server:
        stopped = threading.Event()
        thread = threading.Thread(target=lambda: _serve_until(server, stopped), daemon=True)
        thread.start()
        try:
            yield replies
        finally:
            stopped.set()
            thread.join(5)
            assert not thread.is_alive()


def _serve_until(server, stopped):
    while not stopped.is_set():
        server.serve_one(0.02)


def _observe(runtime):
    state = runtime._state()
    return runtime._history_source(state["source_path"], state["session_id"]).index_projection(secrets=core.secret_values())


def test_separate_real_cli_processes_receive_deltas_across_intervening_observations(runtime):
    _append(runtime, *(_row(runtime, f"background-{i}", "historical public text " * 12) for i in range(256)))
    with _serving(runtime) as replies:
        cold = _cli_search(runtime)
        _append(runtime, _row(runtime, "append-1", "needle first append"))
        _observe(runtime)
        _append(runtime, _row(runtime, "append-2", "needle second append"))
        _observe(runtime)
        first = _cli_search(runtime)
        _append(runtime, _row(runtime, "append-3", "needle third append"))
        _observe(runtime)
        second = _cli_search(runtime)
        unchanged = _cli_search(runtime)
    assert [reply["entries"] for reply in replies] == [257, 2, 1]
    assert [reply["reset"] for reply in replies] == [True, False, False]
    for result, reply in zip((cold, first, second), replies):
        assert result["refresh"]["transmitted_entries"] == reply["entries"]
        assert result["refresh"]["transmitted_bytes"] == reply["bytes"]
    assert replies[1]["bytes"] < replies[0]["bytes"] / 20
    assert replies[2]["bytes"] < replies[0]["bytes"] / 20
    assert len(first["entries"]) == 3 and len(second["entries"]) == 4
    assert unchanged["refresh"]["transmitted_entries"] == unchanged["refresh"]["transmitted_bytes"] == 0
    print(json.dumps({"scenario": "real separate CLI / two appends / intervening observations / unchanged",
                      "socket_replies": replies, "unchanged": unchanged["refresh"]}, sort_keys=True))


@pytest.mark.parametrize("reset", ["projection", "stale_revision", "adjacent_future_revision", "missing_baseline", "signature"])
def test_ipc_baseline_reset_is_an_explicit_full_replacement(runtime, reset):
    with _serving(runtime) as replies:
        _cli_search(runtime)
        if reset == "projection":
            source = runtime._history_source(runtime._state()["source_path"], runtime._state()["session_id"])
            object.__setattr__(source, "_projection", None)
        else:
            cache = runtime.directory / ".cache" / "history.sqlite3"
            with HistoryIndex(cache) as index:
                rows = index._rows("SELECT key, value FROM metadata WHERE key LIKE 'projection:%'")
                key, raw = rows[0]
                if reset == "missing_baseline":
                    index._connection().execute("DELETE FROM metadata WHERE key=?", (key,))
                else:
                    value = json.loads(raw)
                    if reset in {"stale_revision", "adjacent_future_revision"}:
                        value["cursor"]["revision"] += 1 if reset == "adjacent_future_revision" else 1000
                    else:
                        value["signature"][2] += 1
                    index._connection().execute("UPDATE metadata SET value=? WHERE key=?", (json.dumps(value), key))
        _append(runtime, _row(runtime, "after-reset", "needle after reset"))
        result = _cli_search(runtime)
    assert replies[-1]["reset"] and replies[-1]["entries"] == 2
    assert len(result["entries"]) == 2


def test_restarted_controller_socket_cannot_reuse_an_old_projection_epoch(runtime):
    with _serving(runtime):
        _cli_search(runtime)
    fresh = TuiRuntime(runtime.conversation_id)
    _append(fresh, _row(fresh, "after-restart", "needle after restart"))
    with _serving(fresh) as replies:
        result = _cli_search(fresh)
    assert replies[0]["reset"] and replies[0]["entries"] == 2
    assert len(result["entries"]) == 2


def test_policy_change_resets_projection_and_removes_secret_from_socket_and_sql(runtime):
    secret = "fixture-policy-sensitive-value-234567"
    _append(runtime, _row(runtime, "sensitive", f"needle {secret}"))
    forbidden = []
    with _serving(runtime, forbidden=forbidden) as replies:
        _cli_search(runtime)
        forbidden.append(secret)
        protected = _cli_search(runtime, env=os.environ | {"FIXTURE_CLIENT_API_KEY": secret})
        _append(runtime, _row(runtime, "protected-append", f"needle {secret}"))
        incremental = _cli_search(runtime, env=os.environ | {"FIXTURE_CLIENT_API_KEY": secret})
    assert [reply["reset"] for reply in replies] == [True, True, False]
    assert [reply["entries"] for reply in replies] == [2, 2, 1]
    assert secret not in json.dumps(protected) and secret not in json.dumps(incremental)
    assert secret.encode() not in (runtime.directory / ".cache" / "history.sqlite3").read_bytes()


def test_old_answer_changes_and_removals_cross_ipc_as_deltas(runtime):
    questions = [{"question": "needle publish?"}]
    ask = {"type": "assistant", "uuid": "ask", "sessionId": runtime._state()["session_id"],
           "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "question-id",
                       "name": "AskUserQuestion", "input": {"questions": questions}}]}}
    def answer(message_id, content):
        return _row(runtime, message_id, [{"type": "tool_result", "tool_use_id": "question-id", "content": content}],
                    toolUseResult={"questions": questions, "answers": {"needle publish?": "No"}, "annotations": {}})
    _append(runtime, ask, answer("visible-answer", "needle public result"))
    with _serving(runtime) as replies:
        cold = _cli_search(runtime)
        assert any(entry["source_kind"] == "verified_user_answer" for entry in cold["entries"])
        _append(runtime, answer("duplicate", "needle duplicate result"))
        _observe(runtime)
        changed = _cli_search(runtime)
        assert changed["refresh"]["changed_records"] == 1
        assert not any(entry["source_kind"] == "verified_user_answer" for entry in changed["entries"])
        assert not replies[-1]["reset"] and replies[-1]["entries"] == 2

    # A verified answer with no public tool-result text disappears entirely once
    # a duplicate revokes its canonical structured answer.
    other_ask = json.loads(json.dumps(ask).replace("question-id", "empty-question"))
    other_ask["uuid"] = "empty-ask"
    empty = json.loads(json.dumps(answer("empty-answer", "")).replace("question-id", "empty-question"))
    _append(runtime, other_ask, empty)
    with _serving(runtime) as replies:
        _cli_search(runtime)
        _append(runtime, {**empty, "uuid": "empty-duplicate"})
        _observe(runtime)
        removed = _cli_search(runtime)
    assert replies[-1]["removed"] == 1 and replies[-1]["entries"] == 0
    assert not replies[-1]["reset"] and removed["refresh"]["removed_records"] == 1
    assert all(entry["locator"]["message_id"] != "empty-answer" for entry in removed["entries"])


def test_failed_sql_transaction_does_not_acknowledge_ipc_cursor(runtime):
    with _serving(runtime) as replies:
        _cli_search(runtime)
        _append(runtime, _row(runtime, "uncommitted", "needle must be retried"))
        setup = """from claude_context_continuity.history_index import HistoryIndex
import apsw
original=HistoryIndex._store_projection_baseline
def fail_after_cursor(self,*args):
    original(self,*args)
    raise apsw.SQLError('injected transaction failure after cursor write')
HistoryIndex._store_projection_baseline=fail_after_cursor
"""
        failure = _cli_search(runtime, setup=setup, success=False)
        assert failure["status"] == "paused"
        _append(runtime, _row(runtime, "later", "needle later append"))
        _observe(runtime)
        recovered = _cli_search(runtime)
    assert [reply["entries"] for reply in replies] == [1, 1, 2]
    assert not replies[-1]["reset"]
    assert len(recovered["entries"]) == 3
    assert recovered["refresh"]["added_records"] == 2


@pytest.mark.parametrize("change", ["rewrite", "truncate", "replace_inode"])
def test_changed_source_identity_or_old_prefix_explicitly_replaces_sql_projection(runtime, change):
    with _serving(runtime) as replies:
        _cli_search(runtime)
        path = Path(runtime._state()["source_path"])
        replacement = json.dumps(_row(runtime, "rewritten", "needle replacement content")) + "\n"
        if change == "rewrite":
            path.write_text(replacement + json.dumps(_row(runtime, "also-new", "needle extra content")) + "\n")
        elif change == "truncate":
            path.write_text("")
        else:
            candidate = path.with_suffix(".replacement")
            candidate.write_text(replacement)
            candidate.replace(path)
        result = _cli_search(runtime)
        assert replies[-1]["reset"]
        ids = {entry["locator"]["message_id"] for entry in result["entries"]}
        assert "user-1" not in ids
        assert ids == (set() if change == "truncate" else {"rewritten", "also-new"} if change == "rewrite" else {"rewritten"})
        _append(runtime, _row(runtime, "after-mutation", "needle append after reset"))
        after = _cli_search(runtime)
    assert not replies[-1]["reset"] and replies[-1]["entries"] == 1
    assert len(after["entries"]) == len(ids) + 1


def test_local_refresh_invalidates_an_unrelated_controller_acknowledgement(runtime):
    with _serving(runtime) as replies:
        _cli_search(runtime)
        _append(runtime, _row(runtime, "local-update", "needle local cache update"))
        state = runtime._state()
        cache = runtime.directory / ".cache" / "history.sqlite3"
        with HistoryIndex(cache) as index:
            index.search([{"source_path": state["source_path"], "session_id": state["session_id"], "generation": 0}],
                         "needle", secrets=core.secret_values())
            assert not index._rows("SELECT key FROM metadata WHERE key LIKE 'projection:%'")
        _append(runtime, _row(runtime, "remote-update", "needle later remote update"))
        result = _cli_search(runtime)
    assert replies[-1]["reset"] and replies[-1]["entries"] == 3
    assert len(result["entries"]) == 3


@pytest.mark.parametrize("warm", [False, True])
def test_live_append_cannot_commit_a_cursor_for_an_unindexed_bounded_snapshot(runtime, monkeypatch, warm):
    with _serving(runtime) as replies:
        cache = runtime.directory / ".cache" / "history.sqlite3"
        if warm:
            _cli_search(runtime)
            _append(runtime, _row(runtime, "before-race", "needle before live append"))
        with HistoryIndex(cache) as index:
            acknowledged = index._rows("SELECT key, value FROM metadata WHERE key LIKE 'projection:%'")
        original = history._decode
        appended = False

        def append_during_decode(raw, start):
            nonlocal appended
            result = original(raw, start)
            if not appended:
                appended = True
                _append(runtime, _row(runtime, "during-read", "needle concurrent append"))
            return result

        monkeypatch.setattr(history, "_decode", append_during_decode)
        failed = _cli_search(runtime, success=False)
        assert appended and failed["status"] == "paused"
        assert "changed while its projection was being indexed" in failed["error"]
        with HistoryIndex(cache) as index:
            assert index._rows("SELECT key, value FROM metadata WHERE key LIKE 'projection:%'") == acknowledged
        recovered = _cli_search(runtime)
    expected = {"user-1", "during-read"} | ({"before-race"} if warm else set())
    assert {entry["locator"]["message_id"] for entry in recovered["entries"]} == expected
    assert replies[-1]["reset"] == (not warm)
    assert replies[-1]["entries"] == 2


def test_semantically_identical_secret_sets_do_not_reset_between_cli_and_observation(runtime, monkeypatch):
    first_secret = "fixture-equivalent-policy-value-123"
    second_secret = "fixture-equivalent-policy-value-456"
    monkeypatch.setenv("FIXTURE_ONE_API_KEY", first_secret)
    monkeypatch.setenv("FIXTURE_TWO_API_KEY", second_secret)
    monkeypatch.setenv("FIXTURE_DUPLICATE_API_KEY", first_secret)
    state = runtime._state()
    source = runtime._history_source(state["source_path"], state["session_id"])
    with _serving(runtime, forbidden=(first_secret, second_secret)) as replies:
        _cli_search(runtime)
        _append(runtime, _row(runtime, "equivalent-policy", f"needle {first_secret} {second_secret}"))
        source.index_projection(secrets=("", *reversed(core.secret_values())))
        result = _cli_search(runtime)
    assert not replies[-1]["reset"] and replies[-1]["entries"] == 1
    assert result["refresh"]["projection_resets"] == 0
    assert first_secret not in json.dumps(result) and second_secret not in json.dumps(result)
