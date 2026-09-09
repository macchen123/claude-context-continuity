"""Public-package import, entry-point, and isolated generated-hook checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from claude_context_continuity import core  # noqa: E402
from claude_context_continuity.tui_runtime import TuiRuntime, _write_plugin  # noqa: E402


class PublicPackageTests(unittest.TestCase):
    @staticmethod
    def _environment(state_root: Path) -> dict[str, str]:
        inherited_path = os.environ.get("PYTHONPATH", "")
        return {
            **os.environ,
            "PYTHONPATH": str(SRC) + (os.pathsep + inherited_path if inherited_path else ""),
            "CLAUDE_CONTEXT_CONTINUITY_DIR": str(state_root),
        }

    def _fresh_config_environment(self, config: Path) -> dict[str, str]:
        environment = self._environment(config / "session-continuity")
        environment.pop("CLAUDE_CONTEXT_CONTINUITY_DIR")
        environment["CLAUDE_CONFIG_DIR"] = str(config)
        return environment

    @staticmethod
    def _module_command(*arguments: str) -> list[str]:
        return [sys.executable, "-B", "-m", "claude_context_continuity", *arguments]

    def test_import_and_module_entry_work_outside_the_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-release-") as temporary:
            root = Path(temporary)
            outside, config = root / "outside", root / "config"
            outside.mkdir()
            environment = self._fresh_config_environment(config)

            imported = subprocess.run(
                [sys.executable, "-B", "-c", "from claude_context_continuity import core; print(core.state_directory())"],
                cwd=outside,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
            self.assertEqual(Path(imported.stdout.strip()), (config / "session-continuity").resolve(strict=False))
            self.assertFalse((config / "session-continuity").exists())

            entry = subprocess.run(
                self._module_command("--help"),
                cwd=outside,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(entry.returncode, 0, entry.stdout + entry.stderr)
            self.assertIn("usage:", entry.stdout)
            self.assertFalse((config / "session-continuity").exists())

    def test_fresh_config_supports_empty_windows_and_notes_without_manual_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-fresh-state-") as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()

            windows_config, windows_id = root / "windows-config", str(uuid4())
            windows_environment = self._fresh_config_environment(windows_config)
            self.assertFalse((windows_config / "session-continuity").exists())
            windows = subprocess.run(
                self._module_command("history-windows", "--context-id", windows_id),
                cwd=outside,
                env=windows_environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(windows.returncode, 0, windows.stdout + windows.stderr)
            self.assertEqual(json.loads(windows.stdout), {
                "context_id": windows_id,
                "coverage": "native_context_history_sources",
                "entries": [],
                "next_offset": None,
                "write_authority": False,
            })
            self.assertFalse((windows_config / "session-continuity").exists())

            notes_config, notes_id = root / "notes-config", str(uuid4())
            notes_environment = self._fresh_config_environment(notes_config)
            self.assertFalse((notes_config / "session-continuity").exists())
            listed = subprocess.run(
                self._module_command("notes", "list", "--context-id", notes_id),
                cwd=outside,
                env=notes_environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
            self.assertEqual(json.loads(listed.stdout), {
                "context_id": notes_id,
                "entries": [],
                "next_offset": None,
                "total": 0,
                "non_authoritative": True,
            })

            written = subprocess.run(
                self._module_command("notes", "write", "plan", "--context-id", notes_id),
                cwd=outside,
                env=notes_environment,
                input="fresh isolated note",
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
            write_result = json.loads(written.stdout)
            self.assertEqual(write_result["context_id"], notes_id)
            self.assertTrue(write_result["created"])

            read = subprocess.run(
                self._module_command("notes", "read", "plan", "--context-id", notes_id),
                cwd=outside,
                env=notes_environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
            self.assertEqual(json.loads(read.stdout)["text"], "fresh isolated note")

    def test_cron_compat_status_and_live_process_restore_guard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-cron-") as temporary:
            root = Path(temporary)
            workspace, state_root = root / "workspace", root / "state"
            workspace.mkdir()
            sid = str(uuid4())
            with patch.object(core, "HOME", state_root):
                runtime = TuiRuntime.create(cwd=workspace, session_id=sid,
                    configuration={"hash": "test", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}})
                with core.lock(runtime.lock_path):
                    state = runtime._state()
                    state["owned_pid"] = os.getpid()
                    state["tmux"] = {"pane_pid": os.getpid()}
                    state["durable_cron_compat"] = {"enabled": True, "scheduler_session_id": sid}
                    runtime._save(state)
            environment = self._environment(state_root)
            status = subprocess.run(self._module_command("cron-compat", "status", "--context-id", runtime.conversation_id),
                cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            self.assertTrue(json.loads(status.stdout)["durable_cron_compat"]["enabled"])
            restore = subprocess.run(self._module_command("cron-compat", "restore", "--context-id", runtime.conversation_id),
                cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(restore.returncode, 2, restore.stdout + restore.stderr)
            self.assertTrue(core.read_json(runtime.state_path)["durable_cron_compat"]["enabled"])

    def test_generated_plugin_module_command_binds_valid_session_start_in_isolated_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="package-hook-") as temporary:
            root = Path(temporary)
            state_root, workspace, outside = root / "state", root / "workspace", root / "outside"
            workspace.mkdir()
            outside.mkdir()
            context_id, initial_session_id, native_session_id = str(uuid4()), str(uuid4()), str(uuid4())
            transcript = root / f"{native_session_id}.jsonl"
            transcript.write_text(json.dumps({
                "type": "user",
                "uuid": "synthetic-user-message",
                "sessionId": native_session_id,
                "message": {"role": "user", "content": "synthetic authorized instruction"},
            }) + "\n", encoding="utf-8")
            configuration = {"hash": "isolated-test-config", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}

            with patch.object(core, "HOME", state_root):
                runtime = TuiRuntime.create(
                    cwd=workspace,
                    session_id=initial_session_id,
                    conversation_id=context_id,
                    configuration=configuration,
                )
                manifest_path = runtime.directory / "plugin" / ".claude-plugin" / "plugin.json"
                _write_plugin(runtime.directory / "plugin", context_id)
                self.assertTrue((runtime.directory / "plugin/commands/renew.md").is_file())
                self.assertFalse((state_root / "commands").exists())
                hook = core.read_json(manifest_path)["hooks"]["SessionStart"][0]["hooks"][0]

            self.assertEqual(hook["command"], sys.executable)
            self.assertEqual(hook["args"][:3], ["-B", "-m", "claude_context_continuity"])
            self.assertNotEqual(initial_session_id, native_session_id)
            environment = self._environment(state_root)
            environment["CLAUDE_CONFIG_DIR"] = str(root / "isolated-config")
            environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "1000"
            event = {
                "hook_event_name": "SessionStart",
                "session_id": native_session_id,
                "cwd": str(workspace),
                "source": "startup",
                "transcript_path": str(transcript),
            }
            result = subprocess.run(
                [hook["command"], *hook["args"]],
                input=json.dumps(event),
                cwd=outside,
                env={**environment, "CLAUDE_CONTINUITY_ID": context_id},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")

            with patch.object(core, "HOME", state_root):
                receipt = runtime.receipt()
            self.assertEqual(receipt["phase"], "running")
            self.assertEqual(receipt["session_id"], native_session_id)
            self.assertEqual(receipt["source_path"], str(transcript))
            self.assertTrue(receipt["initial_session_started"])
            self.assertEqual(receipt["configuration"]["configured_window"], 1000)


if __name__ == "__main__":
    unittest.main()
