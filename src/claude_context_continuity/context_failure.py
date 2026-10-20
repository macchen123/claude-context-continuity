"""Bind a native context-limit failure to its exact main-session history boundary."""
from __future__ import annotations

from . import core
from .history import _content, _texts


def is_context_rejection(error, text):
    if error != "invalid_request" or not isinstance(text, str):
        return False
    text = text.strip()
    return text == "Prompt is too long" or text.startswith("Prompt is too long · ")


def rejection_locator(state, source, records):
    boundary = state.get("failure_boundary")
    if (not isinstance(boundary, dict) or boundary.get("session_id") != state["session_id"]
            or boundary.get("turn_generation") != state.get("turn_generation", 0)):
        return None
    latest = next((row for row in reversed(records) if row.kind == "assistant"), None)
    if latest is None:
        return None
    data = latest.data
    text = "\n".join(_texts(_content(data))).strip()
    if (data.get("isApiErrorMessage") is not True or data.get("message", {}).get("model") != "<synthetic>"
            or not is_context_rejection(data.get("error"), text)
            or core.digest(text) != boundary.get("message_hash")):
        return None
    last_input = max((row.start for row in records if row.data.get("type") == "user"), default=-1)
    if latest.start <= last_input or latest.start < boundary.get("after_byte", 0):
        return None
    return source._locator(latest, latest.kind)
