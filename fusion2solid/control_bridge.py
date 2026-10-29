"""Speak the SolidCraft *control channel* directly, exposing the MCP tool
surface the sink expects.

The documented way to reach SolidCraft is ``solidcraft-cli mcp --connect``
(a stdio MCP server). When that program is not installed but the app is running,
its control channel is reachable on ``127.0.0.1:PORT`` with this launch's token,
and this module bridges the same tool calls onto it. See the SolidCraft control
protocol for the message format (one JSON line per request and reply, the token
in the first request).
"""

from __future__ import annotations

import base64
import glob
import json
import os
import socket
import tempfile
import time
from typing import Any, Dict, List, Optional

from .mcp_client import MCPError, ToolResult


def token_paths(port: int) -> List[str]:
    name = "%d.token" % port
    candidates: List[str] = []
    override = os.environ.get("SOLIDCRAFT_CONTROL_DIR")
    if override:
        candidates.append(os.path.join(override, name))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "SolidCraft", "control", name))
        candidates.append(os.path.join(local, "local", "SolidCraft", "control", name))
        for package in glob.glob(os.path.join(local, "Packages", "*", "LocalCache", "Local",
                                               "SolidCraft", "control", name)):
            candidates.append(package)
    candidates.append(os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                                  "SolidCraft", "control", name))
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    candidates.append(os.path.join(xdg, "solidcraft", "control", name))
    return candidates


def read_token(port: int) -> Optional[str]:
    explicit = os.environ.get("SOLIDCRAFT_CONTROL_TOKEN")
    if explicit:
        return explicit.strip()
    for path in token_paths(port):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
            if token:
                return token
        except OSError:
            continue
    return None


def discover_port() -> Optional[int]:
    """The port in ``control/current.port`` (the app's discovery file)."""
    name = "current.port"
    for path in [p.rsplit(os.sep, 1)[0] + os.sep + name for p in token_paths(27878)]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            continue
    return None


