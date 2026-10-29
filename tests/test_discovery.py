import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fusion2solid.discovery import build_script_arguments, classify, summarize_design  # noqa: E402


def fusion_execute_tool():
    return {
        "name": "fusion_mcp_execute",
        "description": "Execute operations in the active Fusion model.",
        "inputSchema": {
            "type": "object",
            "required": ["featureType", "object"],
            "properties": {
                "featureType": {"type": "string", "enum": ["script", "document", "purchase"]},
                "object": {
                    "type": "object",
                    "properties": {"script": {"type": "string"}, "readOnly": {"type": "boolean"}},
                },
            },
        },
    }


def electronics_read_tool():
    return {
        "name": "fusion_mcp_electronics_read",
        "description": "Read Fusion Electronics design data.",
        "inputSchema": {
            "type": "object",
            "required": ["entity_type"],
            "properties": {"entity_type": {"type": "string", "enum": ["electronics.Board"]}},
        },
    }


class FusionExecuteTest(unittest.TestCase):
    def test_fusion_execute_is_recognized_as_the_script_tool(self):
        disc = classify([fusion_execute_tool()])
        self.assertEqual(disc.strategy, "script")
        self.assertEqual(disc.script_tool, "fusion_mcp_execute")
        self.assertEqual(disc.code_path, ["object", "script"])

    def test_script_arguments_place_code_under_object_script(self):
        disc = classify([fusion_execute_tool()])
        args = build_script_arguments(disc, "print(1)")
        self.assertEqual(args["featureType"], "script")
        self.assertEqual(args["object"]["script"], "print(1)")
        self.assertTrue(args["object"]["readOnly"])

    def test_domain_reader_is_not_chosen_over_execute(self):
        disc = classify([electronics_read_tool(), fusion_execute_tool()])
        self.assertEqual(disc.script_tool, "fusion_mcp_execute")


class ClassifyTest(unittest.TestCase):
    def test_script_tool_is_detected_with_its_code_parameter(self):
        tools = [
            {"name": "run_fusion_python", "description": "Execute Fusion API Python code",
             "inputSchema": {"type": "object", "properties": {"source": {"type": "string"}},
                             "required": ["source"]}},
        ]
        disc = classify(tools)
        self.assertEqual(disc.strategy, "script")
        self.assertEqual(disc.script_tool, "run_fusion_python")
        self.assertEqual(disc.code_param, "source")

    def test_inspection_only_falls_back_to_structured(self):
        tools = [{"name": "get_timeline", "description": "List the design timeline",
                  "inputSchema": {"type": "object", "properties": {}}}]
        disc = classify(tools)
        self.assertEqual(disc.strategy, "structured")
        self.assertIn("get_timeline", disc.inspection_tools)

    def test_no_usable_tool(self):
        disc = classify([{"name": "ping", "description": "health check"}])
        self.assertEqual(disc.strategy, "none")


class SummarizeTest(unittest.TestCase):
    def test_summary_counts_types_and_kinds(self):
        raw = {
            "design": {"name": "Part", "units": "mm"},
            "parameters": [{"name": "a"}, {"name": "b"}],
            "timeline": [
                {"type": "adsk::fusion::Sketch", "kind": "sketch"},
                {"type": "adsk::fusion::ExtrudeFeature", "kind": "feature"},
                {"type": "adsk::fusion::ExtrudeFeature", "kind": "feature"},
            ],
            "bodies": [{"name": "Body1"}],
        }
        summary = summarize_design(raw)
        self.assertEqual(summary["timeline_length"], 3)
        self.assertEqual(summary["parameter_count"], 2)
        self.assertEqual(summary["body_count"], 1)
        self.assertEqual(summary["feature_types"]["adsk::fusion::ExtrudeFeature"], 2)
        self.assertEqual(summary["step_kinds"], {"sketch": 1, "feature": 2})


if __name__ == "__main__":
    unittest.main()
