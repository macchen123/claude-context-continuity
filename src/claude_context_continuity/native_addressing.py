"""Read-only native recipient syntax and session discovery, never message delivery.

Native Claude Code owns routing, disambiguation, pins and transport validation.
These helpers only establish that a SendMessage target cannot restore a local
subagent; they do not rewrite the address or maintain a second peer registry.
"""
from __future__ import annotations

import json
import re
import subprocess
import unicodedata

from . import core


_ALIAS = re.compile(r"\s*\[[0-9a-fA-F]{6,12}\]$")
_SOCKET = re.compile(r"^/\S*\.sock$")
_PIPE = re.compile(r"^[\\/]{2}[.?][\\/]pipe[\\/]", re.IGNORECASE)


def bare_recipient(value):
    return _ALIAS.sub("", value.strip())


def normalized_name(value):
    # Native 2.1.304's recipient-name matching: NFKC, no non-whitespace
    # control/format/surrogate characters, lower case, whitespace to hyphens.
    value = unicodedata.normalize("NFKC", value)
    value = "".join(char for char in value if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    return re.sub(r"\s+", "-", value.strip().lower())


def explicit_peer(value):
    value = value.strip()
    return (value.startswith(("uds:", "bridge:", "did:"))
            or _SOCKET.fullmatch(value) is not None or _PIPE.match(value) is not None)


def discover_peers():
    """Return native discovery rows, or None when discovery is unavailable.

    Keep the sole read-only discovery call here. No registry, probes or sends.
    """
    try:
        result = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0 or len(result.stdout.encode("utf-8")) > core.MAX_PACKET:
            return None
        rows = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    return rows


def named_peer(value):
    """Prove a pending sender's named peer through native read-only discovery.

    `claude agents --json` lists active interactive/background sessions, not
    in-process subagents. Missing/unsupported/unreadable discovery is not proof.
    Keep identities and command output in memory; never probe or send to peers.
    """
    name = normalized_name(bare_recipient(value))
    if not name:
        return False
    rows = discover_peers()
    if rows is None:
        return False
    names = {normalized_name(row["name"]) for row in rows if isinstance(row.get("name"), str)}
    # A name prefix may be ambiguous among peers. That is still non-local;
    # the native resolver alone decides whether/where the message can be sent.
    return name in names or (len(name) >= 3 and any(peer.startswith(name) for peer in names))
