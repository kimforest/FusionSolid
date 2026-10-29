"""Drive the SolidCraft MCP server (bridged to the running app).

Bridged means the server process (``solidcraft-cli mcp --connect HOST:PORT``)
forwards every call to the open app, so the user watches the timeline build.
A request that outlives the app's timeout comes back as a ``still running:``
error; the sink polls the ``pending`` tool instead of resending.
"""

from __future__ import annotations

import base64
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import refs as refmatch
from .ir import Ref
from .mapper import Catalog, Command, MappedStep, Plan
from .mcp_client import MCPError, StdioMCPClient, ToolResult


@dataclass
class StepOutcome:
    index: int
    symbol: str
    status: str  # mapped | skipped | failed
    reason: str = ""
    commands_run: int = 0
    refs_resolved: int = 0
    refs_low: int = 0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "symbol": self.symbol,
            "status": self.status,
            "reason": self.reason,
            "commands_run": self.commands_run,
            "refs_resolved": self.refs_resolved,
            "refs_low": self.refs_low,
            "error": self.error,
        }


class Registry:
    """Symbolic references produced by steps, resolved to SolidCraft handles."""

    def __init__(self) -> None:
        self.sketches: Dict[str, Any] = {}
        self.bodies: Dict[str, str] = {}
        self.entities: Dict[str, Any] = {}
        self.construction: Dict[str, Any] = {}
        self.current_body: Optional[str] = None
        self.last_sketch: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sketches": {k: v for k, v in self.sketches.items()},
            "bodies": dict(self.bodies),
            "entities": {k: v for k, v in self.entities.items()},
            "current_body": self.current_body,
        }


