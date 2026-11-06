"""Targeted recovery work counts on synthetic native metadata/history only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from claude_context_continuity import agent_history, agent_metadata


def write_agent(project, session, agent, *, name=None, relative=Path(), history=None):
    base = project / session / "subagents" / relative
    base.mkdir(parents=True, exist_ok=True)
    metadata = {"agentType": "implementation-worker", "model": "original-model-slot",
                "toolUseId": f"launch-{agent}", **({"name": name} if name else {})}
    (base / f"agent-{agent}.meta.json").write_text(json.dumps(metadata))
    path = base / f"agent-{agent}.jsonl"
    if history is None:
        path.write_text(json.dumps({"type": "assistant", "uuid": str(uuid4()), "agentId": agent,
            "sessionId": session, "isSidechain": True,
            "message": {"role": "assistant", "content": "synthetic original history"}}) + "\n")
    else:
        os.link(history, path)
    return path


def test_exact_id_work_does_not_read_irrelevant_agents_and_reads_one_inode_once(tmp_path):
    project = tmp_path / "project"
    sessions = [str(uuid4()) for _ in range(160)]
    current = str(uuid4())
    entries = [{"session_id": sid, "source_path": str(project / f"{sid}.jsonl")} for sid in sessions]
    agent = "a1234567890abcdef"
    original = write_agent(project, sessions[0], agent)
    for sid in sessions[1:]:
        write_agent(project, sid, agent, history=original)
    state = {"session_id": current, "source_path": str(project / f"{current}.jsonl"),
             "child_handle_sources": {agent: [sessions[0], entries[0]["source_path"]]}}
    runtime = tmp_path / "runtime"
    binding = runtime / "agent-budgets" / f"{agent}.json"
    binding.parent.mkdir(parents=True)
    binding.write_text(json.dumps({"agent_id": agent, "session_id": sessions[0], "source_path": str(original)}))
    snapshot, history_snapshot, scandir = agent_metadata._snapshot, agent_history._snapshot, os.scandir
    counts = []
    for irrelevant in (0, 1000):
        if irrelevant:
            for number in range(irrelevant):
                path = project / sessions[number % len(sessions)] / "subagents" / f"agent-a9{number:014x}.meta.json"
                path.write_text("{unreadable unrelated metadata")
        # Publish to a new epoch in both cases, rather than comparing cold to warm.
        state["session_id"] = str(uuid4())
        state["source_path"] = str(project / f"{state['session_id']}.jsonl")
        with patch.object(agent_metadata, "_snapshot", wraps=snapshot) as metadata_reads, \
                patch.object(agent_history, "_snapshot", wraps=history_snapshot) as transcript_reads, \
                patch.object(os, "scandir", wraps=scandir) as scans:
            result = agent_metadata.synchronize(state, entries, runtime, to=agent, session_id=state["session_id"])
        assert result["status"] == "verified"
        assert [row["agent_id"] for row in result["agents"]] == [agent]
        assert {Path(call.args[0]).name for call in metadata_reads.call_args_list} == {f"agent-{agent}.meta.json"}
        assert transcript_reads.call_count == 1
        assert os.path.samefile(original, result["agents"][0]["history"]["path"])
        counts.append((metadata_reads.call_count, scans.call_count, transcript_reads.call_count))
        print(f"windows=160 irrelevant={irrelevant} metadata_attempts={counts[-1][0]} "
              f"directory_scans={counts[-1][1]} transcript_reads={counts[-1][2]} "
              f"transcript_bytes={original.stat().st_size}")
    assert counts[0] == counts[1] == (163, 0, 1)
    with patch.object(agent_history, "_snapshot", wraps=history_snapshot) as transcript_reads:
        assert agent_metadata.synchronize(state, entries, runtime, to=agent)["status"] == "verified"
    assert transcript_reads.call_count == 0
    binding.unlink()
    state["session_id"] = str(uuid4())
    state["source_path"] = str(project / f"{state['session_id']}.jsonl")
    with patch.object(agent_metadata, "_snapshot", wraps=snapshot) as metadata_reads, \
            patch.object(agent_history, "_snapshot", wraps=history_snapshot) as transcript_reads, \
            patch.object(os, "scandir", wraps=scandir) as scans:
        result = agent_metadata.synchronize(state, entries, runtime, to=agent)
    assert result["status"] == "verified"
    assert (metadata_reads.call_count, scans.call_count, transcript_reads.call_count) == (163, 160, 1)
    assert {Path(call.args[0]).name for call in metadata_reads.call_args_list} == {f"agent-{agent}.meta.json"}
    print("windows=160 irrelevant=1000 unbound_exact_id metadata_attempts=163 directory_scans=160 transcript_reads=1")


def test_bound_nested_fragments_read_each_physical_source_once_and_reuse_receipt(tmp_path):
    project = tmp_path / "project"
    sessions = [str(uuid4()) for _ in range(160)]
    current = str(uuid4())
    agent = "a1234567890abcdef"
    relative = Path("native-nested")
    root_id, suffix_id = str(uuid4()), str(uuid4())
    original = write_agent(project, sessions[0], agent, relative=relative)
    original.write_text(json.dumps({"type": "user", "uuid": root_id, "parentUuid": None,
        "agentId": agent, "sessionId": sessions[0], "isSidechain": True,
        "message": {"role": "user", "content": "synthetic original"}}) + "\n")
    suffix = write_agent(project, sessions[80], agent, relative=relative)
    suffix.write_text(json.dumps({"type": "assistant", "uuid": suffix_id, "parentUuid": root_id,
        "agentId": agent, "sessionId": sessions[80], "isSidechain": True,
        "message": {"role": "assistant", "content": "synthetic continuation"}}) + "\n")
    for number, sid in enumerate(sessions):
        if number not in (0, 80):
            write_agent(project, sid, agent, relative=relative, history=original if number < 80 else suffix)
    entries = [{"session_id": sid, "source_path": str(project / f"{sid}.jsonl")} for sid in sessions]
    state = {"session_id": current, "source_path": str(project / f"{current}.jsonl")}
    runtime = tmp_path / "runtime"
    binding = runtime / "agent-budgets" / f"{agent}.json"
    binding.parent.mkdir(parents=True)
    binding.write_text(json.dumps({"agent_id": agent, "session_id": sessions[0], "source_path": str(original)}))
    with patch.object(agent_history, "_snapshot", wraps=agent_history._snapshot) as reads, \
            patch.object(os, "scandir", wraps=os.scandir) as scans:
        result = agent_metadata.synchronize(state, entries, runtime, to=agent)
    assert result["status"] == "verified"
    history = result["agents"][0]["history"]
    assert history["status"] == "reconstructed"
    assert Path(history["path"]).read_bytes() == original.read_bytes() + suffix.read_bytes()
    assert reads.call_count == 2
    assert scans.call_count == 0
    with patch.object(agent_history, "_snapshot", side_effect=AssertionError("receipt must avoid history reads")):
        again = agent_metadata.synchronize(state, entries, runtime, to=agent)
    assert again["agents"][0]["history"]["status"] == "available"


def test_stop_settlement_can_select_only_the_recipient(tmp_path):
    project = tmp_path / "project"
    old, current = str(uuid4()), str(uuid4())
    agent = "a1234567890abcdef"
    source = write_agent(project, old, agent)
    metadata = source.with_suffix(".meta.json")
    value = json.loads(metadata.read_text())
    metadata.write_text(json.dumps({**value, "stoppedByUser": True}))
    unrelated = "a9999999999999999"
    (metadata.parent / f"agent-{unrelated}.meta.json").write_text("{incomplete unrelated sidecar")
    state = {"session_id": current, "source_path": str(project / f"{current}.jsonl"),
             "active_child_handles": [agent, unrelated]}
    entries = [{"session_id": old, "source_path": str(project / f"{old}.jsonl")}]
    with patch.object(agent_metadata, "_snapshot", wraps=agent_metadata._snapshot) as reads:
        stopped = agent_metadata.stopped_handles(state, entries, agents=[agent])
    assert set(stopped) == {agent}
    assert {Path(call.args[0]) for call in reads.call_args_list} == {metadata}
    assert not (project / current).exists()


def test_native_prefix_and_name_keep_all_ambiguous_candidates(tmp_path):
    project = tmp_path / "project"
    old, current = str(uuid4()), str(uuid4())
    agents = ["a1234567890aaaaaa", "a1234567890bbbbbb"]
    for agent in agents:
        write_agent(project, old, agent, name="shared-worker")
    for sid in (old, current):
        (project / f"{sid}.jsonl").write_text("")
    state = {"session_id": current, "source_path": str(project / f"{current}.jsonl")}
    entries = [{"session_id": old, "source_path": str(project / f"{old}.jsonl")}]
    for recipient in ("a1234567", "shared", "shared-worker [abc123]"):
        result = agent_metadata.synchronize(state, entries, tmp_path / "runtime", to=recipient)
        assert result["status"] == "verified"
        assert [row["agent_id"] for row in result["agents"]] == agents
        assert all(row["agent_type"] == "implementation-worker" for row in result["agents"])


def test_targeted_id_preserves_nested_fragment_identity_conflicts(tmp_path):
    project = tmp_path / "project"
    old, current = str(uuid4()), str(uuid4())
    agent = "a1234567890abcdef"
    write_agent(project, old, agent)
    write_agent(project, old, agent, relative=Path("nested"))
    state = {"session_id": current, "source_path": str(project / f"{current}.jsonl")}
    entries = [{"session_id": old, "source_path": str(project / f"{old}.jsonl")}]
    result = agent_metadata.synchronize(state, entries, tmp_path / "runtime", to=agent)
    assert result["blocked"][0]["reason"] == "conflicting_agent_directory"
    assert not (project / current).exists()
