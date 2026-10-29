"""Map the IR (Fusion) to SolidCraft commands.

The mapping is data driven (:data:`FEATURE_COMMANDS`, :data:`CONSTRAINT_PARAMS`
and :data:`PROP_PARAM` are tables, not code branches). Command parameters are
*sanitized against the live SolidCraft catalog*: the catalog gives a JSON Schema
for the newer commands and a parameter-doc line for the older ones
(``"edges: [...]; radius: expr; body?"``), and either is turned into a set of
accepted names so a key SolidCraft does not know is dropped and noted rather
than sent.

References to geometry are emitted as placeholders (``@{r0}``) and resolved by
the sink at replay time, because the concrete SolidCraft handles only exist once
the producing steps have run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .ir import Design, Dimension, Entity, FeatureStep, Ref, SketchStep

# --------------------------------------------------------------------------
# Catalog (SolidCraft command surface)
# --------------------------------------------------------------------------

# ``name:``, ``a, b:``, ``radius | diameter:`` and bare ``flip?`` flags.
_DOC_FIELD = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:\s*(?:\||,|/|or|and)\s*[A-Za-z_][A-Za-z0-9_]*)*)"
    r"\s*\??\s*:"
)
_DOC_LEAD = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*(?:\||,|/|or|and)\s*[A-Za-z_][A-Za-z0-9_]*)*)"
)
_DOC_FLAG = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\?")

# Words that look like fields but are prose.
_DOC_STOP = {"e", "g", "i", "n", "x", "y", "z", "mm", "deg", "in", "ft", "rad", "http", "https"}


class Catalog:
    """What the SolidCraft session actually accepts."""

    def __init__(self, described: Optional[Dict[str, Dict[str, Any]]] = None,
                 listed: Optional[Sequence[str]] = None):
        self.described = described or {}
        self.listed = set(listed or [])

    def has(self, command_id: str) -> bool:
        return command_id in self.described or command_id in self.listed

    def property_names(self, command_id: str) -> Optional[set]:
        """Accepted parameter names, from the schema or the doc line."""
        entry = self.described.get(command_id)
        if not entry:
            return None
        schema = entry.get("input")
        if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
            return set(schema["properties"].keys())
        doc = entry.get("params")
        if isinstance(doc, str) and doc.strip():
            names = _names_from_doc(doc)
            if names:
                return names
        params = entry.get("params")
        if isinstance(params, dict):
            return set(params.keys())
        if isinstance(params, list):
            names = set()
            for item in params:
                if isinstance(item, dict) and item.get("name"):
                    names.add(item["name"])
                elif isinstance(item, str):
                    names.add(item)
            return names or None
        return None

    def required_names(self, command_id: str) -> set:
        entry = self.described.get(command_id) or {}
        schema = entry.get("input")
        if isinstance(schema, dict) and isinstance(schema.get("required"), list):
            return set(schema["required"])
        return set()

    def choose_key(self, command_id: str, candidates: Sequence[str], default: Optional[str]) -> Optional[str]:
        names = self.property_names(command_id)
        if names:
            for candidate in candidates:
                if candidate in names:
                    return candidate
            return default if default in names else None
        return default if default is not None else (candidates[0] if candidates else None)


def _names_from_doc(doc: str) -> set:
    """Parameter names from a command's doc line.

    ``"edges: [...]; radius: expr; body?"`` -> {edges, radius, body};
    ``"center, start, end (counter-clockwise from start) | center, start,
    sweep: signed degrees"`` -> {center, start, end, sweep} (the leading list
    has no colon of its own, so it is taken per ``;`` section as well).
    """
    head = doc.split("\u2014")[0]
    names: set = set()
    for match in _DOC_FIELD.finditer(head):
        _add_doc_names(names, match.group(1))
    for part in head.split(";"):
        match = _DOC_LEAD.match(part)
        if match:
            _add_doc_names(names, match.group(1))
    for match in _DOC_FLAG.finditer(head):
        names.add(match.group(1))
    return {n for n in names if n not in _DOC_STOP}


def _add_doc_names(names: set, text: str) -> None:
    for token in re.split(r"\s*(?:\||,|/|or|and)\s*", text):
        token = token.strip()
        if token:
            names.add(token)


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass
class Command:
    command: str
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"command": self.command, "params": self.params}


@dataclass
class MappedStep:
    index: int
    symbol: str
    source_name: str
    source_type: str
    status: str = "mapped"  # mapped | skipped | failed
    reason: str = ""
    commands: List[Command] = field(default_factory=list)
    refs: Dict[str, Ref] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    confidence: str = ""
    suppressed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "symbol": self.symbol,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "status": self.status,
            "reason": self.reason,
            "commands": [c.to_dict() for c in self.commands],
            "refs": {k: v.to_dict() for k, v in self.refs.items()},
            "notes": self.notes,
            "confidence": self.confidence,
            "suppressed": self.suppressed,
        }


@dataclass
class Plan:
    design_name: str
    params: List[Command] = field(default_factory=list)
    steps: List[MappedStep] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "design_name": self.design_name,
            "params": [c.to_dict() for c in self.params],
            "steps": [s.to_dict() for s in self.steps],
            "skipped": self.skipped,
        }


# --------------------------------------------------------------------------
# Mapping tables
# --------------------------------------------------------------------------

OPERATION_MAP = {
    "JoinFeatureOperation": "join",
    "NewBodyFeatureOperation": "new",
    "CutFeatureOperation": "cut",
    "IntersectFeatureOperation": "intersect",
    "NewComponentFeatureOperation": "new_component",
}

FEATURE_COMMANDS: Dict[str, str] = {
    "ExtrudeFeature": "solid.extrude",
    "RevolveFeature": "solid.revolve",
    "FilletFeature": "solid.fillet",
    "ChamferFeature": "solid.chamfer",
    "HoleFeature": "solid.hole",
    "ShellFeature": "solid.shell",
    "SweepFeature": "solid.sweep",
    "LoftFeature": "solid.loft",
    "PipeFeature": "solid.pipe",
    "CoilFeature": "solid.coil",
    "CombineFeature": "solid.combine",
    "SplitBodyFeature": "solid.split_body",
    "ScaleFeature": "solid.scale",
    "DraftFeature": "solid.draft",
    "ThreadFeature": "solid.thread",
    "RibFeature": "solid.rib",
    "WebFeature": "solid.web",
    "EmbossFeature": "solid.emboss",
    "RemoveFeature": "solid.remove",
    "OffsetFacesFeature": "solid.offset_face",
    "ReplaceFaceFeature": "solid.replace_face",
    "BoundaryFillFeature": "solid.boundary_fill",
    "MoveFeature": "solid.move",
    "CircularPatternFeature": "solid.pattern.circular",
    "RectangularPatternFeature": "solid.pattern.rectangular",
    "PathPatternFeature": "solid.pattern.path",
    "MirrorFeature": "solid.mirror",
    "BoxFeature": "solid.box",
    "CylinderFeature": "solid.cylinder",
    "SphereFeature": "solid.sphere",
    "TorusFeature": "solid.torus",
    "ConeFeature": "solid.cone",
    "ConstructionPlane": "construct.plane",
    "ConstructionAxis": "construct.axis",
    "ConstructionPoint": "construct.point",
}

# Fusion constraint type -> (SolidCraft command, parameter names for its refs).
CONSTRAINT_PARAMS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "CoincidentConstraint": ("sketch.constraint.coincident", ("a", "b")),
    "CollinearConstraint": ("sketch.constraint.collinear", ("a", "b")),
    "ConcentricConstraint": ("sketch.constraint.concentric", ("a", "b")),
    "EqualConstraint": ("sketch.constraint.equal", ("a", "b")),
    "HorizontalConstraint": ("sketch.constraint.horizontal_vertical", ("line",)),
    "VerticalConstraint": ("sketch.constraint.horizontal_vertical", ("line",)),
    "MidPointConstraint": ("sketch.constraint.midpoint", ("point", "line")),
    "ParallelConstraint": ("sketch.constraint.parallel", ("a", "b")),
    "PerpendicularConstraint": ("sketch.constraint.perpendicular", ("a", "b")),
    "SymmetryConstraint": ("sketch.constraint.symmetry", ("a", "b", "line")),
    "TangentConstraint": ("sketch.constraint.tangent", ("a", "b")),
    "FixConstraint": ("sketch.constraint.fix", ("entity",)),
    "GroundConstraint": ("sketch.constraint.fix", ("entity",)),
}

DIMENSION_TYPES = {"linear": "distance", "radial": "radius", "diameter": "diameter", "angular": "angle"}

# Attribute on the Fusion feature -> candidate SolidCraft parameter names.
REF_PARAM_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "profile": ("profiles", "profile", "sketch"),
    "profiles": ("profiles", "profile", "sketch"),
    "path": ("path", "paths", "path_sketch"),
    "axis": ("axis", "axes"),
    "mirrorPlane": ("plane", "mirror_plane"),
    "splittingTool": ("tool", "plane", "body"),
    "targetBody": ("target", "targets", "bodies"),
    "toolBodies": ("tools", "tool_bodies", "bodies"),
    "faces": ("faces",),
    "edges": ("edges",),
    "point": ("position", "point", "points"),
    "inputEntities": ("bodies", "targets", "features"),
}

# Fusion prop -> SolidCraft parameter (applied when the catalog accepts it).
PROP_PARAM: Dict[str, str] = {
    "distance": "distance",
    "radius": "radius",
    "angle": "angle",
    "thickness": "thickness",
    "taperAngle": "taper",
    "holeDiameter": "diameter",
    "scaleFactor": "factor",
    "quantity": "count",
    "quantityOne": "count1",
    "quantityTwo": "count2",
    "distanceOne": "spacing1",
    "offsetDistance": "distance",
    "totalAngle": "total_angle",
    "sectionSize": "size",
    "isFullLength": "full_length",
    "revolutions": "turns",
}

# Commands that are meaningless without a geometry input. Fusion sometimes does
# not expose a feature's inputs at all (a CombineFeature's target/tool bodies
# raise "Didn't roll editing feature back"); such a feature is skipped with a
# reason instead of being replayed with the input missing.
REQUIRED_INPUTS: Dict[str, Tuple[str, ...]] = {
    "solid.combine": ("target",),
    "solid.fillet": ("edges",),
    "solid.chamfer": ("edges",),
    "solid.shell": ("faces",),
    "solid.offset_face": ("faces",),
    "solid.draft": ("faces",),
    "solid.mirror": ("plane",),
    "solid.split_body": ("body",),
    "solid.remove": ("bodies",),
}

# Props that are recorded but are not SolidCraft parameters.
INTERNAL_PROPS = {"edge_set_count", "edge_count", "extentType", "isThinExtrude",
                  "isSymmetric", "distanceTwo", "patternComputeOption"}


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def map_design(design: Design, catalog: Catalog) -> Plan:
    plan = Plan(design_name=design.name)

    for param in design.params:
        params: Dict[str, Any] = {"name": param.name}
        if param.expression:
            params["expression"] = param.expression
        elif param.value is not None:
            params["value"] = param.value
        if param.unit:
            params["unit"] = param.unit
        if param.comment:
            params["comment"] = param.comment
        plan.params.append(Command("set_parameter", params))

    # Sketch frames, so a feature's profile reference can be turned into a
    # sketch point (SolidCraft takes profiles as an index, curve ids or {point}).
    frames = {
        step.symbol: (step.plane.geometry or {}).get("frame")
        for step in design.steps
        if isinstance(step, SketchStep) and isinstance(step.plane.geometry, dict)
    }
    profile_counts = {
        step.symbol: getattr(step, "profile_count", 0)
        for step in design.steps
        if isinstance(step, SketchStep)
    }

    skipped_symbols: set = set()
    for index, step in enumerate(design.steps):
        mapped = _map_step(index, step, catalog, frames, profile_counts)
        blocked = [s for s in getattr(step, "input_symbols", []) or [] if s in skipped_symbols]
        if blocked and mapped.status == "mapped":
            mapped.status = "skipped"
            mapped.reason = "depends on skipped step(s): %s" % ", ".join(sorted(blocked))
        if mapped.status == "skipped":
            skipped_symbols.add(step.symbol)
            plan.skipped.append(step.symbol)
        plan.steps.append(mapped)
    return plan


# --------------------------------------------------------------------------
# Per-step mapping
# --------------------------------------------------------------------------


def _map_step(index: int, step: Any, catalog: Catalog, frames: Dict[str, Any],
              profile_counts: Optional[Dict[str, int]] = None) -> MappedStep:
    if isinstance(step, SketchStep):
        return _map_sketch(index, step, catalog)
    if isinstance(step, FeatureStep):
        return _map_feature(index, step, catalog, frames, profile_counts)
    return MappedStep(index=index, symbol=getattr(step, "symbol", "?"), source_name="",
                      source_type=type(step).__name__, status="skipped",
                      reason="unrecognised step type")


def _map_sketch(index: int, step: SketchStep, catalog: Catalog) -> MappedStep:
    out = MappedStep(index=index, symbol=step.symbol, source_name=step.name,
                     source_type="Sketch", suppressed=step.suppressed)
    if not catalog.has("sketch.create"):
        out.status, out.reason = "skipped", "SolidCraft exposes no sketch.create command"
        return out

    plane_value = _plane_value(step, out)
    out.commands.append(Command("sketch.create", _sanitize("sketch.create", {"plane": plane_value}, catalog)))

    frame = (step.plane.geometry or {}).get("frame") if isinstance(step.plane.geometry, dict) else None

    for entity in step.entities:
        command = _entity_command(entity, frame, out)
        if command is None:
            continue
        if catalog.has(command.command):
            out.commands.append(Command(command.command, _sanitize(command.command, command.params, catalog)))
        else:
            out.notes.append("no SolidCraft command %r for sketch entity %s" % (command.command, entity.kind))

    # Fusion lists every curve endpoint and centre as a sketch point; SolidCraft
    # creates those with the curves, so only lone points are emitted.
    curve_points: List[Sequence[float]] = []
    for entity in step.entities:
        data = entity.data or {}
        for key in ("start", "end", "center"):
            if data.get(key):
                curve_points.append(data[key])
    skipped_points = 0
    for point in step.points:
        position = (point.data or {}).get("position")
        if position is None:
            continue
        if any(_close(position, other) for other in curve_points):
            skipped_points += 1
            continue
        command = _entity_command(point, frame, out)
        if command is not None and catalog.has(command.command):
            out.commands.append(Command(command.command, _sanitize(command.command, command.params, catalog)))
    if skipped_points:
        out.notes.append("%d sketch point(s) implied by curves were not recreated" % skipped_points)

    for constraint in step.constraints:
        _emit_constraint(out, constraint, catalog)

    for dim in step.dimensions:
        _emit_dimension(out, dim, catalog)

    if catalog.has("sketch.finish"):
        out.commands.append(Command("sketch.finish", {}))
    return out


def _emit_constraint(out: MappedStep, constraint, catalog: Catalog) -> None:
    entry = CONSTRAINT_PARAMS.get(constraint.kind)
    if entry is None:
        out.notes.append("constraint %r is not mapped in v1" % constraint.kind)
        return
    command_id, keys = entry
    if not catalog.has(command_id):
        out.notes.append("SolidCraft has no %s" % command_id)
        return
    refs = _curve_targets(out, constraint.refs, "constraint %s" % constraint.kind)
    if len(refs) != len(keys):
        out.notes.append("constraint %r needs %d references but %d could be mapped; skipped"
                         % (constraint.kind, len(keys), len(refs)))
        return
    params: Dict[str, Any] = {}
    for key, target in zip(keys, refs):
        params[key] = _register_ref(out, Ref(kind="sketch_entity", target=target))
    out.commands.append(Command(command_id, _sanitize(command_id, params, catalog)))


def _emit_dimension(out: MappedStep, dim: Dimension, catalog: Catalog) -> None:
    if not catalog.has("sketch.dimension"):
        out.notes.append("no sketch.dimension command")
        return
    refs = _curve_targets(out, dim.refs, "dimension")
    if not refs:
        out.notes.append("dimension on points is not migrated in v1")
        return
    tokens = [_register_ref(out, Ref(kind="sketch_entity", target=t)) for t in refs]
    params: Dict[str, Any] = {"value": dim.value, "entities": tokens}
    dim_type = DIMENSION_TYPES.get(dim.kind)
    if dim_type:
        params["type"] = dim_type
    out.commands.append(Command("sketch.dimension", _sanitize("sketch.dimension", params, catalog)))


_CURVE_SYMBOL = re.compile(r":e\d+$")


def _curve_targets(out: MappedStep, refs: Sequence[str], label: str) -> List[str]:
    """Keep only references to curves we created (a curve id we can trust).

    Fusion entities that are *points* map through the sketch's point list, whose
    order does not line up with SolidCraft's auto-created points, so those are
    dropped rather than mis-applied (SolidCraft already joins points that land
    on existing ones).
    """
    curves = [ref for ref in refs if _CURVE_SYMBOL.search(ref)]
    if len(curves) != len(refs):
        out.notes.append("%s: %d point reference(s) skipped" % (label, len(refs) - len(curves)))
    return curves


def _plane_value(step: SketchStep, out: MappedStep) -> Any:
    plane = step.plane
    frame = (plane.geometry or {}).get("frame") if isinstance(plane.geometry, dict) else None
    # The frame is always preferred: SolidCraft's named planes do not share
    # Fusion's axis directions (a Fusion YZ sketch is rotated relative to
    # SolidCraft's YZ), so a name would lay the profile out the wrong way.
    if frame and frame.get("origin") and frame.get("x") and frame.get("y"):
        if plane.kind != "base":
            out.notes.append("sketch plane taken from its frame (%s)" % (plane.name or plane.kind))
        return {"origin": frame["origin"], "x_dir": frame["x"], "y_dir": frame["y"]}
    if plane.kind == "base" and plane.name.strip().upper() in ("XY", "XZ", "YZ"):
        out.notes.append("sketch plane taken from its name; the frame was unavailable")
        return plane.name.strip().upper()
    if plane.kind == "construction" and plane.name:
        out.notes.append("construction plane %r used by name" % plane.name)
        return plane.name
    out.notes.append("sketch plane %r unknown; defaulting to XY" % (plane.name or plane.kind))
    return "XY"


def _entity_command(entity: Entity, frame: Optional[Dict[str, Any]], out: MappedStep) -> Optional[Command]:
    data = dict(entity.data or {})
    kind = entity.kind

    def proj(point: Optional[Sequence[float]]) -> Optional[List[float]]:
        return _to_local(point, frame)

    if kind == "line":
        start, end = proj(data.get("start")), proj(data.get("end"))
        if start is None or end is None:
            out.notes.append("line without both endpoints was skipped")
            return None
        return Command("sketch.line", {"points": [start, end]})
    if kind == "circle":
        return Command("sketch.circle.center", {"center": proj(data.get("center")), "radius": data.get("radius")})
    if kind == "arc":
        return Command("sketch.arc.center_point",
                       {"center": proj(data.get("center")), "start": proj(data.get("start")),
                        "end": proj(data.get("end"))})
    if kind == "ellipse":
        center = proj(data.get("center"))
        major = data.get("major_radius")
        if center is not None and major is not None:
            out.notes.append("ellipse major axis assumed along the sketch x axis")
            return Command("sketch.ellipse",
                           {"center": center, "major": [center[0] + major, center[1]],
                            "minor_radius": data.get("minor_radius")})
        return None
    if kind == "point":
        return Command("sketch.point", {"point": proj(data.get("position"))})
    if kind in ("spline_fit", "spline_control", "conic"):
        out.notes.append("%s curves are not migrated in v1" % kind)
        return None
    out.notes.append("unknown sketch entity kind %r" % kind)
    return None


def _map_feature(index: int, step: FeatureStep, catalog: Catalog,
                 frames: Optional[Dict[str, Any]] = None,
                 profile_counts: Optional[Dict[str, int]] = None) -> MappedStep:
    out = MappedStep(index=index, symbol=step.symbol, source_name=step.name,
                     source_type=step.fusion_type, suppressed=step.suppressed)
    if step.props.get("construction"):
        out.status, out.reason = "skipped", "construction geometry is not migrated in v1"
        return out

    command_id = FEATURE_COMMANDS.get(step.fusion_type)
    if command_id is None:
        out.status = "skipped"
        out.reason = "no SolidCraft mapping for Fusion type %s" % step.fusion_type
        return out
    if not catalog.has(command_id):
        out.status = "skipped"
        out.reason = "SolidCraft has no command %s" % command_id
        return out

    params = _feature_params(step, command_id, out)

    grouped: Dict[str, List[Ref]] = {}
    for ref in step.refs:
        grouped.setdefault(ref.note or "input", []).append(ref)

    for attr, refs in grouped.items():
        candidates = REF_PARAM_CANDIDATES.get(attr)
        if not candidates:
            out.notes.append("input %r on %s has no parameter mapping" % (attr, step.fusion_type))
            continue
        key = catalog.choose_key(command_id, candidates, candidates[0])
        if key is None:
            out.notes.append("no parameter of %s accepts %r" % (command_id, attr))
            continue
        if attr == "axis":
            world = _world_axis(refs)
            if world is not None:
                params[key] = world
                continue
        if attr in ("profile", "profiles") and key != "sketch":
            profiles = _profile_value(refs, frames or {}, profile_counts or {}, out)
            if profiles is not None:
                params[key] = profiles
                continue
        tokens = [_register_ref(out, ref) for ref in refs]
        params[key] = tokens if (len(tokens) != 1 or _is_multi(key)) else tokens[0]

    missing = [key for key in REQUIRED_INPUTS.get(command_id, ())
               if key not in params]
    if missing:
        out.status = "skipped"
        out.reason = ("Fusion did not expose the input(s) %s that %s needs"
                      % (", ".join(missing), command_id))
        out.commands = []
        out.refs = {}
        return out

    out.commands.append(Command(command_id, _sanitize(command_id, params, catalog)))
    return out


def _profile_value(refs: List[Ref], frames: Dict[str, Any],
                   profile_counts: Dict[str, int], out: MappedStep) -> Any:
    """SolidCraft profiles.

    A sketch with one profile maps exactly to ``"all"`` (which extrudes the
    profile of the most recent sketch). Multi-profile sketches use a point
    inside each profile, but nested profiles share a centre, so they fall back
    to ``"all"`` as well.
    """
    targets = {ref.target for ref in refs if ref.target}
    if len(targets) == 1 and profile_counts.get(next(iter(targets)), 0) == 1:
        return "all"

    points: List[List[float]] = []
    for ref in refs:
        centroid = (ref.geometry or {}).get("centroid")
        local = _to_local(centroid, frames.get(ref.target)) if centroid else None
        if local is None:
            out.notes.append("profile point unavailable; extruding all profiles of the last sketch")
            return "all"
        if local not in points:
            points.append(local)
    if not points:
        return "all"
    if len(points) != len(refs):
        out.notes.append("nested profiles share a centre; extruding all profiles")
        return "all"
    return [{"point": point} for point in points]


def _feature_params(step: FeatureStep, command_id: str, out: MappedStep) -> Dict[str, Any]:
    props = step.props or {}
    params: Dict[str, Any] = {}

    operation = props.get("operation")
    if operation:
        params["operation"] = OPERATION_MAP.get(str(operation), str(operation))

    distance = props.get("distance")
    if isinstance(distance, (int, float)):
        if distance < 0:
            params["distance"] = abs(distance)
            params["direction"] = "negative"
        else:
            params["distance"] = distance
    if props.get("isSymmetric"):
        params["direction"] = "symmetric"
        out.notes.append("symmetric extrude mapped to a symmetric direction")
    if isinstance(props.get("distanceTwo"), (int, float)):
        params["distance2"] = abs(props["distanceTwo"])

    if step.fusion_type == "RectangularPatternFeature":
        for source, target in (("distanceOne", "spacing1"), ("distanceTwo", "spacing2"),
                               ("quantityOne", "count1"), ("quantityTwo", "count2")):
            if isinstance(props.get(source), (int, float)):
                params[target] = abs(props[source]) if target.startswith("spacing") else props[source]

    for prop, value in props.items():
        if prop in INTERNAL_PROPS or prop == "operation" or value is None:
            continue
        if prop in ("distance", "distanceTwo"):
            continue
        if step.fusion_type == "RectangularPatternFeature" and prop in (
                "distanceOne", "distanceTwo", "quantityOne", "quantityTwo"):
            continue
        target = PROP_PARAM.get(prop, _snake(prop))
        params.setdefault(target, value)
    return params


def _is_multi(key: str) -> bool:
    return key in ("faces", "edges", "tools", "bodies", "features", "targets", "profiles",
                   "path", "points")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _register_ref(out: MappedStep, ref: Ref) -> str:
    token = "@{r%d}" % len(out.refs)
    out.refs[token] = ref
    return token


def _sanitize(command_id: str, params: Dict[str, Any], catalog: Catalog) -> Dict[str, Any]:
    names = catalog.property_names(command_id)
    if not names:
        return {k: v for k, v in params.items() if v is not None}
    required = catalog.required_names(command_id)
    out: Dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if key in names or key in required:
            out[key] = value
    return out


_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")


def _snake(name: str) -> str:
    return _SNAKE.sub("_", name).lower()


def _world_axis(refs: List[Ref]) -> Optional[str]:
    """A Fusion world construction axis (X/Y/Z) as SolidCraft's ``X``/``Y``/``Z``.

    SolidCraft reads a lowercase axis as the sketch's own axis and uppercase as
    the world axis, so the world axis is passed in upper case.
    """
    if len(refs) != 1:
        return None
    geometry = refs[0].geometry or {}
    name = str(geometry.get("name") or "").strip().upper()
    if "Axis" in str(geometry.get("object_type", "")) and name in ("X", "Y", "Z"):
        return name
    return None


def _close(a: Sequence[float], b: Sequence[float], tol: float = 1e-4) -> bool:
    try:
        return all(abs(float(a[i]) - float(b[i])) <= tol for i in range(3))
    except (IndexError, TypeError, ValueError):
        return False


def _to_local(point: Optional[Sequence[float]], frame: Optional[Dict[str, Any]]) -> Optional[List[float]]:
    if point is None:
        return None
    if not frame:
        return [round(float(point[0]), 6), round(float(point[1]), 6)] if len(point) >= 2 else None
    origin = frame.get("origin") or [0.0, 0.0, 0.0]
    xaxis = frame.get("x") or [1.0, 0.0, 0.0]
    yaxis = frame.get("y") or [0.0, 1.0, 0.0]
    v = [float(point[i]) - float(origin[i]) for i in range(3)]
    x = sum(v[i] * float(xaxis[i]) for i in range(3))
    y = sum(v[i] * float(yaxis[i]) for i in range(3))
    return [round(x, 6), round(y, 6)]