class SolidCraftSink:
    def __init__(self, command: Optional[List[str]] = None, client: Any = None,
                 timeout: float = 330.0, stderr_path: Optional[str] = None,
                 tol_mm: float = refmatch.DEFAULT_TOL_MM,
                 tol_deg: float = refmatch.DEFAULT_TOL_DEG):
        if client is None:
            if not command:
                raise ValueError("SolidCraftSink needs either command or client")
            client = StdioMCPClient(command, timeout=timeout, stderr_path=stderr_path)
        self.client = client
        self.command = list(command or [])
        self.registry = Registry()
        self.tol_mm = tol_mm
        self.tol_deg = tol_deg
        self._topology_cache: Dict[str, Dict[str, Any]] = {}
        self.catalog = Catalog()
        self.outcomes: List[StepOutcome] = []

    # -- lifecycle ---------------------------------------------------------
    def connect(self, design_name: str = "", create: bool = True) -> None:
        self.client.initialize()
        self.client.list_tools()
        self.catalog = self._load_catalog()
        if create:
            self.call_tool("new_design", {"name": design_name} if design_name else {})

    def close(self) -> None:
        self.client.close()

    def _load_catalog(self) -> Catalog:
        result = self.call_tool("list_commands", {})
        listed = _as_json(result, default=[])
        ids: List[str] = []
        if isinstance(listed, dict):
            commands = listed.get("commands") or listed.get("result") or []
        else:
            commands = listed or []
        for entry in commands:
            if isinstance(entry, dict) and entry.get("id"):
                ids.append(str(entry["id"]))
            elif isinstance(entry, str):
                ids.append(entry)
        return Catalog(listed=ids)

    def describe(self, command_ids: List[str]) -> None:
        described: Dict[str, Dict[str, Any]] = {}
        for command_id in dict.fromkeys(command_ids):
            if not self.catalog.has(command_id):
                continue
            try:
                result = self.call_tool("describe_command", {"id": command_id})
            except MCPError:
                continue
            data = _as_json(result, default=None)
            if isinstance(data, dict):
                described[command_id] = data
        self.catalog = Catalog(described=described, listed=self.catalog.listed)

    # -- resilient call ----------------------------------------------------
    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> ToolResult:
        result = self.client.call_tool(name, arguments or {})
        tries = 0
        while result.is_error and result.text.strip().startswith("still running:"):
            if tries >= 4:
                break
            tries += 1
            result = self.client.call_tool("pending", {})
        return result

    # -- replay ------------------------------------------------------------
    def apply_plan(self, plan: Plan) -> List[StepOutcome]:
        self.outcomes = []
        for command in plan.params:
            try:
                result = self.call_tool(command.command, command.params)
                if result.is_error:
                    raise MCPError(result.text or "set_parameter failed")
            except MCPError as exc:
                self.outcomes.append(
                    StepOutcome(-1, "param:%s" % command.params.get("name", "?"), "failed", str(exc))
                )
        for step in plan.steps:
            self.outcomes.append(self._apply_step(step))
        return self.outcomes

    def _apply_step(self, step: MappedStep) -> StepOutcome:
        outcome = StepOutcome(step.index, step.symbol, step.status, step.reason)
        if step.status != "mapped":
            return outcome
        if not step.commands:
            outcome.status = "skipped"
            outcome.reason = outcome.reason or "no commands survived reference resolution"
            return outcome

        pending = list(step.commands)
        while pending:
            stage: List[Dict[str, Any]] = []
            rest: List[Command] = []
            blocked = False
            for command in pending:
                if blocked:
                    rest.append(command)
                    continue
                try:
                    params = self._resolve_params(command, step, outcome)
                except _Unresolved:
                    # A later step in this batch produces the handle this
                    # command needs (e.g. a dimension referring to a curve the
                    # same sketch creates). Send what resolves, then retry.
                    blocked = True
                    rest.append(command)
                    continue
                stage.append({"command": command.command, "params": params})

            if not stage:
                outcome.status = "failed"
                outcome.error = "no command in this step could resolve its references"
                return outcome

            outcome.commands_run += len(stage)
            if os.environ.get("F2S_DEBUG"):
                print("[debug] %s -> %s" % (step.symbol, json.dumps(stage)[:600]),
                      file=sys.stderr)
            batch_result = self.call_tool("batch", {"commands": stage, "verbose": False})
            data = _as_json(batch_result, default={}) or {}
            results = data.get("results") if isinstance(data, dict) else None
            if results:
                self._harvest(step, results)

            if batch_result.is_error or (isinstance(data, dict) and data.get("failed")):
                failure = data.get("failed") if isinstance(data, dict) else None
                outcome.status = "failed"
                outcome.error = _failure_text(failure, batch_result)
                return outcome
            pending = rest

        if step.suppressed:
            try:
                self.call_tool("execute", {"command": "timeline.suppress",
                                           "params": {"name": step.source_name}, "verbose": False})
            except MCPError:
                outcome.status = "failed"
                outcome.error = "could not suppress %r" % step.source_name
        if outcome.status == "mapped" and step.notes:
            outcome.reason = "; ".join(step.notes)
        return outcome

    def _harvest(self, step: MappedStep, results: List[Any]) -> None:
        curve_ids: List[Any] = []
        point_ids: List[Any] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            if result.get("sketch") is not None or str(result.get("command", "")).startswith("sketch.create"):
                handle = result.get("sketch") or result.get("name")
                self.registry.sketches[step.symbol] = handle
                if handle is not None:
                    self.registry.last_sketch = handle
            for key in ("created_bodies", "changed_bodies"):
                for body in result.get(key, []) or []:
                    name = body.get("name") if isinstance(body, dict) else str(body)
                    if name:
                        self.registry.bodies.setdefault(step.symbol, name)
                        self.registry.current_body = name
            for body in result.get("bodies", []) or []:
                name = body.get("name") if isinstance(body, dict) else str(body)
                if name:
                    self.registry.current_body = name
            if result.get("feature") is not None or result.get("name"):
                self.registry.bodies.setdefault(step.symbol, result.get("name"))
            created = result.get("created")
            # A sketch command replies {"created": {points, curves, ...}}; an
            # older/other shape may be a plain list of created ids.
            if isinstance(created, dict):
                curve_ids.extend(_as_list(created.get("curves")))
                point_ids.extend(_as_list(created.get("points")))
            elif isinstance(created, list):
                curve_ids.extend(created)

        # Curve symbols are s<i>:e<n>, point symbols s<i>:p<n>, both in creation
        # order, which is the order the ids came back in.
        for index, entity_id in enumerate(curve_ids):
            self.registry.entities["%s:e%d" % (step.symbol, index)] = entity_id
        for index, entity_id in enumerate(point_ids):
            self.registry.entities["%s:p%d" % (step.symbol, index)] = entity_id

    # -- reference resolution ---------------------------------------------
    def _resolve_params(self, command: Command, step: MappedStep, outcome: StepOutcome) -> Dict[str, Any]:
        return _substitute(command.params, lambda token: self._resolve_token(token, step, outcome))

    def _resolve_token(self, token: str, step: MappedStep, outcome: StepOutcome) -> Any:
        ref = step.refs.get(token)
        if ref is None:
            return token
        value = self._resolve_ref(ref, outcome)
        if value is _UNRESOLVED:
            raise _Unresolved("no SolidCraft handle for %s (%s)" % (ref.kind, ref.note))
        return value

    def _resolve_ref(self, ref: Ref, outcome: StepOutcome) -> Any:
        if ref.kind == "construction_named":
            return self.registry.construction.get(ref.target, ref.target)
        if ref.kind == "sketch_profile":
            handle = self.registry.sketches.get(ref.target)
            if handle is None:
                # The Fusion reference did not carry a sketch token; fall back to
                # the most recently created sketch and mark it low confidence.
                handle = self.registry.last_sketch
                if handle is not None:
                    outcome.refs_low += 1
            return handle if handle is not None else _UNRESOLVED
        if ref.kind == "sketch_entity":
            return self.registry.entities.get(ref.target) or _UNRESOLVED
        if ref.kind == "body":
            handle = self.registry.bodies.get(ref.target)
            if handle is not None:
                return handle
            want = str(ref.geometry.get("name") or "")
            if want:
                for body in self._body_names():
                    if body.lower() == want.lower():
                        return body
            geometric = self._match_body(ref)
            if geometric is not None:
                return geometric
            # Fusion and SolidCraft name bodies independently; the latest body is
            # the usual target of a combine/split, so use it, flagged low.
            if self.registry.current_body:
                outcome.refs_low += 1
                return self.registry.current_body
            return _UNRESOLVED
        if ref.kind in ("face", "edge", "vertex"):
            return self._match_geometry(ref, outcome)
        return _UNRESOLVED

    def _match_geometry(self, ref: Ref, outcome: StepOutcome) -> Any:
        body = self.registry.current_body
        if not body:
            return _UNRESOLVED
        topology = self._topology(body)
        if ref.kind == "face":
            match = refmatch.match_face(ref.geometry, topology.get("faces", []), self.tol_mm, self.tol_deg)
        elif ref.kind == "edge":
            match = refmatch.match_edge(ref.geometry, topology.get("edges", []), self.tol_mm, self.tol_deg)
        else:
            match = refmatch.match_vertex(ref.geometry, topology.get("vertices", []), self.tol_mm, self.tol_deg)
        if match is None:
            return _UNRESOLVED
        if match.confidence == "low":
            outcome.refs_low += 1
        else:
            outcome.refs_resolved += 1
        return match.selection

    def _inspect_bodies(self) -> List[Dict[str, Any]]:
        try:
            data = self.inspect()
        except MCPError:
            return []
        if not isinstance(data, dict):
            return []
        return [b for b in (data.get("bodies") or []) if isinstance(b, dict) and b.get("name")]

    def _body_names(self) -> List[str]:
        return [b["name"] for b in self._inspect_bodies()]

    def _match_body(self, ref: Ref) -> Optional[str]:
        """Match a Fusion body to a SolidCraft one by bounding-box centre."""
        geometry = ref.geometry or {}
        target = _bbox_center(geometry.get("bbox")) or geometry.get("centroid")
        if target is None:
            return None
        best: Optional[Tuple[float, str]] = None
        for body in self._inspect_bodies():
            centre = _bbox_center(body.get("bbox")) or body.get("center_of_mass")
            distance = _distance(target, centre)
            if distance is not None and (best is None or distance < best[0]):
                best = (distance, body["name"])
        if best is not None and best[0] <= 1.0:
            return best[1]
        return None

    def _topology(self, body: str) -> Dict[str, Any]:
        if body in self._topology_cache:
            return self._topology_cache[body]
        data: Dict[str, Any] = {}
        try:
            result = self.call_tool("body_topology", {"body": body})
            if not result.is_error:
                parsed = _as_json(result, default=None)
                if isinstance(parsed, dict):
                    data = parsed
        except MCPError:
            data = {}
        self._topology_cache[body] = data
        return data

    # -- finalisation ------------------------------------------------------
    def save(self, path: str) -> None:
        result = self.call_tool("save", {"path": path})
        if result.is_error:
            raise MCPError("save failed: %s" % result.text)

    def inspect(self) -> Any:
        result = self.call_tool("inspect_design", {"measure": True, "sketches": False, "joints": False})
        return _as_json(result, default=None)

    def measure(self) -> Any:
        result = self.call_tool("measure", {})
        return _as_json(result, default=None)

    def screenshot(self, out_path: str, view: str = "iso") -> bool:
        try:
            result = self.call_tool("screenshot", {"view": view, "source": "model"})
        except MCPError:
            return False
        for block in result.content:
            if block.get("type") == "image" and block.get("data"):
                os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
                with open(out_path, "wb") as fh:
                    fh.write(base64.b64decode(block["data"]))
                return True
        return False


