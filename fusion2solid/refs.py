"""Geometric reference resolution.

A Fusion feature refers to faces, edges and vertices that belong to a body.
SolidCraft refers to the same geometry through *its own* identifiers (a face's
``name`` or a point that picks it, an edge's midpoint). Fusion's topology does
not survive the transfer, so references are resolved by matching the geometric
signature captured in Fusion (see ``fusion_script._ref_geometry``) against the
topology SolidCraft reports for the body it just built (the ``body_topology``
tool, or ``model.faces`` / ``model.edges``).

Everything here is a pure function so the matching logic can be unit tested
without either application running.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Cost units: distances in mm, angles in degrees (1 deg ~ 1 mm), so a single
# scalar threshold in millimetres is meaningful and easy to reason about.
DEFAULT_TOL_MM = 1e-2
DEFAULT_TOL_DEG = 0.5
FACE_TOL_DEG = 5.0


@dataclass
class Match:
    index: int
    cost: float
    confidence: str  # high | medium | low
    selection: Any  # what to hand to SolidCraft (name, point, midpoint...)


def _dist(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> Optional[float]:
    if not a or not b:
        return None
    if len(a) < 3 or len(b) < 3:
        return None
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


def _angle_deg(u: Sequence[float], v: Sequence[float]) -> Optional[float]:
    if not u or not v:
        return None
    nu = math.sqrt(sum(float(c) ** 2 for c in u[:3]))
    nv = math.sqrt(sum(float(c) ** 2 for c in v[:3]))
    if nu == 0 or nv == 0:
        return None
    dot = sum(float(u[i]) * float(v[i]) for i in range(3)) / (nu * nv)
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot))


def _axis_angle_deg(u, v) -> Optional[float]:
    """Angle between two *lines* (undirected axes): 0..90 degrees."""
    a = _angle_deg(u, v)
    if a is None:
        return None
    return min(a, abs(180.0 - a))


def _surface_family(geometry: Dict[str, Any]) -> str:
    obj = str(geometry.get("surface_type") or geometry.get("object_type") or "").lower()
    if "cylinder" in obj:
        return "cylinder"
    if "cone" in obj:
        return "cone"
    if "sphere" in obj:
        return "sphere"
    if "torus" in obj:
        return "torus"
    if "plane" in obj:
        return "plane"
    if "sphere" in obj:
        return "sphere"
    if "nurbs" in obj or "surface" in obj:
        return "freeform"
    return "unknown"


def _face_family(face: Dict[str, Any]) -> str:
    if face.get("cylinder"):
        return "cylinder"
    if face.get("normal"):
        return "plane"
    return "unknown"


def score_face(geometry: Dict[str, Any], face: Dict[str, Any]) -> Optional[float]:
    """Cost of matching a Fusion face reference to a SolidCraft face (lower is
    better), or ``None`` when the surface families are incompatible."""
    want = _surface_family(geometry)
    have = _face_family(face)
    if want not in ("unknown", "freeform") and have != "unknown" and want != have:
        return None

    cost = 0.0
    if want == "plane" or have == "plane":
        ang = _angle_deg(geometry.get("normal") or [], face.get("normal") or [])
        if ang is not None:
            cost += min(ang, abs(180.0 - ang))
        point_distance = _dist(geometry.get("centroid"), face.get("centroid"))
        if point_distance is None:
            point_distance = _dist(geometry.get("origin"), face.get("point") or face.get("centroid"))
        if point_distance is not None:
            cost += point_distance
        if ang is None and point_distance is None:
            return None
        return cost

    if want == "cylinder":
        cyl = face.get("cylinder") or {}
        axis_ang = _axis_angle_deg(geometry.get("axis") or [], cyl.get("axis") or [])
        if axis_ang is not None:
            cost += axis_ang
        radius_delta = _abs_delta(geometry.get("radius"), cyl.get("radius"))
        if radius_delta is not None:
            cost += radius_delta
        center_delta = _dist(geometry.get("centroid"), face.get("centroid"))
        if center_delta is not None:
            cost += center_delta
        if axis_ang is None and radius_delta is None and center_delta is None:
            return None
        return cost

    # Freeform / unknown: fall back to centroid proximity only.
    return _dist(geometry.get("centroid"), face.get("centroid"))


def _abs_delta(a, b) -> Optional[float]:
    if a is None or b is None:
        return None
    return abs(float(a) - float(b))


def _confidence(cost: float, tol_mm: float, tol_deg: float) -> str:
    if cost <= tol_mm + tol_deg:
        return "high"
    if cost <= 10 * (tol_mm + tol_deg):
        return "medium"
    return "low"


def match_face(
    geometry: Dict[str, Any],
    faces: Sequence[Dict[str, Any]],
    tol_mm: float = DEFAULT_TOL_MM,
    tol_deg: float = FACE_TOL_DEG,
) -> Optional[Match]:
    best: Optional[Match] = None
    for face in faces:
        cost = score_face(geometry, face)
        if cost is None:
            continue
        if best is None or cost < best.cost:
            selection = face.get("name") or face.get("point") or face.get("centroid")
            best = Match(
                index=int(face.get("index", -1)),
                cost=cost,
                confidence=_confidence(cost, tol_mm, tol_deg),
                selection=selection,
            )
    return best


def score_edge(geometry: Dict[str, Any], edge: Dict[str, Any]) -> Optional[float]:
    cost = 0.0
    mid = _dist(geometry.get("midpoint"), edge.get("midpoint"))
    if mid is None:
        # Some topologies omit the midpoint; fall back to the ends.
        mid = _dist(geometry.get("start"), edge.get("ends", [None])[0] if edge.get("ends") else None)
    if mid is not None:
        cost += mid
    length_delta = _abs_delta(geometry.get("length_mm"), edge.get("length"))
    if length_delta is not None:
        cost += length_delta
    if mid is None and length_delta is None:
        return None
    return cost


def match_edge(
    geometry: Dict[str, Any],
    edges: Sequence[Dict[str, Any]],
    tol_mm: float = DEFAULT_TOL_MM,
    tol_deg: float = DEFAULT_TOL_DEG,
) -> Optional[Match]:
    best: Optional[Match] = None
    for edge in edges:
        cost = score_edge(geometry, edge)
        if cost is None:
            continue
        if best is None or cost < best.cost:
            best = Match(
                index=int(edge.get("index", -1)),
                cost=cost,
                confidence=_confidence(cost, tol_mm, tol_deg),
                selection=edge.get("midpoint") or edge.get("ends"),
            )
    return best


def match_vertex(
    geometry: Dict[str, Any],
    vertices: Sequence[Dict[str, Any]],
    tol_mm: float = DEFAULT_TOL_MM,
    tol_deg: float = DEFAULT_TOL_DEG,
) -> Optional[Match]:
    best: Optional[Match] = None
    want = geometry.get("centroid") or geometry.get("start") or geometry.get("origin")
    for vertex in vertices:
        have = vertex.get("position") or vertex.get("point") or vertex.get("centroid")
        cost = _dist(want, have)
        if cost is None:
            continue
        if best is None or cost < best.cost:
            best = Match(
                index=int(vertex.get("index", -1)),
                cost=cost,
                confidence=_confidence(cost, tol_mm, tol_deg),
                selection=have,
            )
    return best
