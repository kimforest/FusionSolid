"""Intermediate representation shared by the Fusion extractor, the mapper and
the report.

Invariants:
  * every length is in millimetres;
  * every angle is in degrees;
  * ``Ref`` values are *symbolic* on the way in and are resolved to concrete
    SolidCraft handles by the sink while replaying (see ``refs.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Param:
    """A user parameter."""

    name: str
    expression: str = ""
    value: Optional[float] = None
    unit: str = ""
    comment: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "expression": self.expression,
            "value": self.value,
            "unit": self.unit,
            "comment": self.comment,
        }


@dataclass
class Entity:
    """One sketch curve or point.

    ``kind`` is a normalized kind (``line``, ``circle``, ``arc``, ``ellipse``,
    ``point``, ``spline``, ``conic``); ``data`` holds the kind-specific fields
    (already in mm, in *model* space).
    """

    kind: str
    data: Dict[str, Any] = field(default_factory=dict)
    source_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "data": self.data, "source_id": self.source_id}


@dataclass
class Constraint:
    kind: str
    refs: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "refs": self.refs, "data": self.data}


@dataclass
class Dimension:
    kind: str  # linear | radial | diameter | angular
    value: float
    refs: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "refs": self.refs,
            "data": self.data,
        }


@dataclass
class PlaneRef:
    """Where a sketch lives.

    ``kind`` is ``base`` (``"XY"``/``"XZ"``/``"YZ"``), ``construction`` (the
    symbolic name of a construction-plane step) or ``face`` (a geometric
    reference into a body).
    """

    kind: str
    name: str = ""
    geometry: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "geometry": self.geometry}


@dataclass
class Ref:
    """A symbolic reference emitted by the mapper.

    ``kind`` is one of:
      * ``sketch_profile`` -- profile of a sketch step (``target`` = symbol);
      * ``sketch_entity``  -- a curve/point inside a sketch step;
      * ``body``           -- a body produced by a step (``target`` = symbol);
      * ``face`` / ``edge``/ ``vertex`` -- geometry inside a body;
      * ``construction``   -- a construction plane/axis/point step;
      * ``param``          -- a user parameter name.
    """

    kind: str
    target: str = ""
    geometry: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "geometry": self.geometry,
            "note": self.note,
        }


@dataclass
class SketchStep:
    """A sketch, replayed as ``sketch.create`` -> entities/constraints/dims ->
    ``sketch.finish``."""

    symbol: str
    name: str
    plane: PlaneRef
    entities: List[Entity] = field(default_factory=list)
    points: List[Entity] = field(default_factory=list)
    constraints: List[Constraint] = field(default_factory=list)
    dimensions: List[Dimension] = field(default_factory=list)
    suppressed: bool = False
    profile_count: int = 0

    @property
    def step_kind(self) -> str:
        return "sketch"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_kind": "sketch",
            "symbol": self.symbol,
            "name": self.name,
            "plane": self.plane.to_dict(),
            "entities": [e.to_dict() for e in self.entities],
            "points": [p.to_dict() for p in self.points],
            "constraints": [c.to_dict() for c in self.constraints],
            "dimensions": [d.to_dict() for d in self.dimensions],
            "suppressed": self.suppressed,
        }


@dataclass
class FeatureStep:
    """A solid / construction feature in the timeline."""

    symbol: str
    fusion_type: str
    name: str
    props: Dict[str, Any] = field(default_factory=dict)
    refs: List[Ref] = field(default_factory=list)
    input_symbols: List[str] = field(default_factory=list)
    suppressed: bool = False

    @property
    def step_kind(self) -> str:
        return "feature"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_kind": "feature",
            "symbol": self.symbol,
            "fusion_type": self.fusion_type,
            "name": self.name,
            "props": self.props,
            "refs": [r.to_dict() for r in self.refs],
            "input_symbols": self.input_symbols,
            "suppressed": self.suppressed,
        }


Step = Any  # SketchStep | FeatureStep


@dataclass
class BodySummary:
    """Final body geometry, used only for validation."""

    name: str
    volume: Optional[float] = None  # mm^3
    bbox: Optional[Dict[str, float]] = None  # min/max in mm

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "volume": self.volume, "bbox": self.bbox}


@dataclass
class Design:
    name: str = ""
    units: str = "mm"
    params: List[Param] = field(default_factory=list)
    steps: List[Step] = field(default_factory=list)
    bodies: List[BodySummary] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "units": self.units,
            "params": [p.to_dict() for p in self.params],
            "steps": [s.to_dict() for s in self.steps],
            "bodies": [b.to_dict() for b in self.bodies],
            "metadata": self.metadata,
        }
