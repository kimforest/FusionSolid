"""Read the active Fusion design through the Fusion Desktop MCP server.

The MCP tool surface is discovered (see :mod:`discovery`); when a script/execute
tool exists, the canned Fusion program in :mod:`fusion_script` is sent through
it and its JSON is converted to the shared IR.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .discovery import Discovery, build_script_arguments, classify
from .fusion_script import EXTRACTION_SCRIPT
from .ir import (
    BodySummary,
    Constraint,
    Design,
    Dimension,
    Entity,
    FeatureStep,
    Param,
    PlaneRef,
    Ref,
    SketchStep,
)
from .mcp_client import HttpMCPClient, MCPError, ToolResult

# Attributes on a Fusion feature that name its *inputs* (rather than options).
REF_ATTRS = {
    "profile", "profiles", "path", "mirrorPlane", "splittingTool", "targetBody",
    "toolBodies", "axis", "point", "faces", "edges", "inputEntities", "startFaces",
}

BASE_PLANES = {"XY": "XY", "XZ": "XZ", "YZ": "YZ"}


class FusionSource:
    def __init__(self, url: str, timeout: float = 120.0, allow_design_edit: bool = True):
        self.url = url
        self.timeout = timeout
        # The extractor parks the timeline marker on each feature to read its
        # inputs (Fusion needs this), which requires a writable context. The
        # design itself is never modified; the marker is restored.
        self.allow_design_edit = allow_design_edit
        self.client = HttpMCPClient(url, timeout=timeout)
        self.discovery: Optional[Discovery] = None

    def connect(self) -> Discovery:
        self.client.initialize()
        self.discovery = classify(self.client.list_tools(refresh=True))
        return self.discovery

    def close(self) -> None:
        self.client.close()

    # -- extraction --------------------------------------------------------
    def extract_raw(self) -> Dict[str, Any]:
        if self.discovery is None:
            self.connect()
        assert self.discovery is not None
        if self.discovery.strategy == "script":
            return self._extract_via_script(self.discovery)
        if self.discovery.strategy == "structured":
            return self._extract_via_structured(self.discovery)
        raise MCPError(
            "the Fusion MCP server exposes no usable tool. "
            "Tools found: %s"
            % ", ".join(t.get("name", "?") for t in self.discovery.tools)
        )

    def _extract_via_script(self, disc: Discovery) -> Dict[str, Any]:
        assert disc.script_tool
        arguments = build_script_arguments(disc, EXTRACTION_SCRIPT)
        obj = arguments.get("object")
        if isinstance(obj, dict) and "readOnly" in obj:
            obj["readOnly"] = not self.allow_design_edit
        result = self.client.call_tool(disc.script_tool, arguments)
        raw = _parse_script_result(result)
        if isinstance(raw, dict) and raw.get("error"):
            raise MCPError("Fusion extraction failed: %s" % raw["error"])
        return raw

    def _extract_via_structured(self, disc: Discovery) -> Dict[str, Any]:
        """Best effort when only typed tools exist.

        Try any tool whose name suggests a timeline/history listing and accept
        the first reply that looks like our extraction contract.
        """
        candidates = [
            name
            for name in disc.inspection_tools
            if any(k in name.lower() for k in ("timeline", "history", "feature", "design"))
        ]
        for name in candidates:
            try:
                result = self.client.call_tool(name, {})
            except MCPError:
                continue
            parsed = _try_parse(result)
            if isinstance(parsed, dict) and "timeline" in parsed:
                return parsed
        raise MCPError(
            "only structured Fusion tools are available and none returned a timeline. "
            "Inspection tools found: %s. Enable a script/execute tool, or extend "
            "fusion2solid/fusion_source.py with an adapter for these tools."
            % (", ".join(disc.inspection_tools) or "none")
        )

    def extract_design(self) -> Design:
        return raw_to_design(self.extract_raw())


# -- raw -> IR -------------------------------------------------------------

def raw_to_design(raw: Dict[str, Any]) -> Design:
    design = Design(
        name=str((raw.get("design") or {}).get("name", "")),
        units=str((raw.get("design") or {}).get("units", "mm") or "mm"),
        metadata={
            "unit_note": raw.get("unit_note", ""),
            "timeline_unreadable": raw.get("timeline_unreadable") or [],
        },
    )

    for p in raw.get("parameters", []) or []:
        design.params.append(
            Param(
                name=str(p.get("name", "")),
                expression=str(p.get("expression", "") or ""),
                value=p.get("value"),
                unit=str(p.get("unit", "") or ""),
                comment=str(p.get("comment", "") or ""),
            )
        )

    timeline = raw.get("timeline", []) or []
    # First pass: stable symbols and a Fusion-token -> symbol map, so feature
    # inputs can be expressed as references to the steps that produce them.
    token_to_symbol: Dict[str, str] = {}
    for item in timeline:
        symbol = "s%s" % item.get("index")
        token = item.get("token")
        if token:
            token_to_symbol[str(token)] = symbol

    for item in timeline:
        symbol = "s%s" % item.get("index")
        kind = item.get("kind")
        name = str(item.get("name", "") or "")
        suppressed = bool(item.get("suppressed", False))
        if kind == "sketch":
            design.steps.append(_sketch_step(item, symbol, name, suppressed))
        elif kind == "construction":
            design.steps.append(
                FeatureStep(
                    symbol=symbol,
                    fusion_type=_short(str(item.get("type", "") or "Construction")),
                    name=name,
                    props={"construction": True},
                    suppressed=suppressed,
                )
            )
        else:
            design.steps.append(
                _feature_step(item, symbol, name, suppressed, token_to_symbol)
            )

    for b in raw.get("bodies", []) or []:
        design.bodies.append(
            BodySummary(name=str(b.get("name", "")), volume=b.get("volume"), bbox=b.get("bbox"))
        )
    return design


def _sketch_step(item: Dict[str, Any], symbol: str, name: str, suppressed: bool) -> SketchStep:
    sk = item.get("sketch") or {}
    plane_raw = sk.get("plane") or {}
    plane = PlaneRef(
        kind=_plane_kind(plane_raw),
        name=str(plane_raw.get("name", "") or ""),
        geometry=plane_raw,
    )
    step = SketchStep(symbol=symbol, name=name or "Sketch", plane=plane, suppressed=suppressed)
    try:
        step.profile_count = int(sk.get("profile_count") or 0)
    except (TypeError, ValueError):
        step.profile_count = 0

    token_to_symbol: Dict[str, str] = {}
    for i, curve in enumerate(sk.get("curves", []) or []):
        esym = "%s:e%d" % (symbol, i)
        token = curve.get("token")
        if token:
            token_to_symbol[str(token)] = esym
        data = dict(curve.get("data") or {})
        data["construction"] = bool(curve.get("construction", False))
        data["fusion_curve"] = curve.get("kind")
        step.entities.append(Entity(kind=str(curve.get("kind")), data=data, source_id=token))
    for i, point in enumerate(sk.get("points", []) or []):
        psym = "%s:p%d" % (symbol, i)
        token = point.get("token")
        if token:
            token_to_symbol[str(token)] = psym
        step.points.append(Entity(kind="point", data=dict(point.get("data") or {}), source_id=token))

    for c in sk.get("constraints", []) or []:
        refs = [token_to_symbol[t] for t in c.get("tokens", []) if t in token_to_symbol]
        step.constraints.append(Constraint(kind=_short(str(c.get("type", ""))), refs=refs))
    for d in sk.get("dimensions", []) or []:
        refs = [token_to_symbol[t] for t in d.get("tokens", []) if t in token_to_symbol]
        value = d.get("length_mm")
        kind = "linear"
        low = str(d.get("type", "")).lower()
        if "angular" in low:
            kind, value = "angular", d.get("angle_deg")
        elif "radial" in low:
            kind, value = "radial", d.get("length_mm")
        elif "diameter" in low:
            kind, value = "diameter", d.get("length_mm")
        step.dimensions.append(
            Dimension(kind=kind, value=value if value is not None else 0.0, refs=refs,
                      data={"fusion_type": d.get("type"), "parameter": d.get("parameter")})
        )
    return step


def _feature_step(
    item: Dict[str, Any],
    symbol: str,
    name: str,
    suppressed: bool,
    token_to_symbol: Dict[str, str],
) -> FeatureStep:
    step = FeatureStep(
        symbol=symbol,
        fusion_type=_short(str(item.get("type", "") or "Unknown")),
        name=name,
        props=dict(item.get("props") or {}),
        suppressed=suppressed,
    )
    for ref in item.get("refs", []) or []:
        attr = str(ref.get("attr", ""))
        if attr not in REF_ATTRS:
            continue
        geometry = dict(ref.get("geometry") or {})
        token = ref.get("token")
        kind, target = _classify_ref(geometry, token, token_to_symbol)
        step.refs.append(Ref(kind=kind, target=target, geometry=geometry, note=attr))
        if kind in ("sketch_profile", "construction", "body") and target:
            step.input_symbols.append(target)
    return step


def _classify_ref(
    geometry: Dict[str, Any], token: Any, token_to_symbol: Dict[str, str]
) -> "tuple[str, str]":
    obj = str(geometry.get("object_type", "") or "")
    if token and str(token) in token_to_symbol:
        return "body", token_to_symbol[str(token)]
    if "Profile" in obj or geometry.get("sketch_token"):
        target = token_to_symbol.get(str(geometry.get("sketch_token")), "")
        return "sketch_profile", target
    if "Face" in obj or geometry.get("surface_type"):
        return "face", ""
    if "Edge" in obj or geometry.get("length_mm") is not None:
        return "edge", ""
    if "Vertex" in obj:
        return "vertex", ""
    if "Body" in obj:
        return "body", ""
    if "Construction" in obj:
        target = token_to_symbol.get(str(token), "") if token else ""
        return "construction", target
    if "Sketch" in obj:
        return "sketch_entity", token_to_symbol.get(str(token), "") if token else ""
    return "unknown", ""


def _short(name: str) -> str:
    """``adsk::fusion::ExtrudeFeature`` -> ``ExtrudeFeature``."""
    return name.rsplit("::", 1)[-1]


def _plane_kind(plane_raw: Dict[str, Any]) -> str:
    name = str(plane_raw.get("name", "") or "")
    obj = str(plane_raw.get("kind", "") or "")
    for base in BASE_PLANES:
        if name.strip().upper() == base:
            return "base"
    if "ConstructionPlane" in obj:
        return "construction"
    if "Face" in obj:
        return "face"
    return "unknown"


# -- result parsing --------------------------------------------------------

_PAYLOAD_KEYS = ("timeline", "design", "parameters")


def _parse_script_result(result: ToolResult) -> Any:
    """Find the extraction payload in a tool reply.

    The reply may be the payload itself, or a wrapper whose ``result`` /
    ``output`` / ``stdout`` field holds the script's printed JSON (as a string).
    """
    parsed = _try_parse(result)
    found = _find_payload(parsed)
    if found is not None:
        return found
    found = _find_payload_in_text(result.text or "")
    if found is not None:
        return found
    if isinstance(parsed, dict):
        return parsed
    raise MCPError(
        "could not parse the Fusion extraction output as JSON. First 400 chars: %r"
        % ((result.text or "")[:400],)
    )


def _find_payload(obj: Any) -> Any:
    if isinstance(obj, dict):
        if any(key in obj for key in _PAYLOAD_KEYS):
            return obj
        for value in obj.values():
            found = _find_payload(value)
            if found is not None:
                return found
    elif isinstance(obj, str):
        parsed = _try_json(obj)
        if parsed is not None and parsed is not obj:
            return _find_payload(parsed)
    elif isinstance(obj, list):
        for value in obj:
            found = _find_payload(value)
            if found is not None:
                return found
    return None


def _find_payload_in_text(text: str) -> Any:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        found = _find_payload(obj)
        if found is not None:
            return found
    return None


def _try_parse(result: ToolResult) -> Any:
    try:
        return result.json()
    except MCPError:
        return _try_json(result.text)


def _try_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None
