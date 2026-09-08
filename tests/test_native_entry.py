"""cclaude 原生入口保持 argv、流、退出码和交互分流。"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "claude_context_continuity"
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import native_entry  # noqa: E402


class _ExecCalled(Exception):
    pass


class _TTY:
    def isatty(self) -> bool:
        return True


_DEFAULT_SHAPE = object()


class NativeEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        native = self.directory / "claude"
        native.write_text(f"#!{sys.executable}\n" +
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd()}))\n"
            "print('native-stderr', file=sys.stderr)\n"
            "sys.exit(int(os.environ.get('NATIVE_TEST_EXIT', '0')))\n")
        native.chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.directory) + os.pathsep + os.environ.get("PATH", "")}

    @staticmethod
    def _shape(commands=(), *, flags=(), required=(), optional=(), required_variadic=(), optional_variadic=()):
        forms = {name: ("none", False) for name in flags}
        forms.update({name: ("required", False) for name in required})
        forms.update({name: ("optional", False) for name in optional})
        forms.update({name: ("required", True) for name in required_variadic})
        forms.update({name: ("optional", True) for name in optional_variadic})
        return frozenset(commands), forms

    def native(self, argv, *, exit_code=0):
        result = subprocess.run([sys.executable, "-B", "-m", "claude_context_continuity.native_entry", *argv],
                                input="原生标准输入\n", capture_output=True, text=True, cwd=self.directory,
                                env={**self.env, "PYTHONPATH": str(ROOT / "src"),
                                     "CLAUDE_CONFIG_DIR": str(self.directory / "isolated-config"),
                                     "NATIVE_TEST_EXIT": str(exit_code)}, timeout=15)
        self.assertEqual(result.returncode, exit_code, result.stderr)
        self.assertEqual(result.stderr, "native-stderr\n")
        payload = json.loads(result.stdout)
        self.assertEqual(payload, {"argv": argv, "stdin": "原生标准输入\n", "cwd": str(self.directory)})

    def _entry(self, argv, *, shape=_DEFAULT_SHAPE):
        native_entry._native_cli_shape.cache_clear()
        shape = self._shape() if shape is _DEFAULT_SHAPE else shape
        executed = []

        def execvp(name, command):
            executed.append((name, command))
            raise _ExecCalled

        with patch.object(native_entry.sys, "argv", ["native_entry.py", *argv]), \
                patch.object(native_entry.sys, "stdin", _TTY()), \
                patch.object(native_entry.sys, "stdout", _TTY()), \
                patch.object(native_entry, "_native_cli_shape", return_value=shape), \
                patch.object(native_entry.os, "execvp", side_effect=execvp):
            with self.assertRaises(_ExecCalled):
                native_entry.main()
        return executed

    def _managed_entry(self, argv, *, shape):
        native_entry._native_cli_shape.cache_clear()
        with patch.object(native_entry.sys, "argv", ["native_entry.py", *argv]), \
                patch.object(native_entry.sys, "stdin", _TTY()), \
                patch.object(native_entry.sys, "stdout", _TTY()), \
                patch.object(native_entry, "_native_cli_shape", return_value=shape), \
                patch("claude_context_continuity.tui_runtime.run", return_value={"status": "started"}) as run:
            self.assertEqual(native_entry.main(), 0)
        run.assert_called_once_with(os.getcwd(), native_args=argv)

    def test_native_help_version_and_subcommands_preserve_streams(self):
        for argv in (["--help"], ["--version"], ["mcp", "--help"], ["agents", "--help"], ["auth", "--help"]):
            with self.subTest(argv=argv):
                self.native(argv)

    def test_native_model_permission_and_settings_arguments_are_not_reinterpreted(self):
        self.native(["--model", "user-model", "--effort", "high", "--permission-mode", "default",
                     "--settings", '{"env":{"USER_VALUE":"unchanged"}}', "--tools", "Read,Edit",
                     "--append-system-prompt", "用户自己的提示", "--help"])

    def test_native_print_keeps_input_output_and_exit_status(self):
        self.native(["-p", "--output-format", "json", "hello"], exit_code=7)

    def test_non_tty_native_resume_and_prompt_are_passed_through(self):
        for argv in (["--resume", "native-session"], ["--continue"], ["直接传入的任务"], ["--", "--help"]):
            with self.subTest(argv=argv):
                self.native(argv)

    def test_help_derived_native_subcommands_exec_without_tui_wrapping(self):
        shape = self._shape({"agents", "auth", "mcp", "plugin"}, required=("--model",))
        for argv in (["mcp", "list"], ["auth", "status"], ["agents"], ["--model", "user-model", "plugin", "list"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._entry(argv, shape=shape), [("claude", ["claude", *argv])])

    def test_native_background_and_cloud_modes_exec_without_tui_wrapping(self):
        for argv in (["--bg", "--model", "user-model"], ["--background"], ["--cloud", "existing-session"], ["--tmux"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._entry(argv), [("claude", ["claude", *argv])])

    def test_unreadable_native_help_preserves_an_arg_bearing_native_invocation(self):
        argv = ["future-native-command", "argument"]
        self.assertEqual(self._entry(argv, shape=None), [("claude", ["claude", *argv])])

    def test_end_marker_and_required_operand_keep_print_literal_in_tui(self):
        shape = self._shape(required=("--system-prompt",))
        for argv in (["--", "--print"], ["--system-prompt", "--print"]):
            with self.subTest(argv=argv):
                self._managed_entry(argv, shape=shape)

    def test_optional_debug_does_not_consume_a_following_option_before_subcommand(self):
        argv = ["--debug", "--model", "fable", "mcp", "list"]
        shape = self._shape({"mcp"}, required=("--model",), optional=("--debug",))
        self.assertEqual(self._entry(argv, shape=shape), [("claude", ["claude", *argv])])

    def test_native_option_after_a_positional_prompt_is_not_hidden_in_tui(self):
        argv = ["normal prompt", "--print"]
        shape = self._shape(flags=("--print",))
        self.assertEqual(self._entry(argv, shape=shape), [("claude", ["claude", *argv])])

    def test_environment_launch_mode_execs_native_without_remote_invocation(self):
        argv = ["--environment", "env-local-fixture"]
        shape = self._shape(required=("--environment",))
        self.assertEqual(self._entry(argv, shape=shape), [("claude", ["claude", *argv])])

    def test_interactive_argv_reaches_tui_unchanged(self):
        session_id, resumed_id = str(uuid4()), str(uuid4())
        caller_argv = [
            "--settings", '{"env":{"USER_VALUE":"unchanged"}}',
            "--system-prompt", "caller system prompt",
            "--append-system-prompt", "caller append prompt",
            "--plugin-dir", "/caller/plugin",
            "--model", "user-model",
            "--tools", "Read,Edit",
            "--permission-mode", "manual",
            "--session-id", session_id,
            "--resume", resumed_id,
        ]
        shape = self._shape(
            {"mcp"},
            required=("--settings", "--system-prompt", "--append-system-prompt", "--plugin-dir", "--model",
                      "--permission-mode", "--session-id"),
            required_variadic=("--tools",),
            optional=("--resume",),
        )
        self._managed_entry(caller_argv, shape=shape)

    def test_help_shape_parsing_handles_aliases_required_optional_and_variadic_operands(self):
        native_entry._native_cli_shape.cache_clear()
        help_text = """Usage: claude [options] [command] [prompt]\n\nOptions:\n  -d, --debug [filter]  Debug\n  --chrome              Chrome\n  --model <model>       Model\n  --add-dir <directories...> Directories\n\nCommands:\n  mcp                   Manage MCP\n  plugin|plugins        Manage plugins\n  stop|kill <id>        Stop session\n                                        terminal output continues here\n"""
        with patch.object(native_entry.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=help_text)):
            commands, forms = native_entry._native_cli_shape()
        self.assertEqual(commands, frozenset({"mcp", "plugin", "plugins", "stop", "kill"}))
        self.assertEqual(forms["-d"], ("optional", False))
        self.assertEqual(forms["--debug"], ("optional", False))
        self.assertEqual(forms["--chrome"], ("none", False))
        self.assertEqual(forms["--model"], ("required", False))
        self.assertEqual(forms["--add-dir"], ("required", True))

    def test_canonical_runtime_has_no_legacy_executor_imports(self):
        legacy = {"managed", "task_mode", "interactive", "session_observer", "budget_math"}
        paths = [PACKAGE / name for name in (
            "core.py", "context_runtime.py", "continuity.py", "native_entry.py", "tui_runtime.py", "tmux_transport.py",
        )]
        self.assertTrue(all(not (PACKAGE / name).exists() for name in (
            "managed.py", "task_mode.py", "interactive.py", "session_observer.py", "budget_math.py",
        )))
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(item.name.split(".")[0] for item in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            with self.subTest(path=path):
                self.assertFalse(imported & legacy)


if __name__ == "__main__":
    unittest.main()
