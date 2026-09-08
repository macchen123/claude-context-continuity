"""纯 observe_budget 预算观察检查，不构造或启动 headless runtime。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_context_continuity.context_runtime import ContextRuntimeError, observe_budget  # noqa: E402


class ObserveBudgetTests(unittest.TestCase):
    def test_tracks_actual_input_cache_and_largest_positive_growth(self) -> None:
        bootstrap = observe_budget(
            None,
            {"total_input_tokens": 125, "cache_fields_complete": True},
            {"tokens": 1000, "actual": True},
            1,
        )
        self.assertEqual(bootstrap["observations"]["bootstrap_input_tokens"], 125)
        self.assertEqual(bootstrap["observations"]["max_positive_growth_tokens"], 0)
        self.assertEqual(bootstrap["observations"]["window_is_actual"], 1)
        self.assertEqual(bootstrap["observations"]["cache_fields_complete"], 1)
        self.assertFalse(bootstrap["handoff_required"])

        grown = observe_budget(
            bootstrap,
            {"total_input_tokens": 325, "cache_fields_complete": True},
            {"tokens": 1000, "actual": True},
            2,
        )
        self.assertEqual(grown["observations"]["max_positive_growth_tokens"], 200)
        self.assertEqual(grown["guard_tokens"], 450)

        declined = observe_budget(
            grown["observations"],
            {"total_input_tokens": 250, "cache_fields_complete": True},
            {"tokens": 1000, "actual": True},
            3,
        )
        self.assertEqual(declined["observations"]["last_input_tokens"], 250)
        self.assertEqual(declined["observations"]["max_positive_growth_tokens"], 200)
        self.assertFalse(declined["handoff_required"])

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
