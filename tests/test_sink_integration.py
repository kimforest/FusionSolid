import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import sample_design  # noqa: E402
from fusion2solid.mapper import map_design  # noqa: E402
from fusion2solid.solidcraft_sink import SolidCraftSink  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_solidcraft.py")


class SinkIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="f2s-test-")
        self.log = os.path.join(self.tmp, "calls.jsonl")
        os.environ["F2S_LOG"] = self.log
        self.sink = SolidCraftSink([sys.executable, FAKE], timeout=30.0,
                                   stderr_path=os.path.join(self.tmp, "stderr.log"))

    def tearDown(self):
        self.sink.close()
        os.environ.pop("F2S_LOG", None)

    def _calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_connect_creates_a_design_and_loads_the_catalog(self):
        self.sink.connect(design_name="Bracket-migrated")
        self.assertTrue(self.sink.catalog.has("solid.extrude"))
        tools = [c["tool"] for c in self._calls()]
        self.assertIn("new_design", tools)

    def test_full_replay_resolves_references(self):
        design = sample_design()
        self.sink.connect(design_name="Bracket-migrated", create=True)
        first = map_design(design, self.sink.catalog)
        touched = [c.command for s in first.steps for c in s.commands] + ["set_parameter"]
        self.sink.describe(touched)
        plan = map_design(design, self.sink.catalog)

        outcomes = self.sink.apply_plan(plan)
        statuses = {o.symbol: o.status for o in outcomes}
        self.assertEqual(statuses["s0"], "mapped")
        self.assertEqual(statuses["s1"], "mapped")

        # The extrude's profile placeholder must have resolved to the sketch the
        # fake reported, and the sketch's entities to the ids it created.
        batches = [c for c in self._calls() if c["tool"] == "batch"]
        all_commands = [cmd for b in batches for cmd in b["arguments"]["commands"]]
        extrude = next(c for c in all_commands if c["command"] == "solid.extrude")
        # The fixture carries no sketch frame, so the profile falls back to all.
        self.assertEqual(extrude["params"]["profiles"], "all")
        dimension = next(c for c in all_commands if c["command"] == "sketch.dimension")
        self.assertEqual(dimension["params"]["entities"], ["entity-0"])

    def test_measure_and_inspect_are_reachable(self):
        self.sink.connect(create=False)
        measure = self.sink.measure()
        self.assertEqual(measure["total"]["volume_mm3"], 16000.0)
        inspect = self.sink.inspect()
        self.assertEqual(len(inspect["bodies"]), 1)


if __name__ == "__main__":
    unittest.main()
