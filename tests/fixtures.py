"""Shared fixtures for the offline tests."""

from __future__ import annotations

from typing import Any, Dict

from fusion2solid.ir import Design, FeatureStep, Ref, SketchStep, Param, PlaneRef, Entity, Dimension
from fusion2solid.mapper import Catalog

# Every command id the mapper may emit, so a permissive catalog accepts them all
# (used to test mapping logic independently of the live SolidCraft catalog).
ALL_COMMANDS = {
    "set_parameter", "sketch.create", "sketch.finish", "sketch.dimension",
    "sketch.line", "sketch.circle.center", "sketch.arc.center_point",
    "sketch.ellipse", "sketch.point", "sketch.constraint.coincident",
    "sketch.constraint.equal", "sketch.constraint.horizontal_vertical",
    "solid.extrude", "solid.revolve", "solid.fillet", "solid.chamfer",
    "solid.hole", "solid.shell", "solid.combine", "solid.pattern.circular",
    "solid.pattern.rectangular", "solid.mirror", "solid.move", "solid.remove",
}


def permissive_catalog() -> Catalog:
    return Catalog(listed=ALL_COMMANDS)


def _sketch_with_frame(symbol: str, frame: Dict[str, Any]) -> SketchStep:
    return SketchStep(
        symbol=symbol,
        name="Sketch-" + symbol,
        plane=PlaneRef(kind="base", name="XY", geometry={"frame": frame}),
    )


def sample_design() -> Design:
    design = Design(name="Bracket", units="mm")
    design.params.append(Param(name="width", expression="40 mm", value=40.0, unit="mm"))

    sketch = SketchStep(
        symbol="s0",
        name="Sketch1",
        plane=PlaneRef(kind="base", name="XY"),
    )
    sketch.entities.append(Entity(kind="line", data={"start": [0, 0, 0], "end": [40, 0, 0]}))
    sketch.entities.append(Entity(kind="circle", data={"center": [20, 0, 0], "radius": 5.0}))
    sketch.dimensions.append(Dimension(kind="linear", value=40.0, refs=["s0:e0"]))

    design.steps.append(sketch)

    extrude = FeatureStep(
        symbol="s1",
        fusion_type="ExtrudeFeature",
        name="Extrude1",
        props={"distance": 20.0, "operation": "JoinFeatureOperation", "taperAngle": 0.0},
        refs=[Ref(kind="sketch_profile", target="s0", note="profile")],
        input_symbols=["s0"],
    )
    design.steps.append(extrude)
    return design


def sample_raw() -> Dict[str, Any]:
    return {
        "design": {"name": "Bracket", "units": "mm"},
        "unit_note": "lengths are mm, angles are deg",
        "parameters": [
            {"name": "width", "expression": "40 mm", "value": 4.0, "unit": "mm", "comment": ""},
        ],
        "timeline": [
            {
                "index": 0,
                "name": "Sketch1",
                "type": "adsk::fusion::Sketch",
                "kind": "sketch",
                "token": "T0",
                "suppressed": False,
                "error": "",
                "sketch": {
                    "curves": [
                        {"kind": "line", "data": {"start": [0, 0, 0], "end": [40, 0, 0]},
                         "token": "c0", "construction": False},
                        {"kind": "circle", "data": {"center": [20, 0, 0], "radius": 5.0},
                         "token": "c1", "construction": False},
                    ],
                    "points": [],
                    "constraints": [{"type": "adsk::fusion::CoincidentConstraint", "tokens": ["c0", "c1"]}],
                    "dimensions": [
                        {"type": "SketchLinearDimension", "tokens": ["c0"],
                         "length_mm": 40.0, "raw_value": 4.0, "parameter": "width"},
                    ],
                    "plane": {
                        "kind": "adsk::fusion::ConstructionPlane",
                        "name": "XY",
                        "frame": {"origin": [0, 0, 0], "x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]},
                    },
                    "profile_count": 1,
                },
            },
            {
                "index": 1,
                "name": "Extrude1",
                "type": "adsk::fusion::ExtrudeFeature",
                "kind": "feature",
                "token": "T1",
                "suppressed": False,
                "error": "",
                "props": {"distance": 20.0, "operation": "JoinFeatureOperation"},
                "refs": [
                    {"attr": "profile", "token": "P0",
                     "geometry": {"object_type": "adsk::fusion::Profile", "centroid": [20, 0, 0],
                                  "area_mm2": 800.0, "sketch_token": "T0"}},
                ],
            },
        ],
        "bodies": [{"name": "Body1", "volume": 16000.0, "bbox": None}],
    }
