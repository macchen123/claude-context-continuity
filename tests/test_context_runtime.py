"""纯 observe_budget 预算观察检查，不构造或启动 headless runtime。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity.context_runtime import ContextRuntimeError, observe_budget  # noqa: E402


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
        self.assertEqual(observed["guard_tokens"], 81536)
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
