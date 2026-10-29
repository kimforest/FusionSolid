"""The extraction program that runs *inside Fusion*.

Fusion's MCP server is not documented as exposing a raw script tool, so the
migration sends this source through whichever execute/script tool discovery
finds. The program uses only the public Fusion Python API (``adsk.core`` /
``adsk.fusion``) and is written to stay compatible with both IronPython 2.7 and
CPython 3, since Fusion's script host varies by release.

Every attribute read is guarded, so an unrecognised feature degrades into a
timeline entry with whatever could be read instead of aborting the whole run.

Output contract (JSON, printed as the tool result text):

    {
      "design":    {"name", "units"},
      "parameters":[{"name","expression","value","unit","comment"}],
      "timeline":  [{"index","name","type","kind","suppressed","error",
                     "props":{...}, "sketch":{...}, "tokens":{...}}],
      "bodies":    [{"name","volume","bbox"}],
      "unit_note": "..."
    }

Lengths are converted to millimetres and angles to degrees here, so the Python
side never has to guess units.
"""

from __future__ import annotations

# Kept as a plain string: it is shipped to Fusion verbatim.
EXTRACTION_SCRIPT = r'''
import json

def _app():
    import adsk.core
    return adsk.core.Application.get()

def _num(x):
    """Best-effort numeric value of a ValueInput / Parameter / float."""
    if x is None:
        return None
    for attr in ("value",):
        try:
            v = getattr(x, attr)
            if callable(v):
                v = v()
            if v is not None:
                return float(v)
        except Exception:
            pass
    try:
        return float(x)
    except Exception:
        return None

def _len_mm(x):
    v = _num(x)
    return None if v is None else v * 10.0          # Fusion API lengths are cm

def _deg(x):
    import math
    v = _num(x)
    return None if v is None else math.degrees(v)  # Fusion API angles are radians

def _enum_name(x):
    if x is None:
        return None
    try:
        return str(x)  # adsk enums stringify to the constant name
    except Exception:
        return None

def _pt(p):
    try:
        w = p.worldGeometry
        return [w.x * 10.0, w.y * 10.0, w.z * 10.0]
    except Exception:
        try:
            return [p.x * 10.0, p.y * 10.0, p.z * 10.0]
        except Exception:
            return None

def _token(obj):
    try:
        return obj.entityToken
    except Exception:
        return None

ANGLE_HINTS = ("angle", "taper", "twist", "draft", "sweepangle", "pitch")
LENGTH_HINTS = ("distance", "radius", "thickness", "diameter", "height", "width",
                "length", "offset", "pitch", "size", "depth", "section")

def _get(obj, name):
    """Walk a dotted attribute path (``extentDefinition.distance``)."""
    node = obj
    for part in name.split("."):
        try:
            node = getattr(node, part)
        except Exception:
            return _MISSING
    return node


def _read(obj, name):
    """Read one attribute, converting ValueInputs by a name heuristic."""
    value = _get(obj, name)
    if isinstance(value, _Missing) or value is None:
        return _MISSING
    low = name.lower()
    if any(h in low for h in ANGLE_HINTS):
        return _deg(value)
    if any(h in low for h in LENGTH_HINTS):
        return _len_mm(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value
    return _enum_name(value)


def _put(out, key, value):
    if not isinstance(value, _Missing) and value is not None:
        out[key] = value
    return out

class _Missing(object):
    pass

_MISSING = _Missing()

def _props(obj, names):
    out = {}
    for n in names:
        v = _read(obj, n)
        if not isinstance(v, _Missing) and v is not None:
            out[n] = v
    return out

FEATURE_ATTRS = {
    "ExtrudeFeature": ["operation", "extentType", "taperAngle", "isThinExtrude",
                       "extentDefinition.distance", "extentDefinition.isSymmetric",
                       "extentOne.distance", "extentTwo.distance"],
    "RevolveFeature": ["operation", "isSolid", "extentDefinition.angle", "angle"],
    "FilletFeature": ["isRollingBallCorner", "isTangentChain"],
    "ChamferFeature": ["isTangentChain"],
    "HoleFeature": ["holeType", "holeDiameter", "holeDepth", "operation"],
    "ShellFeature": ["thickness", "isTangentChain"],
    "SweepFeature": ["distance", "taperAngle", "twistAngle", "operation"],
    "LoftFeature": ["operation", "isClosed", "isSolid", "isTangentEdges"],
    "PipeFeature": ["sectionSize", "sectionType", "operation"],
    "CoilFeature": ["sectionSize", "sectionType", "diameter", "pitch",
                    "revolutions", "isClockwise", "operation"],
    "CombineFeature": ["operation"],
    "SplitBodyFeature": ["isSplittingToolExtended"],
    "ScaleFeature": ["scaleFactor"],
    "DraftFeature": ["angle", "isTangentChain"],
    "ThreadFeature": ["isFullLength", "threadOffset", "threadLength", "isModeled"],
    "RibFeature": ["thickness", "isDirectionPositive", "isTangentChain"],
    "WebFeature": ["thickness", "isTangentChain"],
    "EmbossFeature": ["thickness", "depth", "isTangentChain"],
    "RemoveFeature": [],
    "OffsetFacesFeature": ["distance"],
    "ReplaceFaceFeature": ["isTangentChain"],
    "BoundaryFillFeature": ["operation", "isTangentChain"],
    "PatchFeature": ["isTangentChain", "operation"],
    "MoveFeature": [],
    "CircularPatternFeature": ["quantity", "totalAngle", "isSymmetric"],
    "RectangularPatternFeature": ["quantityOne", "quantityTwo", "distanceOne", "distanceTwo"],
    "PathPatternFeature": ["quantity", "distance", "isOrientationAligned"],
    "MirrorFeature": ["patternComputeOption"],
    "BoxFeature": ["length", "width", "height"],
    "CylinderFeature": ["diameter", "height"],
    "SphereFeature": ["diameter"],
    "TorusFeature": ["majorRadius", "minorRadius"],
    "ConeFeature": ["bottomDiameter", "topDiameter", "height"],
}


def _extrude_props(e):
    """ExtrudeFeature keeps its distance in the extent definition, not on the feature."""
    out = {}
    for name in ("operation", "extentType", "taperAngle", "isThinExtrude"):
        _put(out, name, _read(e, name))
    distance = _read(e, "extentDefinition.distance")
    if isinstance(distance, _Missing) or distance is None:
        distance = _read(e, "extentOne.distance")
    _put(out, "distance", distance)
    _put(out, "isSymmetric", _read(e, "extentDefinition.isSymmetric"))
    _put(out, "distanceTwo", _read(e, "extentTwo.distance"))
    return out


def _revolve_props(e):
    out = {}
    for name in ("operation", "isSolid"):
        _put(out, name, _read(e, name))
    for name in ("extentDefinition.angle", "angle"):
        value = _read(e, name)
        if not isinstance(value, _Missing) and value is not None:
            out["angle"] = value
            break
    return out


def _edge_set_props(e, value_names):
    """Fillet/Chamfer: take the first edge set's radius or distance."""
    out = {}
    try:
        sets = list(e.edgeSets)
    except Exception:
        return out
    out["edge_set_count"] = len(sets)
    if sets:
        first = sets[0]
        for name in value_names:
            value = _read(first, name)
            if not isinstance(value, _Missing) and value is not None:
                out[name] = value
                break
        try:
            out["edge_count"] = first.edges.count
        except Exception:
            pass
    _put(out, "operation", _read(e, "operation"))
    return out


def _sketch_json(sketch):
    curves = []
    tokens = {}
    def add(kind, curve, extra):
        t = _token(curve)
        entry = {"kind": kind, "data": extra, "token": t, "construction": bool(getattr(curve, "isConstruction", False))}
        curves.append(entry)
        if t:
            tokens[t] = len(curves) - 1
    try:
        sc = sketch.sketchCurves
    except Exception:
        sc = None
    if sc is not None:
        for ln in getattr(sc, "sketchLines", []):
            add("line", ln, {"start": _pt(ln.startSketchPoint), "end": _pt(ln.endSketchPoint)})
        for c in getattr(sc, "sketchCircles", []):
            add("circle", c, {"center": _pt(c.centerSketchPoint), "radius": _len_mm(c.radius)})
        for a in getattr(sc, "sketchArcs", []):
            add("arc", a, {"center": _pt(a.centerSketchPoint), "radius": _len_mm(a.radius),
                           "start": _pt(a.startSketchPoint), "end": _pt(a.endSketchPoint)})
        for e in getattr(sc, "sketchEllipses", []):
            add("ellipse", e, {"center": _pt(e.centerSketchPoint),
                               "major_radius": _len_mm(e.majorAxisRadius),
                               "minor_radius": _len_mm(e.minorAxisRadius)})
        for s in getattr(sc, "sketchFittedSplines", []):
            add("spline_fit", s, {"point_count": _count(getattr(s, "fitPoints", None))})
        for s in getattr(sc, "sketchControlPointSplines", []):
            add("spline_control", s, {"point_count": _count(getattr(s, "controlPoints", None))})
        for c in getattr(sc, "sketchConics", []):
            add("conic", c, {})

    points = []
    try:
        for p in sketch.sketchPoints:
            t = _token(p)
            geom = _pt(p)
            if geom is None or (abs(geom[0]) < 1e-9 and abs(geom[1]) < 1e-9 and abs(geom[2]) < 1e-9):
                continue  # origin points are implied by the plane
            points.append({"data": {"position": geom}, "token": t})
            if t:
                tokens[t] = len(points) - 1
    except Exception:
        pass

    constraints = []
    try:
        for c in sketch.geometricConstraints:
            constraints.append({"type": _enum_name(getattr(c, "objectType", None)) or "Unknown",
                                "tokens": _entity_tokens(c)})
    except Exception:
        pass

    dimensions = []
    try:
        for d in sketch.sketchDimensions:
            t = _enum_name(getattr(d, "objectType", None)) or "Dimension"
            value = _num(d.value)
            entry = {"type": t, "tokens": _entity_tokens(d), "raw_value": value}
            low = t.lower()
            if "angular" in low:
                entry["angle_deg"] = _deg(d.value)
            else:
                entry["length_mm"] = _len_mm(d.value)
            try:
                entry["parameter"] = str(d.parameter.name)
            except Exception:
                pass
            dimensions.append(entry)
    except Exception:
        pass

    plane = {}
    try:
        rp = sketch.referencePlane
        plane = {"kind": _enum_name(getattr(rp, "objectType", None)),
                 "name": _safe_name(rp)}
    except Exception:
        pass
    try:
        o = sketch.origin
        xa = sketch.xDirection
        ya = sketch.yDirection
        frame = {
            "origin": [o.x * 10.0, o.y * 10.0, o.z * 10.0],
            "x": [xa.x, xa.y, xa.z],
            "y": [ya.x, ya.y, ya.z],
        }
        try:
            n = sketch.referencePlane.geometry.normal
            frame["z"] = [n.x, n.y, n.z]
        except Exception:
            pass
        plane["frame"] = frame
    except Exception:
        pass

    profile_count = 0
    try:
        profile_count = sketch.profiles.count
    except Exception:
        pass

    return {"curves": curves, "points": points, "constraints": constraints,
            "dimensions": dimensions, "plane": plane, "profile_count": profile_count,
            "token_index": tokens}


def _entity_tokens(obj):
    out = []
    for attr in ("entity", "entityOne", "entityTwo", "entities", "points",
                 "entityOnePoint", "entityTwoPoint"):
        try:
            value = getattr(obj, attr)
        except Exception:
            continue
        if value is None:
            continue
        try:
            for sub in value:
                t = _token(sub)
                if t:
                    out.append(t)
        except Exception:
            t = _token(value)
            if t:
                out.append(t)
    seen = []
    for t in out:
        if t not in seen:
            seen.append(t)
    return seen

def _count(coll):
    try:
        return coll.count
    except Exception:
        try:
            return len(coll)
        except Exception:
            return None

def _safe_name(o):
    try:
        return o.name
    except Exception:
        return None


GENERIC_VALUE_ATTRS = ("distance", "offsetDistance", "thickness", "depth", "radius",
                       "angle", "scaleFactor", "quantity", "sectionSize", "diameter",
                       "pitch", "height", "width", "length")


def _feature_props(entity, ftype):
    """Props for one feature, using dotted attribute paths where Fusion keeps a
    value in a sub-object (e.g. an extrude's distance in its extent definition)."""
    short = ftype.rsplit("::", 1)[-1]
    special = {
        "ExtrudeFeature": _extrude_props,
        "RevolveFeature": _revolve_props,
        "FilletFeature": lambda e: _edge_set_props(e, ("radius",)),
        "ChamferFeature": lambda e: _edge_set_props(e, ("distance", "distanceTwo")),
    }
    builder = special.get(short)
    if builder is not None:
        props = builder(entity)
    else:
        props = _props(entity, FEATURE_ATTRS.get(short, []))
    if not props:
        # Unknown feature type: salvage a single obvious value if there is one.
        for name in GENERIC_VALUE_ATTRS:
            value = _read(entity, name)
            if not isinstance(value, _Missing) and value is not None:
                props[name] = value
                break
    operation = _operation_name(entity)
    if not isinstance(operation, _Missing) and operation is not None:
        props["operation"] = operation
    return props


def _operation_name(entity):
    """The FeatureOperations member name (Fusion hands back a bare int)."""
    try:
        import adsk.fusion
        value = entity.operation
    except Exception:
        return _MISSING
    try:
        for name in dir(adsk.fusion.FeatureOperations):
            if name.startswith("_"):
                continue
            try:
                if int(getattr(adsk.fusion.FeatureOperations, name)) == int(value):
                    return name
            except Exception:
                pass
    except Exception:
        pass
    return str(value)


def _extract():
    import adsk.core, adsk.fusion
    app = _app()
    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        return {"error": "no active Fusion design is open"}

    out = {"unit_note": "lengths are mm, angles are deg",
           "design": {}, "parameters": [], "timeline": [], "bodies": []}
    try:
        out["design"]["name"] = design.parentDocument.name
    except Exception:
        out["design"]["name"] = ""
    try:
        out["design"]["units"] = _enum_name(design.unitsManager.defaultLengthUnits)
    except Exception:
        out["design"]["units"] = "mm"

    # User parameters
    try:
        for p in design.userParameters:
            out["parameters"].append({
                "name": p.name,
                "expression": _safe_expr(p),
                "value": _num(p.value),
                "unit": getattr(p, "unit", ""),
                "comment": getattr(p, "comment", ""),
            })
    except Exception:
        pass

    out["timeline"] = _timeline_json(design)
    try:
        out["timeline_unreadable"] = _UNREADABLE[0]
    except Exception:
        pass
    _UNREADABLE[0] = []

    # Bodies (final geometry, for validation only). Read with the timeline at
    # the end, then put the marker back where the user left it.
    try:
        original_marker = design.timeline.markerPosition
    except Exception:
        original_marker = None
    try:
        try:
            design.timeline.markerPosition = design.timeline.count
        except Exception:
            pass
        for b in design.rootComponent.bRepBodies:
            entry = {"name": b.name}
            try:
                entry["volume"] = b.volume * 1000.0  # cm^3 -> mm^3
            except Exception:
                entry["volume"] = None
            try:
                bb = b.boundingBox
                entry["bbox"] = {
                    "min": [bb.minPoint.x * 10.0, bb.minPoint.y * 10.0, bb.minPoint.z * 10.0],
                    "max": [bb.maxPoint.x * 10.0, bb.maxPoint.y * 10.0, bb.maxPoint.z * 10.0],
                }
            except Exception:
                entry["bbox"] = None
            out["bodies"].append(entry)
    except Exception:
        pass
    finally:
        try:
            if original_marker is not None:
                design.timeline.markerPosition = original_marker
        except Exception:
            pass

    return out


def _safe_expr(p):
    try:
        return p.expression
    except Exception:
        return ""

def _error_text(item):
    try:
        msg = item.errorOrWarningMessage
        return msg or ""
    except Exception:
        return ""

def _ref_geometry(design, entity):
    """Geometric signature of a referenced face/edge/profile/vertex, so the
    Python side can match it against a SolidCraft body topology later."""
    g = {"object_type": _enum_name(getattr(entity, "objectType", None))}
    try:
        g["name"] = entity.name
    except Exception:
        pass
    try:
        if "BRepBody" in str(g.get("object_type")):
            bb = entity.boundingBox
            g["bbox"] = {"min": [bb.minPoint.x * 10.0, bb.minPoint.y * 10.0, bb.minPoint.z * 10.0],
                         "max": [bb.maxPoint.x * 10.0, bb.maxPoint.y * 10.0, bb.maxPoint.z * 10.0]}
    except Exception:
        pass
    try:
        c = getattr(entity, "centroid", None)
        if c is not None:
            g["centroid"] = [c.x * 10.0, c.y * 10.0, c.z * 10.0]
    except Exception:
        pass
    try:
        area = getattr(entity, "area", None)
        if isinstance(area, (int, float)):
            g["area_mm2"] = area * 100.0
    except Exception:
        pass
    try:
        surf = entity.geometry
        # Only a face has a surface; an edge's geometry is a curve, and tagging
        # it as a surface would make the Python side treat the edge as a face.
        if "Face" in str(g.get("object_type")):
            g["surface_type"] = _enum_name(getattr(surf, "objectType", None))
        for attr, scale in (("normal", 1.0), ("axis", 1.0), ("origin", 10.0),
                            ("radius", 10.0), ("center", 10.0)):
            try:
                v = getattr(surf, attr)
            except Exception:
                continue
            if hasattr(v, "x"):
                g[attr] = [v.x * scale, v.y * scale, v.z * scale]
            elif isinstance(v, (int, float)):
                g[attr] = v * scale
    except Exception:
        pass
    try:
        length = getattr(entity, "length", None)
        if isinstance(length, (int, float)):
            g["length_mm"] = length * 10.0
        start = _pt(entity.startVertex.geometry)
        end = _pt(entity.endVertex.geometry)
        g["start"] = start
        g["end"] = end
        if start and end:
            g["midpoint"] = [(start[i] + end[i]) / 2.0 for i in range(3)]
    except Exception:
        pass
    try:
        p = entity.geometry
        if hasattr(p, "x") and hasattr(p, "y"):
            g.setdefault("point", _pt(p))
    except Exception:
        pass
    try:
        g["sketch_token"] = _token(entity.parentSketch)
    except Exception:
        pass
    try:
        ap = entity.areaProperties
        if not callable(ap):
            cen = ap.centroid
            g.setdefault("centroid", [cen.x * 10.0, cen.y * 10.0, cen.z * 10.0])
            g["area_mm2"] = ap.area * 100.0
    except Exception:
        pass
    try:
        if "centroid" not in g:
            bb = entity.boundingBox
            mn = bb.minPoint
            mx = bb.maxPoint
            g["centroid"] = [(mn.x + mx.x) * 5.0, (mn.y + mx.y) * 5.0, (mn.z + mx.z) * 5.0]
    except Exception:
        pass
    return g


# Only the *inputs* of each feature type. Reading the feature's own output
# faces (ExtrudeFeature.faces and friends) would wrongly drive later features.
FEATURE_REF_ATTRS = {
    "ExtrudeFeature": ["profile", "profiles"],
    "RevolveFeature": ["profile", "profiles", "axis"],
    "SweepFeature": ["profile", "profiles", "path"],
    "LoftFeature": ["loftSections", "rails"],
    "PipeFeature": ["path"],
    "CoilFeature": ["profile"],
    "FilletFeature": ["edgeSets", "faces"],
    "ChamferFeature": ["edgeSets"],
    "ShellFeature": ["inputEntities", "faces"],
    "CombineFeature": ["targetBody", "toolBodies"],
    "OffsetFacesFeature": ["inputFaces", "faces"],
    "DraftFeature": ["faces", "neutralPlane"],
    "MoveFeature": ["inputEntities"],
    "ScaleFeature": ["inputEntities"],
    "SplitBodyFeature": ["splittingTool", "inputEntities"],
    "MirrorFeature": ["mirrorEntities", "mirrorPlane"],
    "CircularPatternFeature": ["entities", "axis"],
    "RectangularPatternFeature": ["entities", "directionOneEntity"],
    "PathPatternFeature": ["entities", "path"],
    "RibFeature": ["profile"],
    "WebFeature": ["profile"],
    "HoleFeature": ["positionPoint"],
    "ThreadFeature": ["face"],
    "RemoveFeature": ["inputEntities", "bodies"],
}


def _iter(value):
    items = []
    try:
        for sub in value:
            items.append(sub)
    except Exception:
        items.append(value)
    return items


def _feature_refs(feature, design):
    """The input handles of a feature, each with its geometry.

    Edge sets (fillet, chamfer) are flattened to their individual edges, which
    is what SolidCraft's ``edges`` parameter takes.
    """
    short = str(getattr(feature, "objectType", "")).rsplit("::", 1)[-1]
    out = []
    for attr in FEATURE_REF_ATTRS.get(short, []):
        value = _get(feature, attr)
        if isinstance(value, _Missing) or value is None:
            continue
        for sub in _iter(value):
            if sub is None:
                continue
            if attr == "edgeSets":
                edges = _get(sub, "edges")
                if isinstance(edges, _Missing) or edges is None:
                    continue
                for edge in _iter(edges):
                    try:
                        out.append({"attr": "edges", "token": _token(edge),
                                    "geometry": _ref_geometry(design, edge)})
                    except Exception:
                        pass
                continue
            try:
                out.append({"attr": attr, "token": _token(sub),
                            "geometry": _ref_geometry(design, sub)})
            except Exception:
                continue
    return out


# Features whose inputs could not be read (reported to the migration report).
_UNREADABLE = [[]]


def _timeline_json(design):
    """Every timeline item, with the marker parked on each feature in turn.

    Fusion only exposes a feature's input geometry while the timeline marker
    sits on that feature; leaving it at the end makes ``edgeSets`` and
    ``targetBody`` raise "Didn't roll editing feature back". Moving the marker
    needs a writable context, so the caller must not run this read-only.
    """
    try:
        timeline = design.timeline
    except Exception:
        return []
    if timeline is None:
        return []

    def set_marker(position):
        try:
            timeline.markerPosition = position
            return True
        except Exception:
            return False

    moved = False
    original = None
    try:
        original = timeline.markerPosition
    except Exception:
        pass

    items = []
    try:
        for i in range(_count(timeline) or 0):
            moved = set_marker(i) or moved
            try:
                item = timeline.item(i)
            except Exception:
                continue
            entity = None
            try:
                entity = item.entity
            except Exception:
                pass
            ftype = _enum_name(getattr(entity, "objectType", None)) or "Unknown"
            entry = {
                "index": i,
                "name": item.name,
                "type": ftype,
                "kind": "feature",
                "token": _token(entity),
                "suppressed": bool(getattr(item, "isSuppressed", False)),
                "error": _error_text(item),
                "props": {},
                "tokens": {},
            }
            if ftype.endswith("Sketch"):
                entry["kind"] = "sketch"
                try:
                    entry["sketch"] = _sketch_json(entity)
                except Exception as exc:
                    entry["sketch_error"] = str(exc)
            elif ftype.endswith(("ConstructionPlane", "ConstructionAxis", "ConstructionPoint")):
                entry["kind"] = "construction"
                entry["props"] = _props(entity, ["name"])
            else:
                entry["kind"] = "feature"
                entry["props"] = _feature_props(entity, ftype)
                short = ftype.rsplit("::", 1)[-1]
                refs = _feature_refs(entity, design)
                if not refs and FEATURE_REF_ATTRS.get(short):
                    # Different features expose their inputs at different marker
                    # positions: most want the marker on the feature, some (an
                    # OffsetFaces, say) want it just after.
                    for fallback in (i + 1, original):
                        if fallback is None or fallback == i:
                            continue
                        set_marker(fallback)
                        refs = _feature_refs(entity, design)
                        if refs:
                            break
                    set_marker(i)
                    if not refs:
                        unreadable = {"name": _safe_name(entity), "type": short,
                                      "reason": "inputs not exposed by the Fusion API"}
                        _UNREADABLE[0].append(unreadable)
                entry["refs"] = refs
            items.append(entry)
    finally:
        try:
            if original is not None:
                timeline.markerPosition = original
        except Exception:
            pass

    if not moved:
        _UNREADABLE[0].append({
            "note": "the timeline marker could not be moved (read-only context), "
                    "so some feature inputs are unavailable",
        })
    return items


_printed = False


def _emit():
    global _printed
    _printed = True
    try:
        result = _extract()
    except Exception as exc:
        result = {"error": "%s: %s" % (type(exc).__name__, exc)}
    print(json.dumps(result))
    return result


def run(*args, **kwargs):
    # Fusion's script host executes the module and then calls run(context); the
    # module-level call below has already produced the output by then.
    global _printed
    if _printed:
        return None
    return _emit()


_emit()
'''
