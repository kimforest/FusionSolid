import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import sample_raw  # noqa: E402
from fusion2solid.fusion_script import EXTRACTION_SCRIPT  # noqa: E402
from fusion2solid.fusion_source import _parse_script_result, raw_to_design  # noqa: E402
from fusion2solid.ir import FeatureStep, SketchStep  # noqa: E402
from fusion2solid.mcp_client import ToolResult  # noqa: E402


class ExtractionScriptTest(unittest.TestCase):
    def test_script_is_valid_python(self):
        compile(EXTRACTION_SCRIPT, "<fusion_extraction>", "exec")

    def test_script_prints_json(self):
        self.assertIn("json.dumps(result)", EXTRACTION_SCRIPT)
        self.assertIn("adsk.fusion.Design.cast", EXTRACTION_SCRIPT)
        # Fusion's host calls run(context); the entry point must accept it.
        self.assertIn("def run(*args, **kwargs)", EXTRACTION_SCRIPT)


def _result(text):
    return ToolResult({"content": [{"type": "text", "text": text}]})


class PayloadParsingTest(unittest.TestCase):
    def test_plain_payload(self):
        payload = {"design": {"name": "P"}, "timeline": [], "parameters": []}
        parsed = _parse_script_result(_result(json.dumps(payload)))
        self.assertEqual(parsed["design"]["name"], "P")

    def test_payload_nested_in_a_wrapper_string(self):
        payload = {"design": {"name": "Q"}, "timeline": [], "parameters": []}
        wrapper = {"success": True, "result": json.dumps(payload)}
        parsed = _parse_script_result(_result(json.dumps(wrapper)))
        self.assertEqual(parsed["design"]["name"], "Q")

    def test_payload_embedded_in_surrounding_text(self):
        payload = {"design": {"name": "R"}, "timeline": [], "parameters": []}
        text = "Script finished.\n" + json.dumps(payload) + "\nDone."
        parsed = _parse_script_result(_result(text))
        self.assertEqual(parsed["design"]["name"], "R")


class RawToDesignTest(unittest.TestCase):
    def setUp(self):
        self.design = raw_to_design(sample_raw())

    def test_design_header(self):
        self.assertEqual(self.design.name, "Bracket")
        self.assertEqual(self.design.units, "mm")

    def test_parameters(self):
        self.assertEqual(len(self.design.params), 1)
        self.assertEqual(self.design.params[0].name, "width")
        self.assertEqual(self.design.params[0].expression, "40 mm")

    def test_sketch_step(self):
        step = self.design.steps[0]
        self.assertIsInstance(step, SketchStep)
        self.assertEqual(step.symbol, "s0")
        self.assertEqual(step.plane.kind, "base")
        self.assertEqual(step.plane.name, "XY")
        self.assertEqual(len(step.entities), 2)
        self.assertEqual(step.entities[0].kind, "line")

    def test_sketch_constraint_and_dimension_tokens_resolve_to_symbols(self):
        step = self.design.steps[0]
        self.assertEqual(len(step.constraints), 1)
        self.assertEqual(step.constraints[0].kind, "CoincidentConstraint")
        self.assertEqual(step.constraints[0].refs, ["s0:e0", "s0:e1"])
        self.assertEqual(len(step.dimensions), 1)
        self.assertEqual(step.dimensions[0].refs, ["s0:e0"])
        self.assertAlmostEqual(step.dimensions[0].value, 40.0)

    def test_feature_step_type_is_namespace_stripped(self):
        step = self.design.steps[1]
        self.assertIsInstance(step, FeatureStep)
        self.assertEqual(step.fusion_type, "ExtrudeFeature")
        self.assertEqual(step.props["distance"], 20.0)

    def test_profile_reference_points_at_the_sketch_symbol(self):
        step = self.design.steps[1]
        self.assertEqual(len(step.refs), 1)
        ref = step.refs[0]
        self.assertEqual(ref.kind, "sketch_profile")
        self.assertEqual(ref.target, "s0")
        self.assertIn("s0", step.input_symbols)

    def test_bodies_captured_for_validation(self):
        self.assertEqual(len(self.design.bodies), 1)
        self.assertEqual(self.design.bodies[0].name, "Body1")
        self.assertAlmostEqual(self.design.bodies[0].volume, 16000.0)


if __name__ == "__main__":
    unittest.main()
