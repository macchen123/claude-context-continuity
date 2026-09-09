"""tmux transport 的纯本地单元测试；绝不连接或修改真实 tmux server。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


CONTINUITY_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(CONTINUITY_ROOT / "src"))

from claude_context_continuity import core, tmux_transport


class TmuxTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=TESTS_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "global"
        self.home_patch = patch.object(core, "HOME", self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.conversation_id = str(uuid4())
        # 测试目录本身超过 macOS AF_UNIX 限制；命令测试使用 mock socket 身份。
        self.socket_limit_patch = patch.object(tmux_transport, "_MAX_MACOS_SOCKET_PATH_BYTES", 4096)
        self.socket_limit_patch.start()
        self.addCleanup(self.socket_limit_patch.stop)

    def _short_socket_path(self, conversation_id: str | None = None) -> Path:
        return self.home / "runtime" / "tmux" / f"{conversation_id or self.conversation_id}.sock"

    def _context_socket_path(self) -> Path:
        return self.home / "runtime" / "contexts" / self.conversation_id / "tui" / "native.sock"

    def _binding(self, path: Path | None = None, *, pane_id: str = "%7") -> dict[str, object]:
        return {
            "socket_path": str(path or self._short_socket_path()),
            "pane_id": pane_id,
            "pane_pid": 123,
            "session_name": "continuity",
            "server_pid": 456,
        }

    def _prepared_socket_path(self, path: Path | None = None) -> Path:
        path = path or self._short_socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _result(args, output: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode, stdout=output)

    @staticmethod
    def _inspection_output(*, pane_id: str = "%7", pane_pid: int = 123,
                           server_pid: int = 456, session_name: str = "continuity",
                           pane_dead: int = 0, cursor_x: int = 5, cursor_y: int = 3,
                           width: int = 120, height: int = 40, command: str = "claude") -> str:
        return (f"{pane_id}\t{pane_pid}\t{server_pid}\t{session_name}\t{pane_dead}\t"
                f"{cursor_x}\t{cursor_y}\t{width}\t{height}\t{command}\n")

    def _inspect_command(self, path: Path) -> list[str]:
        return [
            "tmux", "-S", str(path), "list-panes", "-s", "-t", "continuity", "-F",
            "#{pane_id}\t#{pane_pid}\t#{pid}\t#{session_name}\t#{pane_dead}\t"
            "#{cursor_x}\t#{cursor_y}\t#{pane_width}\t#{pane_height}\t#{pane_current_command}",
        ]

    def test_create_uses_short_private_socket_and_one_shell_command(self) -> None:
        path = self._short_socket_path()
        environment = {"PATH": "/usr/bin", "TEST_ONLY_TOKEN": "kept-in-memory"}
        calls: list[tuple[list[str], dict[str, object]]] = []
        responses = [
            self._result([], ""),
            self._result([], "%7\t123\t456\tcontinuity\tclaude\n"),
        ]

        def fake_run(args, **kwargs):
            calls.append((list(args), kwargs))
            return responses.pop(0)

        with patch.object(tmux_transport.subprocess, "run", side_effect=fake_run):
            binding = tmux_transport.create(path, self.cwd, ["claude"], environment)

        self.assertEqual(binding, self._binding(path))
        self.assertEqual(calls[0][0], [
            "tmux", "-S", str(path), "new-session", "-d", "-s", "continuity",
            "-x", "120", "-y", "40", "-c", str(self.cwd), "exec claude",
        ])
        self.assertEqual(calls[1][0], [
            "tmux", "-S", str(path), "display-message", "-p", "-t", "continuity:0.0",
            "#{pane_id}\t#{pane_pid}\t#{pid}\t#{session_name}\t#{pane_current_command}",
        ])
        for _, kwargs in calls:
            self.assertEqual(kwargs["env"], environment)
            self.assertIsNot(kwargs["env"], environment)
            self.assertEqual(kwargs["cwd"], str(self.cwd))
            self.assertNotIn("input", kwargs)
            self.assertNotIn("shell", kwargs)
        self.assertTrue(all(command[1:3] == ["-S", str(path)] for command, _ in calls))
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.iterdir()), [])

    def test_create_rejects_existing_socket_and_preserves_user_native_flags(self) -> None:
        path = self._short_socket_path()
        path.parent.mkdir(parents=True)
        path.write_text("do not adopt")
        with patch.object(tmux_transport.subprocess, "run") as run:
            with self.assertRaises(tmux_transport.TmuxTransportError):
                tmux_transport.create(path, self.cwd, ["claude"], {"PATH": "/usr/bin"})
            run.assert_not_called()

        fresh = self._short_socket_path(str(uuid4()))
        calls: list[list[str]] = []
        responses = [
            self._result([], ""),
            self._result([], "%7\t123\t456\tcontinuity\tclaude\n"),
        ]

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return responses.pop(0)

        user_argv = ["claude", "--model", "user-selected", "--permission-mode", "default", "--tools", "Read,Edit"]
        with patch.object(tmux_transport.subprocess, "run", side_effect=fake_run):
            binding = tmux_transport.create(fresh, self.cwd, user_argv, {"PATH": "/usr/bin"})

        self.assertEqual(binding, self._binding(fresh))
        self.assertEqual(calls[0], [
            "tmux", "-S", str(fresh), "new-session", "-d", "-s", "continuity",
            "-x", "120", "-y", "40", "-c", str(self.cwd),
            "exec claude --model user-selected --permission-mode default --tools Read,Edit",
        ])
        self.assertEqual(calls[1], [
            "tmux", "-S", str(fresh), "display-message", "-p", "-t", "continuity:0.0",
            "#{pane_id}\t#{pane_pid}\t#{pid}\t#{session_name}\t#{pane_current_command}",
        ])

    def test_socket_path_identity_accepts_context_and_short_runtime_forms(self) -> None:
        short = self._short_socket_path()
        legacy = self._context_socket_path()
        self.assertEqual(tmux_transport.attach_argv(self._binding(short)), [
            "tmux", "-S", str(short), "attach-session", "-t", "continuity",
        ])
        self.assertEqual(tmux_transport.attach_argv(self._binding(legacy)), [
            "tmux", "-S", str(legacy), "attach-session", "-t", "continuity",
        ])
        wrong = self.root / "not-runtime.sock"
        with self.assertRaises(tmux_transport.TmuxTransportError):
            tmux_transport.attach_argv(self._binding(wrong))

    def test_socket_path_rejects_symlink_and_macos_length_before_client(self) -> None:
        self.home.mkdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.home / "runtime").symlink_to(elsewhere, target_is_directory=True)
        with patch.object(tmux_transport.subprocess, "run") as run:
            with self.assertRaises(tmux_transport.TmuxTransportError):
                tmux_transport.create(self._short_socket_path(), self.cwd, ["claude"], {"PATH": "/usr/bin"})
            run.assert_not_called()

        long_home = self.root / ("long-home-" * 12)
        long_path = long_home / "runtime" / "tmux" / f"{uuid4()}.sock"
        with patch.object(core, "HOME", long_home), \
                patch.object(tmux_transport, "_MAX_MACOS_SOCKET_PATH_BYTES", 103), \
                patch.object(tmux_transport.subprocess, "run") as run:
            with self.assertRaisesRegex(tmux_transport.TmuxTransportError, "AF_UNIX"):
                tmux_transport.attach_argv(self._binding(long_path))
            run.assert_not_called()

    def test_inspect_returns_identity_cursor_and_geometry_without_pane_content(self) -> None:
        path = self._prepared_socket_path()
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run(args, **kwargs):
            calls.append((list(args), kwargs))
            return self._result(args, self._inspection_output(command="node"))

        with patch.object(tmux_transport, "_require_private_socket"), \
                patch.object(tmux_transport.subprocess, "run", side_effect=fake_run):
            observed = tmux_transport.inspect(self._binding(path))

        self.assertEqual(observed, {
            **self._binding(path),
            "cursor_x": 5,
            "cursor_y": 3,
            "pane_width": 120,
            "pane_height": 40,
            "pane_current_command": "node",
        })
        self.assertEqual(calls, [(self._inspect_command(path), {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "strict",
            "check": False,
        })])
        self.assertEqual(tmux_transport.attach_argv(observed), [
            "tmux", "-S", str(path), "attach-session", "-t", "continuity",
        ])

    def test_inspect_rejects_wrong_or_nonunique_pane_target(self) -> None:
        path = self._prepared_socket_path()
        wrong = self._inspection_output(pane_id="%9")
        with patch.object(tmux_transport, "_require_private_socket"), \
                patch.object(tmux_transport.subprocess, "run", return_value=self._result([], wrong)):
            with self.assertRaises(tmux_transport.TmuxTransportError) as raised:
                tmux_transport.inspect(self._binding(path))
        self.assertNotIn("%9", str(raised.exception))

        multiple = self._inspection_output() + self._inspection_output(pane_id="%8", pane_pid=124)
        with patch.object(tmux_transport, "_require_private_socket"), \
                patch.object(tmux_transport.subprocess, "run", return_value=self._result([], multiple)):
            with self.assertRaises(tmux_transport.TmuxTransportError):
                tmux_transport.inspect(self._binding(path))

    def test_inspect_rejects_non_socket_path_before_client(self) -> None:
        path = self._prepared_socket_path()
        path.write_text("not a socket")
        with patch.object(tmux_transport.subprocess, "run") as run:
            with self.assertRaises(tmux_transport.TmuxTransportError):
                tmux_transport.inspect(self._binding(path))
            run.assert_not_called()

    def test_capture_reads_visible_pane_after_inspect_without_joining_wrapped_lines(self) -> None:
        path = self._prepared_socket_path()
        responses = [
            self._result([], self._inspection_output()),
            self._result([], "最后一屏\n原样保留\n"),
        ]
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return responses.pop(0)

        with patch.object(tmux_transport, "_require_private_socket"), \
                patch.object(tmux_transport.subprocess, "run", side_effect=fake_run):
            captured = tmux_transport.capture(self._binding(path))

        self.assertEqual(captured, "最后一屏\n原样保留\n")
        self.assertEqual(calls, [
            self._inspect_command(path),
            ["tmux", "-S", str(path), "capture-pane", "-p", "-t", "%7"],
        ])
        self.assertTrue(all("-J" not in command for command in calls))


if __name__ == "__main__":
    unittest.main()
