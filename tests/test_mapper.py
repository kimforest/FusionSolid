import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import _sketch_with_frame, permissive_catalog, sample_design  # noqa: E402
from fusion2solid.ir import FeatureStep, Ref  # noqa: E402
from fusion2solid.mapper import Catalog, map_design  # noqa: E402


class MapDesignTest(unittest.TestCase):
    def test_parameter_becomes_set_parameter(self):
        plan = map_design(sample_design(), permissive_catalog())
        self.assertEqual(len(plan.params), 1)
        command = plan.params[0]
        self.assertEqual(command.command, "set_parameter")
        self.assertEqual(command.params["name"], "width")
        self.assertEqual(command.params["expression"], "40 mm")

    def test_sketch_replays_as_create_entities_finish(self):
        plan = map_design(sample_design(), permissive_catalog())
        sketch = plan.steps[0]
        self.assertEqual(sketch.status, "mapped")
        ids = [c.command for c in sketch.commands]
        self.assertEqual(ids[0], "sketch.create")
        self.assertEqual(ids[-1], "sketch.finish")
        self.assertIn("sketch.line", ids)
        self.assertIn("sketch.circle.center", ids)
        # The plane is a base plane, so it is a plain string, not a placeholder.
        self.assertEqual(sketch.commands[0].params["plane"], "XY")

    def test_sketch_entity_coordinates_are_projected_to_local_2d(self):
        plan = map_design(sample_design(), permissive_catalog())
        sketch = plan.steps[0]
        line = next(c for c in sketch.commands if c.command == "sketch.line")
        self.assertEqual(line.params["points"], [[0.0, 0.0], [40.0, 0.0]])

    def test_dimension_references_are_placeholders(self):
        plan = map_design(sample_design(), permissive_catalog())
        sketch = plan.steps[0]
        dim = next(c for c in sketch.commands if c.command == "sketch.dimension")
        entities = dim.params["entities"]
        self.assertTrue(entities and str(entities[0]).startswith("@{r"))
        self.assertIn(entities[0], sketch.refs)

    def test_extrude_maps_with_operation_and_profile_value(self):
        plan = map_design(sample_design(), permissive_catalog())
        extrude = plan.steps[1]
        self.assertEqual(extrude.status, "mapped")
        command = extrude.commands[0]
        self.assertEqual(command.command, "solid.extrude")
        self.assertEqual(command.params["distance"], 20.0)
        self.assertEqual(command.params["operation"], "join")
        # No frame and no profile centroid in this fixture: falls back to "all".
        self.assertEqual(command.params["profiles"], "all")

    def test_profile_becomes_a_sketch_point_when_the_frame_is_known(self):
        from fusion2solid.ir import Design, FeatureStep, PlaneRef, Ref

        design = Design(name="p")
        sketch = _sketch_with_frame("s0", {"origin": [0, 0, 0], "x": [1, 0, 0], "y": [0, 1, 0]})
        design.steps.append(sketch)
        design.steps.append(
            FeatureStep(
                symbol="s1", fusion_type="ExtrudeFeature", name="E",
                props={"distance": 10.0},
                refs=[Ref(kind="sketch_profile", target="s0", note="profile",
                          geometry={"centroid": [30.0, 5.0, 0.0]})],
                input_symbols=["s0"],
            )
        )
        plan = map_design(design, permissive_catalog())
        params = plan.steps[1].commands[0].params
        self.assertEqual(params["profiles"], [{"point": [30.0, 5.0]}])

    def test_unknown_schema_drops_unlisted_parameters(self):
        catalog = Catalog(
            described={
                "solid.extrude": {
                    "input": {"type": "object", "properties": {"distance": {}, "operation": {}},
                              "required": ["distance"]}
                }
            },
            listed={"set_parameter", "sketch.create", "sketch.finish", "sketch.dimension",
                    "sketch.line", "sketch.circle.center", "solid.extrude"},
        )
        plan = map_design(sample_design(), catalog)
        extrude = plan.steps[1]
        params = extrude.commands[0].params
        self.assertIn("distance", params)
        self.assertNotIn("taper_angle", params)  # not in the schema
        self.assertNotIn("profiles", params)     # extrude schema has no profiles key

    def test_unknown_fusion_feature_is_skipped(self):
        from fusion2solid.ir import Design

        design = Design(name="x")
        design.steps.append(
            FeatureStep(symbol="s0", fusion_type="BaseFeature", name="Insert1")
        )
        plan = map_design(design, permissive_catalog())
        self.assertEqual(plan.steps[0].status, "skipped")
        self.assertIn("BaseFeature", plan.steps[0].reason)

    def test_arc_parameters_survive_the_doc_line_sanitizer(self):
        # The real doc line has no colon after "center, start, end".
        catalog = Catalog(
            described={
                "sketch.arc.center_point": {
                    "params": "center, start, end (counter-clockwise from start) | "
                              "center, start, sweep: signed degrees; construction?: bool",
                    "input": None,
                },
                "solid.extrude": {"params": "", "input": None},
            },
            listed={"sketch.create", "sketch.finish", "solid.extrude"},
        )
        from fusion2solid.ir import Design, Entity, PlaneRef, SketchStep

        design = Design(name="a")
        sketch = SketchStep(symbol="s0", name="S", plane=PlaneRef(kind="base", name="XY"))
        sketch.entities.append(Entity(kind="arc", data={
            "center": [0.0, 0.0, 0.0], "start": [1.0, 0.0, 0.0], "end": [0.0, 1.0, 0.0]}))
        design.steps.append(sketch)
        plan = map_design(design, catalog)
        arc = next(c for c in plan.steps[0].commands if c.command == "sketch.arc.center_point")
        self.assertEqual(arc.params["center"], [0.0, 0.0])
        self.assertEqual(arc.params["start"], [1.0, 0.0])
        self.assertEqual(arc.params["end"], [0.0, 1.0])

    def test_world_construction_axis_maps_to_an_uppercase_letter(self):
        from fusion2solid.ir import Design, FeatureStep, Ref

        design = Design(name="r")
        design.steps.append(
            FeatureStep(
                symbol="s0", fusion_type="RevolveFeature", name="R",
                props={"angle": 360.0},
                refs=[
                    Ref(kind="sketch_profile", target="s0", note="profile",
                        geometry={"centroid": [1.0, 1.0, 0.0]}),
                    Ref(kind="construction", note="axis",
                        geometry={"object_type": "adsk::fusion::ConstructionAxis", "name": "Y"}),
                ],
            )
        )
        plan = map_design(design, permissive_catalog())
        params = plan.steps[0].commands[0].params
        self.assertEqual(params["axis"], "Y")

    def test_points_implied_by_curves_are_not_recreated(self):
        from fusion2solid.ir import Design, Entity, PlaneRef, SketchStep

        design = Design(name="p")
        sketch = SketchStep(symbol="s0", name="S", plane=PlaneRef(kind="base", name="XY"))
        sketch.entities.append(Entity(kind="line", data={"start": [0, 0, 0], "end": [10, 0, 0]}))
        sketch.points.append(Entity(kind="point", data={"position": [0, 0, 0]}))     # endpoint
        sketch.points.append(Entity(kind="point", data={"position": [5, 5, 0]}))     # lone
        design.steps.append(sketch)
        plan = map_design(design, permissive_catalog())
        points = [c for c in plan.steps[0].commands if c.command == "sketch.point"]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].params["point"], [5.0, 5.0])

    def test_sketch_plane_uses_the_frame_not_the_name(self):
        from fusion2solid.ir import Design, PlaneRef, SketchStep

        design = Design(name="f")
        sketch = SketchStep(
            symbol="s0", name="S",
            plane=PlaneRef(kind="base", name="YZ",
                           geometry={"frame": {"origin": [1.0, 2.0, 3.0],
                                               "x": [0.0, 1.0, 0.0],
                                               "y": [0.0, 0.0, 1.0]}}),
        )
        design.steps.append(sketch)
        plan = map_design(design, permissive_catalog())
        plane = plan.steps[0].commands[0].params["plane"]
        # SolidCraft's named YZ plane is not Fusion's, so the explicit frame wins.
        self.assertEqual(plane, {"origin": [1.0, 2.0, 3.0],
                                 "x_dir": [0.0, 1.0, 0.0], "y_dir": [0.0, 0.0, 1.0]})

    def test_missing_command_marks_step_skipped(self):
        catalog = Catalog(listed={"set_parameter", "sketch.create"})
        plan = map_design(sample_design(), catalog)
        extrude = plan.steps[1]
        self.assertEqual(extrude.status, "skipped")
        self.assertIn("solid.extrude", extrude.reason)

    def test_step_depending_on_skipped_step_is_skipped(self):
        from fusion2solid.ir import Design

        design = Design(name="x")
        design.steps.append(FeatureStep(symbol="s0", fusion_type="BaseFeature", name="Base"))
        design.steps.append(
            FeatureStep(
                symbol="s1", fusion_type="ExtrudeFeature", name="E",
                refs=[Ref(kind="sketch_profile", target="s0", note="profile")],
                input_symbols=["s0"],
            )
        )
        plan = map_design(design, permissive_catalog())
        self.assertEqual(plan.steps[0].status, "skipped")
        self.assertEqual(plan.steps[1].status, "skipped")
        self.assertIn("s0", plan.steps[1].reason)


if __name__ == "__main__":
    unittest.main()
