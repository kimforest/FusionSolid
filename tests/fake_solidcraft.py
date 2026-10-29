"""A stub SolidCraft MCP server, spoken over stdio.

It implements just enough of the protocol for the sink and the offline
integration test: ``initialize``, ``tools/list`` and ``tools/call`` for the
tools the migration uses. Every call is appended to the file named by the
``F2S_LOG`` environment variable, so the test can assert on the exchange.

Run with:  python tests/fake_solidcraft.py
"""

from __future__ import annotations

import json
import os
import sys

LOG = os.environ.get("F2S_LOG")

TOOLS = [
    {"name": "list_commands", "description": "command catalog",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "describe_command", "description": "describe a command",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "batch", "description": "run steps",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "new_design", "description": "new design", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "body_topology", "description": "topology", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "measure", "description": "measure", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_design", "description": "inspect", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "save", "description": "save", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "screenshot", "description": "screenshot", "inputSchema": {"type": "object", "properties": {}}},
]

COMMANDS = [
    "set_parameter", "sketch.create", "sketch.finish", "sketch.dimension",
    "sketch.line", "sketch.circle.center", "sketch.arc.center_point",
    "sketch.ellipse", "sketch.point", "sketch.constraint.coincident",
    "sketch.constraint.equal", "sketch.constraint.horizontal_vertical",
    "solid.extrude", "solid.revolve", "solid.fillet", "solid.chamfer",
    "solid.hole", "solid.shell", "solid.combine", "solid.pattern.circular",
    "solid.pattern.rectangular", "solid.mirror", "solid.move", "solid.remove",
    "timeline.suppress",
]


def _log(entry):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def _content(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _handle_call(name, args):
    _log({"tool": name, "arguments": args})
    if name == "list_commands":
        return _content({"ok": True, "commands": [{"id": c} for c in COMMANDS]})
    if name == "describe_command":
        # input/params null: the mapper's sanitizer then keeps everything, which
        # is what the integration test wants (it is not testing sanitizing).
        return _content({"ok": True, "id": args.get("id"), "params": "", "input": None})
    if name == "new_design":
        return _content({"ok": True, "document_id": "doc1"})
    if name == "body_topology":
        return _content({
            "faces": [
                {"index": 0, "name": "top", "normal": [0, 0, 1], "centroid": [20, 0, 20],
                 "point": [20, 0, 20]},
            ],
            "edges": [
                {"index": 0, "midpoint": [20, 0, 20], "length": 40.0, "ends": [[0, 0, 20], [40, 0, 20]]},
            ],
            "vertices": [],
        })
    if name == "measure":
        return _content({"ok": True, "total": {"volume_mm3": 16000.0, "area_mm2": 4800.0}})
    if name == "inspect_design":
        return _content({
            "ok": True,
            "bodies": [{"name": "Body1", "volume": 16000.0}],
            "timeline": [{"type": "Extrude", "name": "Extrude1"}],
        })
    if name == "save":
        return _content({"ok": True, "path": args.get("path")})
    if name == "screenshot":
        return _content({"ok": True, "source": "model"})
    if name == "batch":
        results = []
        created_index = 0
        for step in args.get("commands", []):
            command = step.get("command")
            if command == "sketch.create":
                results.append({"command": command, "sketch": "Sketch1", "created": []})
            elif command and command.startswith("sketch.") and command not in (
                "sketch.finish", "sketch.dimension",
            ):
                results.append({"command": command, "created": ["entity-%d" % created_index]})
                created_index += 1
            elif command and command.startswith("solid."):
                results.append({"command": command, "created_bodies": [{"name": "Body1"}],
                                "bodies": [{"name": "Body1"}]})
            else:
                results.append({"command": command})
        return _content({"ok": True, "completed": len(results), "results": results, "rolled_back": False})
    return _content({"ok": True})


def _handle(request):
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {},
                "serverInfo": {"name": "fake-solidcraft", "version": "0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = request.get("params") or {}
        return _handle_call(params.get("name"), params.get("arguments") or {})
    if method and method.startswith("notifications/"):
        return None
    return {}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if "id" not in request:
            continue  # notification
        result = _handle(request)
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
