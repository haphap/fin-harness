from __future__ import annotations

import ipaddress
import uuid
from pathlib import Path
from typing import Any

import anyio
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from . import __version__
from .core import Engine
from .protocol import (DEFAULT_TIMEOUT_SECONDS, MAX_RESPONSE_BYTES, PROTOCOL, ExecutionControl,
                       ProtocolError, canonical_json, exception_response, load_schema)
from .store import Store

INSTRUCTIONS = (
    "Use financial_analyze for auditable financial facts or derived metrics. "
    "Ask for entity, period, and as_of before calling it. Use financial_explain only when a prior run_id exists "
    "and the user asks how a result was calculated or sourced. Never pass source values, formulas, SQL, or credentials."
)

_DESCRIPTIONS = {
    "analyze": (
        "Calculate an auditable point-in-time financial metric from harness-owned facts. "
        "v0.1 supports derived.cashflow.operating.single_quarter_yoy, Q2 and consolidated scope. "
        "public uses disclosure time; system uses local ingestion history. "
        "Ask for entity, period and timezone-qualified as_of; never supply values, formulas, SQL or credentials."
    ),
    "explain": "Explain calculation steps and provenance for an existing fin-harness run_id. Use after financial_analyze.",
}


def create_mcp_server(database: str | Path, metric_registry: str | Path | None = None) -> MCPServer:
    class FinancialServer(MCPServer):
        # Public SDK extension points avoid a second contract and JSON-string argument coercion.
        async def list_tools(self) -> list[Tool]:
            definitions = load_schema("request")["$defs"]
            return [Tool(
                name=f"financial_{operation}",
                description=_DESCRIPTIONS[operation],
                input_schema={**definitions[f"{operation}Request"], "$defs": {"target": definitions["target"]}},
                output_schema=load_schema(f"{operation}-response"),
                annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False),
            ) for operation in ("analyze", "explain")]

        async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> CallToolResult:
            control = ExecutionControl()
            envelope = {"protocol": PROTOCOL, "operation": name.removeprefix("financial_"),
                        "request_id": "mcp-" + uuid.uuid4().hex, "request": arguments, "context": {"client": "mcp"}}

            def run() -> dict[str, Any]:
                try:
                    control.check()
                    if name not in ("financial_analyze", "financial_explain"):
                        raise ProtocolError("invalid_request", "unknown financial tool")
                    with Store(database) as store:
                        return Engine(store, metric_registry, control).handle(envelope)
                except Exception as exc:
                    return exception_response(envelope["request_id"], exc)

            try:
                with anyio.fail_after(DEFAULT_TIMEOUT_SECONDS):
                    response = await anyio.to_thread.run_sync(run, abandon_on_cancel=True)
            except TimeoutError:
                control.cancel()
                response = exception_response(envelope["request_id"], ProtocolError("timeout", "execution deadline exceeded"))
            except BaseException:
                control.cancel()
                raise
            return CallToolResult(content=[TextContent(type="text", text=canonical_json(response))],
                                  structured_content=response, is_error=response["status"] == "error")

    return FinancialServer("fin-harness", description="Deterministic point-in-time financial analysis for agents",
                           instructions=INSTRUCTIONS, version=__version__)


def run_mcp(args: Any) -> int:
    from .cli import _config

    config = _config(args.config)
    server = create_mcp_server(config["database"], config.get("metric_registry"))
    if args.transport == "stdio":
        server.run("stdio")
        return 0
    if not _is_loopback(args.host):
        raise ValueError("unauthenticated Streamable HTTP is restricted to a loopback host")
    server.run("streamable-http", host=args.host, port=args.port, stateless_http=True,
               json_response=True, max_request_body_size=MAX_RESPONSE_BYTES)
    return 0


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
