"""Mini-framework MCP over stdio (JSON-RPC 2.0), no external dependencies.

Implements the subset of the Model Context Protocol needed by a tools-only
server: initialize, tools/list, tools/call and ping.

Important rules:
  - stdout is EXCLUSIVELY reserved for JSON-RPC messages.
  - Any log or trace must go to stderr (use log()).
"""

import json
import sys
import traceback

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}


def log(*args):
    """Write to stderr; never contaminates the JSON-RPC channel."""
    print(*args, file=sys.stderr, flush=True)


class MCPServer:
    def __init__(self, name, version="1.0.0", instructions=""):
        self.name = name
        self.version = version
        self.instructions = instructions
        self._tools = {}
        self._order = []

    def tool(self, name, description, properties, required=()):
        """Decorator that registers a tool.

        properties: dict JSON-Schema for each parameter.
        The handler receives parameters as keyword arguments.
        """

        def deco(fn):
            self._tools[name] = {
                "definition": {
                    "name": name,
                    "description": description,
                    "inputSchema": {
                        "type": "object",
                        "properties": properties,
                        "required": list(required),
                    },
                },
                "properties": set(properties.keys()),
                "required": list(required),
                "fn": fn,
            }
            self._order.append(name)
            return fn

        return deco

    def tools(self):
        """Map name -> handler, for reusing tools outside MCP
        (e.g. from the local web interface)."""
        return {name: entry["fn"] for name, entry in self._tools.items()}

    def definitions(self):
        """Tool schemas, for building generic forms."""
        return [self._tools[n]["definition"] for n in self._order]

    # ------------------------------------------------------------------ core

    def _result_text(self, value):
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        return {"content": [{"type": "text", "text": text}]}

    def _call_tool(self, name, arguments):
        entry = self._tools.get(name)
        if entry is None:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            }

        arguments = arguments or {}
        missing = [r for r in entry["required"] if r not in arguments]
        if missing:
            return {
                "content": [
                    {"type": "text", "text": "Missing required parameters: " + ", ".join(missing)}
                ],
                "isError": True,
            }

        # Discard undeclared keys to avoid breaking the handler signature.
        kwargs = {k: v for k, v in arguments.items() if k in entry["properties"]}
        try:
            return self._result_text(entry["fn"](**kwargs))
        except Exception as exc:  # noqa: BLE001 - reported to MCP client
            log(traceback.format_exc())
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }

    def _handle(self, message):
        """Return the JSON-RPC response, or None if it is a notification."""
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        def ok(result):
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}

        def error(code, message_text):
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message_text}}

        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
            return ok(
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                    "instructions": self.instructions,
                }
            )

        if method == "tools/list":
            return ok({"tools": [self._tools[n]["definition"] for n in self._order]})

        if method == "tools/call":
            return ok(self._call_tool(params.get("name"), params.get("arguments")))

        if method == "ping":
            return ok({})

        if method is not None and method.startswith("notifications/"):
            return None

        if msg_id is None:
            return None
        return error(-32601, f"Unsupported method: {method}")

    def run(self):
        try:
            sys.stdin.reconfigure(encoding="utf-8")
            sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        except AttributeError:  # Python < 3.7
            pass

        log(f"[{self.name}] MCP server started ({len(self._tools)} tools)")

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                log(f"[{self.name}] Invalid JSON discarded")
                continue

            if isinstance(message, list):  # JSON-RPC batch
                responses = [r for r in (self._handle(m) for m in message) if r is not None]
                if responses:
                    self._send(responses)
                continue

            response = self._handle(message)
            if response is not None:
                self._send(response)

    def _send(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
