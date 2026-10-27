"""Project native response usage without retaining message content."""
from __future__ import annotations


def message_usage(message):
    if not isinstance(message, dict) or message.get("model") == "<synthetic>":
        return None
    usage, model = message.get("usage"), message.get("model")
    if not isinstance(usage, dict) or not isinstance(model, str) or not model:
        raise ValueError("latest native assistant record has no actual model/input usage")
    names = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    values = {name: usage.get(name, 0) for name in names}
    if "input_tokens" not in usage or any(type(value) is not int or value < 0 for value in values.values()):
        raise ValueError("latest native input usage is incomplete")
    output = usage.get("output_tokens", 0)
    if type(output) is not int or output < 0:
        raise ValueError("latest native output usage is invalid")
    return {"actual_model": model, "usage": values, "total_input_tokens": sum(values.values()),
            "output_tokens": output, "cache_fields_complete": all(name in usage for name in names[1:])}