class ControlMCPClient:
    """The MCP tool surface, implemented over the control channel."""

    def __init__(self, port: int = 27878, host: str = "127.0.0.1",
                 timeout: float = 330.0, allow_paths: Optional[List[str]] = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.allow_paths = list(allow_paths or [])
        self.sock: Optional[socket.socket] = None
        self._buf = b""
        self._id = 0
        self._first = True

    # -- connection --------------------------------------------------------
    def initialize(self) -> Dict[str, Any]:
        token = read_token(self.port)
        if not token:
            raise MCPError(
                "no SolidCraft control token for port %d. Looked in: %s. Start the app "
                "(it writes the token for this launch), or install solidcraft-cli and pass "
                "--transport cli."
                % (self.port, ", ".join(token_paths(self.port)))
            )
        self._token = token
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=min(self.timeout, 30))
        except OSError as exc:
            raise MCPError("no SolidCraft app is listening at %s:%d (%s)"
                           % (self.host, self.port, exc)) from exc
        self.sock.settimeout(self.timeout)
        return {"protocolVersion": "control", "capabilities": {}, "serverInfo": {"name": "solidcraft-control"}}

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self) -> "ControlMCPClient":
        self.initialize()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- protocol ----------------------------------------------------------
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _write(self, message: Dict[str, Any]) -> None:
        assert self.sock is not None
        self.sock.sendall((json.dumps(message) + "\n").encode("utf-8"))

    def _read(self) -> Dict[str, Any]:
        assert self.sock is not None
        while b"\n" not in self._buf:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise MCPError("the SolidCraft app closed the connection")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        try:
            message = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return self._read()  # not protocol/JSON on this line (e.g. an event)
        if isinstance(message, dict) and "id" not in message:
            return self._read()  # an event line
        return message

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        params = dict(params or {})
        request_id = self._next_id()
        message: Dict[str, Any] = {"id": request_id, "method": method, "params": params}
        if self._first:
            message["token"] = self._token
            self._first = False
        self._write(message)
        reply = self._read()
        if reply.get("ok") is False and reply.get("running"):
            return self._await(request_id)
        if reply.get("ok") is False:
            raise MCPError("%s: %s" % (method, reply.get("error")))
        return reply.get("result")

    def _await(self, request_id: int) -> Any:
        """A timed-out request is still running; poll ``control.pending``."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            time.sleep(0.5)
            pending_id = self._next_id()
            self._write({"id": pending_id, "method": "control.pending",
                         "params": {"pending": request_id}})
            reply = self._read()
            if reply.get("ok") is False:
                raise MCPError("control.pending: %s" % reply.get("error"))
            result = reply.get("result") or {}
            if result.get("done"):
                inner = result.get("reply") or {}
                if inner.get("ok") is False:
                    raise MCPError(inner.get("error") or "request failed")
                return inner.get("result")
            if result.get("pending") is None:
                raise MCPError("no result for the pending request %d" % request_id)
        raise MCPError("request %d is still running after %ss" % (request_id, self.timeout))

    def _execute(self, command: str, params: Optional[Dict[str, Any]] = None,
                 file_access: bool = False) -> Any:
        request: Dict[str, Any] = {"command": command, "params": params or {}}
        if file_access and self.allow_paths:
            request["allow_paths"] = [os.path.abspath(p) for p in self.allow_paths]
        return self._request("engine.execute", request)

    def _text(self, payload: Any) -> ToolResult:
        if isinstance(payload, (dict, list)):
            text = json.dumps(payload)
        else:
            text = "" if payload is None else str(payload)
        return ToolResult({"content": [{"type": "text", "text": text}]})

    # -- MCP tool surface --------------------------------------------------
    def list_tools(self, refresh: bool = False) -> List[Dict[str, Any]]:
        return [
            {"name": "list_commands"}, {"name": "describe_command"}, {"name": "batch"},
            {"name": "execute"}, {"name": "new_design"}, {"name": "set_parameter"},
            {"name": "measure"}, {"name": "inspect_design"}, {"name": "body_topology"},
            {"name": "save"}, {"name": "screenshot"}, {"name": "pending"},
        ]

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> ToolResult:
        arguments = arguments or {}
        handler = getattr(self, "_tool_" + name, None)
        if handler is None:
            raise MCPError("no control mapping for tool %r" % name)
        return handler(arguments)

    def _tool_list_commands(self, args: Dict[str, Any]) -> ToolResult:
        catalog = self._request("engine.commands") or {}
        commands = catalog.get("commands") if isinstance(catalog, dict) else catalog
        return self._text({"ok": True, "commands": commands or []})

    def _tool_describe_command(self, args: Dict[str, Any]) -> ToolResult:
        described = self._execute("engine.describe", {"command": args.get("id")})
        if isinstance(described, dict):
            return self._text(described)
        return self._text({"id": args.get("id"), "params": "", "input": None})

    def _tool_batch(self, args: Dict[str, Any]) -> ToolResult:
        result = self._request("engine.batch", {
            "commands": args.get("commands", []),
            "rollback": True,
        })
        if result is None:
            result = {}
        if isinstance(result, dict):
            result.setdefault("results", result.get("results") or [])
            if "rolled_back" not in result:
                result["rolled_back"] = bool((result.get("undone") or 0) > 0
                                             or not result.get("failed"))
        return self._text(result)

    def _tool_execute(self, args: Dict[str, Any]) -> ToolResult:
        return self._text(self._execute(args.get("command"), args.get("params")))

    def _tool_new_design(self, args: Dict[str, Any]) -> ToolResult:
        params = {"name": args["name"]} if args.get("name") else {}
        return self._text(self._execute("file.new", params) or {"ok": True})

    def _tool_set_parameter(self, args: Dict[str, Any]) -> ToolResult:
        params: Dict[str, Any] = {"name": args.get("name")}
        if args.get("expression") is not None:
            params["expression"] = args["expression"]
        elif args.get("value") is not None:
            params["expression"] = args["value"]
        if args.get("unit"):
            params["unit"] = args["unit"]
        return self._text(self._execute("parameters.change", params) or {"ok": True})

    def _tool_measure(self, args: Dict[str, Any]) -> ToolResult:
        bodies = self._bodies()
        volume = sum(float(b.get("volume") or 0.0) for b in bodies)
        area = sum(float(b.get("area") or 0.0) for b in bodies)
        return self._text({
            "ok": True,
            "total": {"volume_mm3": volume, "area_mm2": area, "body_count": len(bodies)},
            "bodies": bodies,
        })

    def _bodies(self) -> List[Dict[str, Any]]:
        inspect = self._request("document.inspect", {"measure": True}) or {}
        bodies = inspect.get("bodies") or []
        # The control channel reports volume_mm3 / area_mm2; the MCP tool adds
        # plain volume / area keys, so provide both.
        for body in bodies:
            if isinstance(body, dict):
                if "volume" not in body:
                    body["volume"] = body.get("volume_mm3")
                if "area" not in body:
                    body["area"] = body.get("area_mm2")
        return bodies

    def _tool_inspect_design(self, args: Dict[str, Any]) -> ToolResult:
        inspect = self._request("document.inspect", {"measure": True}) or {}
        bodies = inspect.get("bodies")
        if isinstance(bodies, list):
            for body in bodies:
                if isinstance(body, dict):
                    body.setdefault("volume", body.get("volume_mm3"))
                    body.setdefault("area", body.get("area_mm2"))
        return self._text(inspect)

    def _tool_body_topology(self, args: Dict[str, Any]) -> ToolResult:
        body = args.get("body")
        faces = self._execute("model.faces", {"body": body}) or {}
        edges = self._execute("model.edges", {"body": body}) or {}
        return self._text({
            "faces": _normalize_faces(faces),
            "edges": _normalize_edges(edges),
        })

    def _tool_save(self, args: Dict[str, Any]) -> ToolResult:
        return self._text(self._execute("file.save", {"path": args.get("path")},
                                        file_access=True) or {"ok": True})

    def _tool_screenshot(self, args: Dict[str, Any]) -> ToolResult:
        if args.get("view"):
            try:
                self._request("ui.view", {"view": args["view"]})
            except MCPError:
                pass
        # ui.render must write inside a folder the agent may use.
        target_dir = self.allow_paths[0] if self.allow_paths else tempfile.gettempdir()
        fd, path = tempfile.mkstemp(prefix="fusion2solid-", suffix=".png", dir=target_dir)
        os.close(fd)
        try:
            self._request("ui.render", {"path": path, "grid": False, "axes": False})
            with open(path, "rb") as fh:
                data = fh.read()
        except (MCPError, OSError):
            return self._text({"ok": False})
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return ToolResult({
            "content": [
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(data).decode("ascii")},
                {"type": "text", "text": json.dumps({"source": "model"})},
            ]
        })

    def _tool_pending(self, args: Dict[str, Any]) -> ToolResult:
        reply = self._request("control.pending", {"pending": args.get("pending")})
        return self._text(reply or {"done": True, "pending": None})


def _normalize_faces(payload: Any) -> List[Dict[str, Any]]:
    faces = payload.get("faces") if isinstance(payload, dict) else payload
    out = []
    for face in faces or []:
        if not isinstance(face, dict):
            continue
        out.append({
            "index": face.get("index"),
            "name": face.get("name"),
            "area": face.get("area"),
            "centroid": face.get("centroid"),
            "normal": face.get("normal") or face.get("plane_normal"),
            "point": face.get("point"),
            "cylinder": face.get("cylinder"),
        })
    return out


def _normalize_edges(payload: Any) -> List[Dict[str, Any]]:
    edges = payload.get("edges") if isinstance(payload, dict) else payload
    out = []
    for edge in edges or []:
        if not isinstance(edge, dict):
            continue
        start = edge.get("start")
        end = edge.get("end")
        midpoint = edge.get("midpoint") or edge.get("mid")
        if midpoint is None and start and end:
            midpoint = [(start[i] + end[i]) / 2.0 for i in range(3)]
        out.append({
            "index": edge.get("index"),
            "name": edge.get("name"),
            "midpoint": midpoint,
            "length": edge.get("length"),
            "ends": edge.get("ends") or ([start, end] if start and end else None),
        })
    return out
