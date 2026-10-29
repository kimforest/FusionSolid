"""Task 0: discover the Fusion MCP tool surface and pick an extraction strategy.

Autodesk does not document the individual tools of the Fusion Desktop MCP
server, so the migration inspects ``tools/list`` at run time and classifies the
result:

* ``script``     -- a tool that runs arbitrary code against the Fusion API is
                    available (preferred: one call returns the whole history);
* ``structured`` -- only typed inspection/modeling tools exist, so the timeline
                    must be walked feature by feature;
* ``none``       -- nothing usable, and the migration must stop with a clear
                    message naming what it did find.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

SCRIPT_KEYWORDS = ("script", "python", "execute", "run_code", "runcode", "eval", "api", "code")
code_names = ("code", "script", "source", "python", "command", "commandtext", "text", "program")
INSPECT_KEYWORDS = ("inspect", "list", "get", "timeline", "history", "feature", "parameter",
                    "sketch", "body", "document", "design", "info", "read")


@dataclass
class Discovery:
    strategy: str = "none"
    script_tool: Optional[str] = None
    code_param: Optional[str] = None
    code_path: List[str] = field(default_factory=list)
    fixed_args: Dict[str, Any] = field(default_factory=dict)
    inspection_tools: List[str] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "script_tool": self.script_tool,
            "code_param": self.code_param,
            "code_path": self.code_path,
            "fixed_args": self.fixed_args,
            "inspection_tools": self.inspection_tools,
            "reasons": self.reasons,
            "tool_count": len(self.tools),
            "tool_names": [t.get("name") for t in self.tools],
        }


def _tool_blob(tool: Dict[str, Any]) -> str:
    parts = [str(tool.get("name", "")), str(tool.get("title", "")), str(tool.get("description", ""))]
    return " ".join(parts).lower()


def classify(tools: Sequence[Dict[str, Any]]) -> Discovery:
    disc = Discovery(tools=list(tools))

    # 1. The Autodesk Fusion Desktop MCP server runs model code through a single
    #    "execute" tool: featureType == "script" with the code under object.script.
    for tool in tools:
        if _is_fusion_execute(tool):
            disc.script_tool = str(tool.get("name"))
            disc.code_path = ["object", "script"]
            disc.fixed_args = {"featureType": "script", "object": {"readOnly": True}}
            disc.reasons.append(
                "%r runs a script (featureType=script, object.script); extraction is read-only"
                % disc.script_tool
            )
            break

    # 2. Otherwise, any tool that looks like a generic code runner with a string
    #    parameter holding the code.
    if disc.script_tool is None:
        candidate = _best_generic_script_tool(tools)
        if candidate is not None:
            disc.script_tool = str(candidate.get("name"))
            disc.code_param = _pick_code_param(candidate)
            disc.code_path = [disc.code_param] if disc.code_param else []
            disc.reasons.append(
                "found script/execute tool %r (code parameter %r)"
                % (disc.script_tool, disc.code_param)
            )

    for tool in tools:
        if any(k in _tool_blob(tool) for k in INSPECT_KEYWORDS):
            disc.inspection_tools.append(str(tool.get("name")))

    if disc.script_tool:
        disc.strategy = "script"
    elif disc.inspection_tools:
        disc.strategy = "structured"
        disc.reasons.append(
            "no script tool found; %d inspection tools available: %s"
            % (len(disc.inspection_tools), ", ".join(disc.inspection_tools))
        )
    else:
        disc.strategy = "none"
        disc.reasons.append("no script tool and no inspection tools were found")
    return disc


def build_script_arguments(disc: Discovery, code: str) -> Dict[str, Any]:
    """Assemble the tool arguments that carry ``code`` for the discovered tool."""
    import copy

    if not disc.code_path:
        raise ValueError("no code_path for the %r tool" % disc.script_tool)
    args = copy.deepcopy(disc.fixed_args)
    node = args
    for key in disc.code_path[:-1]:
        node = node.setdefault(key, {})
    node[disc.code_path[-1]] = code
    return args


def _is_fusion_execute(tool: Dict[str, Any]) -> bool:
    props = _properties(tool)
    feature_type = props.get("featureType")
    if not isinstance(feature_type, dict):
        return False
    values = feature_type.get("enum")
    if not isinstance(values, list) or "script" not in values:
        return False
    obj = props.get("object")
    if not isinstance(obj, dict):
        return False
    script = (obj.get("properties") or {}).get("script")
    return isinstance(script, dict) and script.get("type") == "string"


def _score_generic_script_tool(tool: Dict[str, Any]) -> int:
    if not _string_properties(tool):
        return -1
    name = str(tool.get("name", "")).lower()
    blob = _tool_blob(tool)
    score = 0
    if "execute" in name:
        score += 5
    if "script" in name:
        score += 4
    if "python" in name:
        score += 3
    if "run" in name:
        score += 2
    if any(k in blob for k in ("script", "python", "execute")):
        score += 1
    # Domain-specific readers are not general code runners.
    if any(k in name for k in ("electronics", "read", "list", "get", "query")):
        score -= 3
    return score


def _best_generic_script_tool(tools: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best = None
    best_score = 0
    for tool in tools:
        score = _score_generic_script_tool(tool)
        if score > best_score:
            best, best_score = tool, score
    return best


def _properties(tool: Dict[str, Any]) -> Dict[str, Any]:
    schema = tool.get("inputSchema") or {}
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    return props if isinstance(props, dict) else {}


def _string_properties(tool: Dict[str, Any]) -> List[str]:
    out = []
    for name, spec in _properties(tool).items():
        if isinstance(spec, dict) and spec.get("type") == "string":
            out.append(name)
    return out


def _pick_code_param(tool: Dict[str, Any]) -> Optional[str]:
    strings = _string_properties(tool)
    if not strings:
        return None
    lowered = {s.lower(): s for s in strings}
    for candidate in code_names:
        for low, original in lowered.items():
            if candidate in low:
                return original
    schema = tool.get("inputSchema") or {}
    required = schema.get("required") or []
    required_strings = [r for r in required if r in strings]
    if len(required_strings) == 1:
        return required_strings[0]
    return strings[0]


def summarize_design(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A small, human-checkable view of the extracted design."""
    timeline = raw.get("timeline", []) or []
    hist: Dict[str, int] = {}
    kinds: Dict[str, int] = {}
    for item in timeline:
        hist[str(item.get("type"))] = hist.get(str(item.get("type")), 0) + 1
        kinds[str(item.get("kind"))] = kinds.get(str(item.get("kind")), 0) + 1
    return {
        "design": (raw.get("design") or {}).get("name", ""),
        "units": (raw.get("design") or {}).get("units", ""),
        "parameter_count": len(raw.get("parameters", []) or []),
        "timeline_length": len(timeline),
        "feature_types": dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0]))),
        "step_kinds": kinds,
        "body_count": len(raw.get("bodies", []) or []),
    }


def write_json(path: str, data: Any) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=False)
    return path
