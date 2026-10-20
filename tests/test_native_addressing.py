"""Native addressing syntax and read-only discovery; no native process or message is sent."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from claude_context_continuity import core, native_addressing


class NativeAddressingTests(unittest.TestCase):
    def test_explicit_native_peer_schemes_and_socket_syntax(self):
        for address in ("uds:/tmp/cc-socks/example.sock", "bridge:fixture-session", "did:example:fixture",
                        "/tmp/cc-socks/example.sock", r"\\.\pipe\cc-msg-example"):
            with self.subTest(address=address):
                self.assertTrue(native_addressing.explicit_peer(address))
        for name in ("bounded-worker", "bounded-worker [abc123]", "a1234567890abcdef", "main",
                     "relative/example.sock", "/tmp/not a socket.sock"):
            with self.subTest(name=name):
                self.assertFalse(native_addressing.explicit_peer(name))

    def test_native_name_normalization_and_existing_aliases(self):
        self.assertEqual(native_addressing.normalized_name(" ＢＯＵＮＤＥＤ\u200b  Worker "), "bounded-worker")
        self.assertEqual(native_addressing.bare_recipient(" bounded-worker [ABC123] "), "bounded-worker")
        self.assertEqual(native_addressing.bare_recipient("bounded-worker[abc123]"), "bounded-worker")

    def test_native_json_discovery_handles_names_aliases_and_prefixes(self):
        # Field layouts were checked against native 2.1.304's real read-only CLI.
        rows = [{"kind": "interactive", "name": "openmetadata-dd", "sessionId": "fixture-session",
                 "cwd": "/fixture", "pid": 123, "status": "idle", "startedAt": 0},
                {"kind": "background", "name": "other-peer", "sessionId": "fixture-background",
                 "id": "fixture-job", "cwd": "/fixture", "startedAt": 0, "state": "running"}]
        result = SimpleNamespace(returncode=0, stdout=json.dumps(rows))
        with patch.object(native_addressing.subprocess, "run", return_value=result) as run:
            for to in ("openmetadata-dd", "openmetadata-dd [abc123]", " OPENMETADATA DD ", "openmetadata"):
                with self.subTest(to=to):
                    self.assertTrue(native_addressing.named_peer(to))
            self.assertFalse(native_addressing.named_peer("unlisted-local-worker"))
            self.assertFalse(native_addressing.named_peer("op"))
        run.assert_called_with(["claude", "agents", "--json"], capture_output=True, text=True, timeout=5)

    def test_missing_or_failed_native_discovery_is_not_peer_proof(self):
        results = [SimpleNamespace(returncode=1, stdout='[{"name":"peer"}]')]
        results.extend(SimpleNamespace(returncode=0, stdout=value) for value in (
            "[]", "{", "null", '{}', '[{"name":"peer"},null]', '[{"name":123}]',
            json.dumps([{"name": "x" * core.MAX_PACKET}])))
        for result in results:
            with self.subTest(size=len(result.stdout), code=result.returncode), \
                    patch.object(native_addressing.subprocess, "run", return_value=result):
                self.assertFalse(native_addressing.named_peer("peer"))
        for error in (FileNotFoundError("fixture missing CLI"), subprocess.TimeoutExpired("claude", 5),
                      UnicodeDecodeError("utf8", b"\xff", 0, 1, "fixture decode failure")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(native_addressing.subprocess, "run", side_effect=error):
                self.assertFalse(native_addressing.named_peer("peer"))


if __name__ == "__main__":
    unittest.main()
