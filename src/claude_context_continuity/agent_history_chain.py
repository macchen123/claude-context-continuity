"""Verify native fragment ancestry without rewriting or publishing history."""
from __future__ import annotations


class AgentHistoryError(ValueError):
    """Public reason only; never include native message content."""


def _message(row):
    value = row.get("message")
    return value if isinstance(value, dict) else {}


def _parallel_boundary(previous, following, parents):
    indexed = {row["uuid"]: row for row in previous if isinstance(row.get("uuid"), str)}
    bridges = [indexed[parent] for parent in parents]
    response_id = _message(bridges[0]).get("id")
    if (not isinstance(response_id, str) or not response_id
            or any(row.get("type") != "assistant" or _message(row).get("id") != response_id for row in bridges)):
        return False
    response = [row for row in previous
                if row.get("type") == "assistant" and _message(row).get("id") == response_id]
    calls = {}
    for row in response:
        content = _message(row).get("content")
        if not isinstance(content, list):
            return False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            if not isinstance(tool_id, str) or not tool_id or tool_id in calls:
                return False
            calls[tool_id] = row.get("uuid")
    if len(calls) < 2 or not parents <= set(calls.values()):
        return False
    settled = set()

    def accept_result(row):
        content = _message(row).get("content")
        if (row.get("type") != "user" or not isinstance(content, list) or not content
                or any(not isinstance(block, dict) or block.get("type") != "tool_result" for block in content)):
            return False
        ids = [block.get("tool_use_id") for block in content]
        if (any(not isinstance(tool_id, str) or tool_id not in calls for tool_id in ids)
                or len(set(ids)) != len(ids) or settled.intersection(ids)
                or any(calls[tool_id] != row.get("parentUuid") for tool_id in ids)):
            return False
        settled.update(ids)
        return True

    response_ids = {row.get("uuid") for row in response}
    start = next(index for index, row in enumerate(previous) if row.get("uuid") in response_ids)
    attached, pending = set(response_ids), {}
    for row in previous[start:]:
        uid = row.get("uuid")
        if not isinstance(uid, str) or uid in response_ids:
            continue
        if row.get("type") == "attachment":
            pending[uid] = row.get("parentUuid")
        elif accept_result(row):
            attached.add(uid)
        else:
            return False
    while pending:
        linked = {uid for uid, parent in pending.items() if parent in attached}
        if not linked:
            return False
        attached.update(linked)
        pending = {uid: parent for uid, parent in pending.items() if uid not in linked}
    matched = False
    for row in following:
        if row.get("parentUuid") not in parents:
            continue
        if row.get("type") == "attachment":
            continue
        if not accept_result(row):
            return False
        matched = True
    return matched


def order_fragments(snapshots):
    # A previously reconstructed full stream may coexist with its retained inputs.
    maximal = [item for item in snapshots if not any(
        item["raw"] != other["raw"] and item["raw"] in other["raw"] for other in snapshots)]
    owners, identifiers = {}, []
    for index, item in enumerate(maximal):
        ids = set()
        for row in item["rows"]:
            uid = row.get("uuid")
            if not isinstance(uid, str):
                continue
            if uid in owners:
                raise AgentHistoryError("divergent_agent_histories")
            owners[uid] = index
            ids.add(uid)
        if not ids:
            raise AgentHistoryError("missing_agent_history_predecessor")
        identifiers.append(ids)
    predecessors, successors = {}, {}
    for index, item in enumerate(maximal):
        external = {row["parentUuid"] for row in item["rows"]
                    if row.get("parentUuid") and row["parentUuid"] not in identifiers[index]}
        if not external:
            continue
        if not external <= owners.keys():
            raise AgentHistoryError("missing_agent_history_predecessor")
        sources = {owners[parent] for parent in external}
        if len(sources) != 1:
            raise AgentHistoryError("divergent_agent_histories")
        previous = sources.pop()
        ancestry = [row for row in maximal[previous]["rows"] if isinstance(row.get("uuid"), str)]
        linear = (len(external) == 1 and ancestry[-1]["uuid"] in external
                  and not any(row.get("parentUuid") in external for row in maximal[previous]["rows"]))
        if (previous in successors or not (linear or _parallel_boundary(ancestry, item["rows"], external))):
            raise AgentHistoryError("divergent_agent_histories")
        predecessors[index], successors[previous] = previous, index
    roots = set(range(len(maximal))) - predecessors.keys()
    if len(roots) != 1:
        raise AgentHistoryError("divergent_agent_histories")
    order, current = [], roots.pop()
    while current not in order:
        order.append(current)
        if current not in successors:
            break
        current = successors[current]
    if len(order) != len(maximal):
        raise AgentHistoryError("divergent_agent_histories")
    return [maximal[index] for index in order]
