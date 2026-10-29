import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import permissive_catalog, sample_design  # noqa: E402
from fusion2solid.mapper import map_design  # noqa: E402
from fusion2solid.report import build_report, render_markdown  # noqa: E402
from fusion2solid.solidcraft_sink import StepOutcome, _substitute  # noqa: E402


class SubstituteTest(unittest.TestCase):
    def test_substitutes_placeholders_in_nested_structures(self):
        resolver = lambda token: {"r0": "Body1"}.get(token.strip("@{ }"), token)
        params = {"bodies": ["@{r0}"], "opts": {"nested": "@{r0}"}, "keep": 5}
        out = _substitute(params, resolver)
        self.assertEqual(out["bodies"], ["Body1"])
        self.assertEqual(out["opts"]["nested"], "Body1")
        self.assertEqual(out["keep"], 5)

    def test_leaves_non_placeholders_alone(self):
        out = _substitute({"a": "Plain", "b": [1, 2]}, lambda token: token)
        self.assertEqual(out, {"a": "Plain", "b": [1, 2]})


class ReportTest(unittest.TestCase):
    def test_report_counts_and_markdown(self):
        design = sample_design()
        plan = map_design(design, permissive_catalog())
        outcomes = [
            StepOutcome(0, "s0", "mapped"),
            StepOutcome(1, "s1", "failed", error="solid.extrude failed"),
        ]
        report = build_report(
            design, plan, outcomes, "http://127.0.0.1:27182/mcp",
            ["solidcraft-cli", "mcp"], discovery={"strategy": "script"},
            validation={"fusion_total_volume_mm3": 16000.0}, dry_run=False,
        )
        self.assertEqual(report["summary"]["timeline_steps"], 2)
        self.assertEqual(report["summary"]["mapped"], 1)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(len(report["unmapped"]), 1)

        markdown = render_markdown(report)
        self.assertIn("# Fusion -> SolidCraft migration report", markdown)
        self.assertIn("solid.extrude failed", markdown)
        self.assertIn("| failed | 1 |", markdown)

    def test_dry_run_flag_recorded(self):
        design = sample_design()
        plan = map_design(design, permissive_catalog())
        report = build_report(design, plan, [], "ep", ["cli"], dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertIn("dry run", render_markdown(report))


if __name__ == "__main__":
    unittest.main()
