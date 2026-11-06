"""Share only the current canonical projection, never SQLite work, with hooks."""
from __future__ import annotations

import json

from . import hook_transport
from .context_runtime import ContextRuntime
from .history import HistoryError, HistorySource
from .history_projection import HistoryProjection


class _CurrentProjector:
    def __init__(self, context_id, binding):
        self.context_id = context_id
        self.binding = binding

    def __call__(self, source: HistorySource, secrets: tuple[str, ...]):
        return self.project_since(source, secrets)

    def project_since(self, source: HistorySource, secrets: tuple[str, ...], *,
                      baseline=None, redaction_policy=None):
        if (str(source.path), source.session_id) != self.binding:
            return None
        result = hook_transport.request(self.context_id, {
            "_cclaude_action": "history-projection",
            "source_path": str(source.path),
            "session_id": source.session_id,
            "known_secrets": list(secrets),
            "baseline": baseline,
            "redaction_policy": redaction_policy,
        })
        if result is not None:
            # Match HookServer's actual response encoding, before adding local
            # accounting. Authentication and known-secret request bytes stay out.
            response_bytes = len((json.dumps({"result": result}, ensure_ascii=False) + "\n").encode("utf-8"))
            result["transport_work"] = {
                "transmitted_entries": len(result["entries"]),
                "transmitted_bytes": response_bytes,
                "projection_resets": int(result["reset"]),
            }
        return result


def current_projector(context_id: str):
    if not hook_transport._path(context_id).exists():
        return None
    state = ContextRuntime(context_id)._state()
    return _CurrentProjector(context_id, (state.get("source_path"), state["session_id"]))


def project_current(runtime, event):
    state = runtime._state()
    binding = (state.get("source_path"), state["session_id"])
    if (event["source_path"], event["session_id"]) != binding:
        raise HistoryError("current history binding changed; restart the search")
    source = runtime._history_source(*binding)
    info = source.path.stat()
    identity = [info.st_dev, info.st_ino]
    baseline = event.get("baseline")
    policy = HistoryProjection.normalize_policy(event["known_secrets"])
    index_policy = event.get("redaction_policy")
    expected = {
        "version": 1,
        "source_path": str(source.path),
        "session_id": source.session_id,
        "identity": identity,
        "policy_id": HistoryProjection.policy_id(policy),
        "index_policy": index_policy,
        "projection_id": source._projection.identity if source._projection is not None else None,
    }
    valid = (isinstance(baseline, dict) and set(baseline) == {*expected, "revision"}
             and all(baseline[key] == value for key, value in expected.items())
             and type(baseline["revision"]) is int and baseline["revision"] >= 0
             and source._projection is not None and baseline["revision"] <= source._projection.revision)
    projection = source.index_projection(secrets=policy, since=baseline["revision"] if valid else None)
    cursor = {**expected, "projection_id": projection["projection_id"], "revision": projection["revision"]}
    return {**projection, "projection_cursor": cursor, "reading_work": source.reading_work}
