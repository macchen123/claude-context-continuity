"""Budget observation and persisted-state contract checks; no native runtime is launched."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity import core  # noqa: E402
from claude_context_continuity.context_runtime import ContextRuntime, ContextRuntimeError, observe_budget  # noqa: E402


class RuntimeStateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ROOT / ".cache")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        home = patch.object(core, "HOME", self.root / "state")
        home.start()
        self.addCleanup(home.stop)
        self.config = {"hash": "state-contract", "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000"}}
        self.runtime = ContextRuntime.create(cwd=self.root, session_id=str(uuid4()), configuration=self.config,
                                             configuration_reader=lambda _: self.config)

    def test_only_actual_current_protocol_phases_round_trip_without_rewrite(self) -> None:
        for phase in ("created", "running", "rotation_requested", "waiting_safe_boundary", "clear_sent",
                      "awaiting_tui_prompt", "continuation_dispatching", "awaiting_continuation", "closed"):
            with self.subTest(phase=phase):
                state = self.runtime._state()
                state["phase"] = phase
                self.runtime._save(state)
                before = self.runtime.state_path.read_bytes()
                stamp = self.runtime.state_path.stat()
                loaded = ContextRuntime.load(self.runtime.conversation_id, configuration_reader=lambda _: self.config)
                self.assertEqual(loaded.receipt()["phase"], phase)
                self.assertEqual(self.runtime.state_path.read_bytes(), before)
                after = self.runtime.state_path.stat()
                self.assertEqual((after.st_ino, after.st_mtime_ns), (stamp.st_ino, stamp.st_mtime_ns))

    def test_obsolete_unknown_missing_and_non_string_phases_fail_without_rewrite(self) -> None:
        initial = self.runtime._state()
        for phase in ("paused", "in_progress", "clearing", "future_phase", "", None, True, 0, [], {}, "missing"):
            with self.subTest(phase=phase):
                state = {**initial, "phase": phase}
                if phase == "missing":
                    state.pop("phase")
                core.atomic(self.runtime.state_path, state)
                before = self.runtime.state_path.read_bytes()
                stamp = self.runtime.state_path.stat()
                operations = (lambda: ContextRuntime.load(self.runtime.conversation_id), self.runtime._state,
                              self.runtime.receipt, lambda: self.runtime.request_rotation("Do not migrate this state."))
                with patch.object(self.runtime, "_save") as save:
                    for operation in operations:
                        with self.assertRaisesRegex(ContextRuntimeError, "phase.*unsupported"):
                            operation()
                    save.assert_not_called()
                self.assertEqual(self.runtime.state_path.read_bytes(), before)
                after = self.runtime.state_path.stat()
                self.assertEqual((after.st_ino, after.st_mtime_ns), (stamp.st_ino, stamp.st_mtime_ns))
                self.assertFalse((self.runtime.directory / "notes").exists())

    def test_invalid_phase_cannot_be_persisted_by_the_current_writer(self) -> None:
        state = self.runtime._state()
        state["phase"] = "paused"
        before = self.runtime.state_path.read_bytes()
        with self.assertRaisesRegex(ContextRuntimeError, "phase.*unsupported"):
            self.runtime._save(state)
        self.assertEqual(self.runtime.state_path.read_bytes(), before)

    def test_inert_extra_fields_are_not_rejected_migrated_or_exposed_as_current_receipts(self) -> None:
        state = self.runtime._state()
        extras = {"pause_reason": "historical note", "observation_only_pause": True,
                  "pause_notice_key": "historical-key", "legacy_pause_migrated": True}
        self.assertFalse(extras.keys() & state.keys())
        state.update(extras)
        self.runtime._save(state)
        before = self.runtime.state_path.read_bytes()
        loaded = ContextRuntime.load(self.runtime.conversation_id)
        self.assertEqual(loaded._state(), state)
        self.assertEqual(json.loads(before)["schema"], "claude-context-runtime.v1")
        self.assertFalse(extras.keys() & loaded.receipt().keys())
        self.assertEqual(self.runtime.state_path.read_bytes(), before)
        ContextRuntime._wait(state, "current_native_fact_pending")
        self.assertEqual(state["reconciliation"], {"stage": "created", "reason": "current_native_fact_pending"})
        ContextRuntime._clear_wait(state)
        self.assertEqual({key: state[key] for key in extras}, extras)

    def test_current_waits_do_not_create_obsolete_pause_fields(self) -> None:
        state = self.runtime._state()
        ContextRuntime._wait(state, "current_native_fact_pending")
        ContextRuntime._clear_wait(state)
        self.runtime._save(state)
        for value in (self.runtime._state(), self.runtime.receipt()):
            self.assertFalse({"pause_reason", "observation_only_pause", "pause_notice_key",
                              "legacy_pause_migrated"} & value.keys())


class ObserveBudgetTests(unittest.TestCase):
    def test_fixed_reserve_does_not_predict_from_prior_growth(self) -> None:
        previous = None
        for value in (125, 325, 250, 600, 749):
            observed = observe_budget(
                previous, {"total_input_tokens": value, "cache_fields_complete": True},
                {"tokens": 1000, "actual": True}, 1,
            )
            self.assertEqual(observed["guard_tokens"], 250)
            self.assertEqual(observed["remaining_tokens"], 1000 - value)
            self.assertFalse(observed["near_limit"])
            self.assertEqual(observed["observations"]["window_is_actual"], 1)
            self.assertEqual(observed["observations"]["cache_fields_complete"], 1)
            previous = observed
        self.assertTrue(observe_budget(previous, 750, 1000, 1)["near_limit"])

    def test_native_output_tokens_are_counted_once(self) -> None:
        observed = observe_budget(
            None, {"total_input_tokens": 700, "output_tokens": 50, "cache_fields_complete": True}, 1000, 1)
        self.assertTrue(observed["near_limit"])
        self.assertEqual(observed["remaining_tokens"], 250)
        repeated = observe_budget(
            observed, {"total_input_tokens": 700, "output_tokens": 50}, 1000, 1)
        self.assertEqual(repeated["remaining_tokens"], 250)
        self.assertFalse(repeated["handoff_required"])
        consumed = observe_budget(repeated, {"total_input_tokens": 760, "output_tokens": 10}, 1000, 2)
        self.assertEqual(consumed["remaining_tokens"], 230)

    def test_old_growth_observations_cannot_inflate_reserve(self) -> None:
        old = {"last_input_tokens": 96933, "max_positive_growth_tokens": 500000,
               "bootstrap_input_tokens": 125, "guard_tokens": 580000, "window_tokens": 600000}
        observed = observe_budget(old, 198068, 600000, 1)
        self.assertFalse(observed["near_limit"])
        self.assertEqual(observed["guard_tokens"], 150000)
        self.assertNotIn("max_positive_growth_tokens", observed["observations"])

    def test_handoff_notice_is_once_per_observed_window(self) -> None:
        first = observe_budget(None, 900, 1000, 1)
        self.assertTrue(first["near_limit"])
        self.assertTrue(first["handoff_required"])
        self.assertEqual(first["observations"]["window_is_actual"], 0)
        self.assertEqual(first["observations"]["cache_fields_complete"], 0)

        repeated = observe_budget(first, 900, 1000, 2)
        self.assertTrue(repeated["near_limit"])
        self.assertFalse(repeated["handoff_required"])
        self.assertEqual(repeated["observations"]["handoff_reported"], 1)

        changed_window = observe_budget(repeated, 1800, 2000, 3)
        self.assertTrue(changed_window["handoff_required"])
        self.assertEqual(changed_window["observations"]["window_tokens"], 2000)

    def test_synthetic_records_never_replace_a_real_observation(self) -> None:
        previous = observe_budget(None, {"total_input_tokens": 400, "cache_fields_complete": True}, 1000, 1)
        ignored = observe_budget(previous, {"synthetic": True}, 1000, 2)
        self.assertTrue(ignored["sample_ignored"])
        self.assertFalse(ignored["handoff_required"])
        self.assertEqual(ignored["observations"], previous["observations"])

    def test_invalid_budget_inputs_are_rejected(self) -> None:
        invalid_calls = (
            lambda: observe_budget(None, True, 1000, 1),
            lambda: observe_budget(None, {"total_input_tokens": 1, "cache_fields_complete": "yes"}, 1000, 1),
            lambda: observe_budget(None, 1, 0, 1),
            lambda: observe_budget(None, 1, {"tokens": 1000, "actual": "yes"}, 1),
            lambda: observe_budget({"observations": {"window_is_actual": 2}}, 1, 1000, 1),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ContextRuntimeError):
                    call()


if __name__ == "__main__":
    unittest.main()