class _Unresolved(Exception):
    pass


_UNRESOLVED = object()


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _bbox_center(bbox: Any) -> Optional[List[float]]:
    if not isinstance(bbox, dict):
        return None
    lo, hi = bbox.get("min"), bbox.get("max")
    if not lo or not hi:
        return None
    return [(float(lo[i]) + float(hi[i])) / 2.0 for i in range(3)]


def _distance(a: Any, b: Any) -> Optional[float]:
    if not a or not b:
        return None
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


def _substitute(value: Any, resolver) -> Any:
    if isinstance(value, str) and value.startswith("@{") and value.endswith("}"):
        return resolver(value)
    if isinstance(value, list):
        return [_substitute(v, resolver) for v in value]
    if isinstance(value, tuple):
        return [_substitute(v, resolver) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, resolver) for k, v in value.items()}
    return value


def _as_json(result: ToolResult, default: Any) -> Any:
    try:
        return result.json()
    except MCPError:
        return default


def _failure_text(failure: Any, result: ToolResult) -> str:
    if isinstance(failure, dict):
        return "step %s (%s): %s" % (
            failure.get("index"), failure.get("command"), failure.get("error"),
        )
    return result.text or "batch failed"


def skill_hint(step: MappedStep) -> str:
    """Human-readable hint appended to a report entry (kept tiny)."""
    if step.status == "skipped":
        return step.reason or "skipped"
    return "; ".join(step.notes)


def dump_registry(registry: Registry) -> str:
    return json.dumps(registry.to_dict(), indent=2, default=str)
