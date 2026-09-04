from __future__ import annotations

import ipaddress
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .core import Engine
from .protocol import PROTOCOL, ProtocolError, error_response, validate_envelope
from .store import Store

INSTRUCTIONS = (
    "Use financial_analyze for auditable financial facts or derived metrics. "
    "Ask for entity, period, and as_of before calling it. Use financial_explain only when a prior run_id exists "
    "and the user asks how a result was calculated or sourced. Never pass source values, formulas, SQL, or credentials."
)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


class FinancialTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_id: str = Field(
        min_length=1,
        description="Versioned metric identifier; v0.1 supports derived.cashflow.operating.single_quarter_yoy",
    )
    period: str = Field(pattern=r"^[0-9]{4}Q[1-4]$", description="Calendar quarter such as 2026Q2")
    scope: Literal["consolidated", "parent"] = Field(
        description="Financial statement scope; v0.1 calculation supports consolidated"
    )


def create_mcp_server(database: str | Path, metric_registry: str | Path | None = None) -> MCPServer:
    server = MCPServer(
        "fin-harness",
        description="Deterministic point-in-time financial analysis for agents",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    def handle(envelope: dict[str, Any]) -> dict[str, Any]:
        store = Store(database)
        try:
            return Engine(store, metric_registry).handle(validate_envelope(envelope))
        except ProtocolError as exc:
            return error_response(envelope.get("request_id"), exc.code, exc.message)
        finally:
            store.close()

    @server.tool(
        name="financial_analyze",
        description=(
            "Calculate an auditable point-in-time financial metric from harness-owned source facts. "
            "Provide an entity ticker or alias, one or more metric/period/scope targets, an ISO-8601 as_of with timezone, "
            "and public or system knowledge policy. v0.1 supports derived.cashflow.operating.single_quarter_yoy, Q2, "
            "and consolidated scope. Do not provide financial values or formulas."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def financial_analyze(
        entity: Annotated[str, Field(min_length=1, max_length=128, description="Ticker, canonical entity ID, or alias")],
        targets: Annotated[list[FinancialTarget], Field(min_length=1, max_length=16)],
        as_of: Annotated[str, Field(description="ISO-8601 point in time including timezone")],
        knowledge_policy: Literal["system", "public"],
    ) -> dict[str, Any]:
        return handle(
            {
                "protocol": PROTOCOL,
                "operation": "analyze",
                "request_id": "mcp-" + uuid.uuid4().hex,
                "request": {
                    "entity": entity,
                    "targets": [target.model_dump() for target in targets],
                    "as_of": as_of,
                    "knowledge_policy": knowledge_policy,
                },
                "context": {"client": "mcp"},
            }
        )

    @server.tool(
        name="financial_explain",
        description=(
            "Explain calculation steps and input provenance for an existing fin-harness run. "
            "Use only after financial_analyze returned a run_id; optionally restrict the explanation to result_ids."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def financial_explain(
        run_id: Annotated[str, Field(min_length=1, max_length=128, description="run_id returned by financial_analyze")],
        result_ids: Annotated[list[str] | None, Field(min_length=1, max_length=16)] = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"run_id": run_id}
        if result_ids is not None:
            request["result_ids"] = result_ids
        return handle(
            {
                "protocol": PROTOCOL,
                "operation": "explain",
                "request_id": "mcp-" + uuid.uuid4().hex,
                "request": request,
                "context": {"client": "mcp"},
            }
        )

    return server


def run_mcp(args: Any) -> int:
    from .cli import _config

    config = _config(args.config)
    server = create_mcp_server(config["database"], config.get("metric_registry"))
    if args.transport == "stdio":
        server.run("stdio")
        return 0
    if not _is_loopback(args.host):
        raise ValueError("unauthenticated Streamable HTTP is restricted to a loopback host")
    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        stateless_http=True,
        json_response=True,
        max_request_body_size=1024 * 1024,
    )
    return 0


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
