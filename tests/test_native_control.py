from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import core, native_control
from claude_context_continuity.context_runtime import _runtime_message


class NativeControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        scope = patch.object(core, "HOME", self.home)
        scope.start()
        self.addCleanup(scope.stop)
        self.context_id = str(uuid4())
        self.path = self.home / "runtime/control" / f"{self.context_id}.sock"
        self.control = {"protocol": native_control.PROTOCOL, "context_id": self.context_id,
                        "socket_path": str(self.path)}
        self.auth = "synthetic-local-auth-for-isolated-tests"

    def test_native_messages_do_not_touch_the_terminal_and_do_not_persist_auth(self):
        native_control.prepare(self.control)
        self.assertFalse(native_control.ready(self.control))
        client = Mock()
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=client)
        factory.return_value.__exit__ = Mock(return_value=False)
        with patch.object(native_control, "ready", return_value=True), \
                patch.object(native_control.socket, "socket", factory):
            native_control.send_clear(self.control, self.auth)
            first = client.sendall.call_args.args[0]
            text = _runtime_message("从原生历史定位继续，保留正在编辑的草稿。")
            native_control.send_continuation(self.control, self.auth, text)
            second = client.sendall.call_args.args[0]
        self.assertEqual([json.loads(line) for line in first.splitlines()], [
            {"role": "attacher", "auth": self.auth}, {"type": "reply", "text": "/clear"}])
        self.assertEqual(json.loads(second.splitlines()[1]), {"type": "reply", "text": text})
        self.assertEqual(client.sendall.call_count, 2)
        self.assertEqual(list(self.path.parent.iterdir()), [])
        self.assertNotIn(self.auth, json.dumps(self.control))

    def test_socket_owner_path_and_type_are_checked(self):
        native_control.prepare(self.control)
        self.path.write_text("not a socket")
        with self.assertRaises(native_control.NativeControlError):
            native_control.ready(self.control)
        wrong = dict(self.control, socket_path=str(self.home / "another.sock"))
        with self.assertRaises(native_control.NativeControlError):
            native_control.ready(wrong)
        with self.assertRaises(native_control.NativeControlError):
            native_control.prepare(self.control)

    def test_unknown_delivery_is_not_retried(self):
        client = Mock()
        client.sendall.side_effect = BrokenPipeError("closed")
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=client)
        factory.return_value.__exit__ = Mock(return_value=False)
        with patch.object(native_control, "ready", return_value=True), \
                patch.object(native_control.socket, "socket", factory):
            with self.assertRaisesRegex(native_control.NativeControlError, "不自动重发"):
                native_control.send_clear(self.control, self.auth)
        client.sendall.assert_called_once()

    def test_only_marked_continuation_is_allowed_and_auth_is_redacted(self):
        with self.assertRaises(native_control.NativeControlError):
            native_control.send_continuation(self.control, self.auth, "新用户指令")
        with patch.dict(os.environ, {native_control.AUTH_ENV: self.auth}):
            with self.assertRaises(core.ContinuityError):
                core.no_secrets({"accidental_output": self.auth})
        env = native_control.environment(self.control, self.auth)
        self.assertEqual(env[native_control.AUTH_ENV], self.auth)
        self.assertNotIn("CLAUDE_JOB_DIR", env)

    def test_version_and_socket_length_fail_before_starting_an_unsupported_host(self):
        with patch.object(native_control.subprocess, "run", return_value=Mock(returncode=0, stdout="2.1.266 (Claude Code)")):
            self.assertEqual(native_control.require_supported_cli(), "2.1.266")
        with patch.object(native_control.subprocess, "run", return_value=Mock(returncode=0, stdout="2.1.200 (Claude Code)")):
            with self.assertRaises(native_control.NativeControlError):
                native_control.require_supported_cli()
        with patch.object(core, "HOME", Path("/" + "long" * 40)):
            with self.assertRaises(native_control.NativeControlError):
                native_control.binding(self.context_id)


if __name__ == "__main__":
    unittest.main()
