"""Shape checks for the opt-in, private native parallel-history fixture."""
import json

from native_agent_resume_probe import PARALLEL_MARKER, parallel_fragments


def test_parallel_fragments_keep_response_and_distinct_result_parents(tmp_path):
    rows = [
        {"type": "user", "uuid": "prompt", "parentUuid": None,
         "message": {"role": "user", "content": "private probe"}},
        {"type": "assistant", "uuid": "call-a", "parentUuid": "prompt",
         "agentId": "private-child", "sessionId": "private-session", "isSidechain": True,
         "message": {"id": "response-a", "role": "assistant", "content": [
             {"type": "tool_use", "id": "tool-a", "name": "Bash", "input": {"command": "true"}}]}},
        {"type": "user", "uuid": "result-a", "parentUuid": "call-a",
         "agentId": "private-child", "sessionId": "private-session", "isSidechain": True,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tool-a", "content": "ORIGINAL_PRODUCER_RECEIPT"}]}},
        {"type": "assistant", "uuid": "complete", "parentUuid": "result-a",
         "message": {"id": "response-b", "role": "assistant", "content": [{"type": "text", "text": "done"}]}},
    ]
    raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    prefix, suffix, fixture = parallel_fragments(raw, tmp_path)
    before, after = [[json.loads(line) for line in part.splitlines()] for part in (prefix, suffix)]
    assert prefix.startswith(b"".join(raw.splitlines(keepends=True)[:2]))
    assert before[-2]["message"]["id"] == before[-1]["message"]["id"] == fixture["response_id"]
    assert [row["uuid"] for row in before[-2:]] == fixture["parent_uuids"]
    assert len(set(fixture["parent_uuids"])) == 2
    assert after[0]["type"] == "attachment"
    assert after[0]["attachment"]["type"] == "hook_success"
    assert after[0]["parentUuid"] == fixture["parent_uuids"][1]
    assert [row["parentUuid"] for row in after[1:3]] == fixture["parent_uuids"]
    assert [row["message"]["content"][0]["tool_use_id"] for row in after[1:3]] == fixture["tool_ids"]
    assert after[1]["message"] == rows[2]["message"]
    assert after[2]["message"]["content"][0]["content"] == PARALLEL_MARKER
    assert after[3]["parentUuid"] == after[2]["uuid"]
    assert rows[2]["parentUuid"] == "call-a"
