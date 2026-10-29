"""A tiny, dependency-free MCP client (JSON-RPC 2.0).

Two transports are supported, enough for this tool:

* :class:`StdioMCPClient` -- spawn a server process and speak newline-delimited
  JSON-RPC over stdin/stdout (this is how ``solidcraft-cli mcp`` is used).
* :class:`HttpMCPClient`  -- MCP *Streamable HTTP* (POST + optional SSE), which
  is how Autodesk's Fusion Desktop MCP server is reached.

Only the pieces the migration needs are implemented: ``initialize``,
``tools/list`` and ``tools/call``. Notifications and server-initiated requests
are tolerated and ignored (requests get an empty "method not found" reply so a
strict server does not stall).
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    """Raised for transport failures and JSON-RPC errors."""


class ToolResult:
    """The result of a ``tools/call``."""

    def __init__(self, payload: Dict[str, Any]):
        self.raw = payload
        self.is_error = bool(payload.get("isError", False))
        self.content: List[Dict[str, Any]] = payload.get("content", []) or []
        self.structured = payload.get("structuredContent")

    @property
    def text(self) -> str:
        parts = [
            block.get("text", "")
            for block in self.content
            if block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)

    def json(self) -> Any:
        """Best-effort JSON decode of the text blocks.

        A ``tools/call`` result is a text block whose body is usually JSON, so
        try each block in turn and fall back to the whole concatenation.
        """
        for block in self.content:
            if block.get("type") != "text":
                continue
            parsed = _try_json(block.get("text", ""))
            if parsed is not None:
                return parsed
        parsed = _try_json(self.text)
        if parsed is not None:
            return parsed
        raise MCPError("tool result was not JSON: %r" % (self.text[:400],))

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return "ToolResult(is_error=%s, blocks=%d)" % (self.is_error, len(self.content))


def _try_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class _BaseClient:
    def __init__(self) -> None:
        self._id = 0
        self._tool_cache: Optional[List[Dict[str, Any]]] = None
        self._initialized = False

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _initialize_params(self) -> Dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "fusion2solid", "version": "0.1.0"},
        }

    # -- transport hooks ---------------------------------------------------
    def _request(self, method: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        raise NotImplementedError

    def _notify(self, method: str, params: Optional[Dict[str, Any]]) -> None:
        raise NotImplementedError

    # -- public API --------------------------------------------------------
    def initialize(self) -> Dict[str, Any]:
        result = self._request("initialize", self._initialize_params())
        self._notify("notifications/initialized", None)
        self._initialized = True
        return result

    def list_tools(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if self._tool_cache is None or refresh:
            result = self._request("tools/list", {})
            self._tool_cache = result.get("tools", []) or []
        return self._tool_cache

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> ToolResult:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        return ToolResult(result)

    def close(self) -> None:  # pragma: no cover - overridden where needed
        return None

    def __enter__(self) -> "_BaseClient":
        self.initialize()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class StdioMCPClient(_BaseClient):
    """Speak MCP over a child process' stdin/stdout, one JSON message per line."""

    def __init__(
        self,
        command: Sequence[str],
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        stderr_path: Optional[str] = None,
        timeout: float = 330.0,
    ) -> None:
        super().__init__()
        self.command = list(command)
        self.timeout = timeout
        err_target = open(stderr_path, "wb") if stderr_path else subprocess.PIPE
        self._stderr_target = err_target if stderr_path else None
        self._stderr_path = stderr_path
        try:
            self.proc = subprocess.Popen(
                self.command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err_target,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise MCPError(
                "could not start %r: %s" % (" ".join(self.command), exc)
            ) from exc
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- internals ---------------------------------------------------------
    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self._queue.put(line)
        finally:
            self._queue.put(None)

    def _write(self, message: Dict[str, Any]) -> None:
        if self.proc.stdin is None:
            raise MCPError("stdin closed")
        data = json.dumps(message)
        try:
            self.proc.stdin.write(data + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(self._server_died("broken pipe while writing")) from exc

    def _read_message(self, deadline: float) -> Dict[str, Any]:
        import time

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError("timed out waiting for a reply from the MCP server")
            try:
                line = self._queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                raise MCPError(self._server_died("server closed its output"))
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except ValueError:
                # Not protocol on this line; keep reading.
                continue

    def _server_died(self, why: str) -> str:
        detail = ""
        if self._stderr_path:
            try:
                with open(self._stderr_path, "r", encoding="utf-8", errors="replace") as fh:
                    detail = fh.read()[-2000:]
            except OSError:
                pass
        base = "MCP server (%s) %s" % (" ".join(self.command), why)
        return base + ("\n--- stderr ---\n" + detail if detail else "")

    def _exchange(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        import time

        expect_id = message.get("id")
        self._write(message)
        deadline = time.monotonic() + self.timeout
        while True:
            reply = self._read_message(deadline)
            if "id" not in reply:
                continue  # a notification
            if "method" in reply and "id" in reply:
                self._answer_server_request(reply)
                continue
            if expect_id is None or reply.get("id") == expect_id:
                return reply

    def _answer_server_request(self, request: Dict[str, Any]) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32601, "message": "client does not implement " + str(request.get("method"))},
            }
        )

    # -- transport hooks ---------------------------------------------------
    def _request(self, method: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        reply = self._exchange(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params or {}}
        )
        assert reply is not None
        if "error" in reply:
            raise MCPError("%s: %s" % (method, _format_rpc_error(reply["error"])))
        return reply.get("result", {}) or {}

    def _notify(self, method: str, params: Optional[Dict[str, Any]]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            if self._stderr_target is not None:
                self._stderr_target.close()
            for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    if pipe is not None and not getattr(pipe, "closed", False):
                        pipe.close()
                except (OSError, ValueError):  # pragma: no cover - best effort
                    pass


class HttpMCPClient(_BaseClient):
    """MCP *Streamable HTTP*: POST JSON-RPC to a single endpoint (optionally
    receiving the reply inside an SSE stream)."""

    def __init__(self, url: str, timeout: float = 330.0, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__()
        self.url = url
        self.timeout = timeout
        self._session_id: Optional[str] = None
        self._extra_headers = dict(headers or {})
        self._lock = threading.Lock()

    # -- internals ---------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        headers.update(self._extra_headers)
        return headers

    def _post(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        body = json.dumps(message).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                sid = response.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                status = getattr(response, "status", 200)
                content_type = (response.headers.get("Content-Type") or "").lower()
                if status == 202 or "id" not in message:
                    return None
                if "text/event-stream" in content_type:
                    return self._read_sse(response, message.get("id"))
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # pragma: no cover - defensive
                pass
            raise MCPError("HTTP %s from %s: %s" % (exc.code, self.url, detail or exc.reason)) from exc
        except urllib.error.URLError as exc:
            raise MCPError("could not reach %s: %s" % (self.url, exc.reason)) from exc
        if not raw:
            return None
        parsed = _try_json(raw.decode("utf-8", "replace"))
        if parsed is None:
            raise MCPError("server returned non-JSON body: %r" % (raw[:400],))
        return parsed

    def _read_sse(self, response: Any, expect_id: Any) -> Optional[Dict[str, Any]]:
        buffer: List[str] = []
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if buffer:
                    payload = "\n".join(buffer)
                    buffer = []
                    parsed = _try_json(payload)
                    if parsed is not None and parsed.get("id") == expect_id:
                        return parsed
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                buffer.append(line[len("data:"):].lstrip())
        if buffer:
            parsed = _try_json("\n".join(buffer))
            if parsed is not None and parsed.get("id") == expect_id:
                return parsed
        return None

    # -- transport hooks ---------------------------------------------------
    def _request(self, method: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        with self._lock:
            reply = self._post(
                {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params or {}}
            )
        if reply is None:
            raise MCPError("%s: server sent no reply" % method)
        if "error" in reply:
            raise MCPError("%s: %s" % (method, _format_rpc_error(reply["error"])))
        return reply.get("result", {}) or {}

    def _notify(self, method: str, params: Optional[Dict[str, Any]]) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})
        except MCPError:
            # A server may legitimately reject/accept silently; never fail the
            # handshake because of the initialized notification.
            pass


def _format_rpc_error(error: Any) -> str:
    if isinstance(error, dict):
        return "code %s: %s" % (error.get("code"), error.get("message"))
    return str(error)
