from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .core import Engine
from .protocol import (
    MAX_RESPONSE_BYTES,
    CancelledError,
    ProtocolError,
    canonical_json,
    error_response,
    validate_envelope,
)
from .store import Store


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fin-harness")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("invoke", "doctor", "capabilities"):
        command = subparsers.add_parser(name)
        command.add_argument("--config")
        if name != "invoke":
            command.add_argument("--json", action="store_true")
    importer = subparsers.add_parser("import-fixture")
    importer.add_argument("path")
    importer.add_argument("--config")
    importer.add_argument("--json", action="store_true")
    tushare = subparsers.add_parser("import-tushare")
    tushare.add_argument("ts_code")
    tushare.add_argument("period", help="report period as YYYYMMDD")
    tushare.add_argument("--entity-id", required=True)
    tushare.add_argument("--alias", action="append", default=[])
    tushare.add_argument("--license-label", required=True)
    tushare.add_argument("--acknowledge-license", action="store_true", required=True)
    tushare.add_argument("--config")
    tushare.add_argument("--json", action="store_true")
    explain = subparsers.add_parser("explain")
    explain.add_argument("run_id")
    explain.add_argument("--result-id", action="append", dest="result_ids")
    explain.add_argument("--config")
    explain.add_argument("--json", action="store_true")
    replay = subparsers.add_parser("replay")
    replay.add_argument("run_id")
    replay.add_argument("--config")
    replay.add_argument("--json", action="store_true")
    mcp = subparsers.add_parser("mcp")
    mcp.add_argument("--config")
    mcp.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8000)
    return parser


def _config(path: str | None) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if path:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or loaded.keys() - {"database", "metric_registry"}:
            raise ValueError("config supports only database and metric_registry")
        config.update(loaded)
    config.setdefault("database", os.environ.get("FIN_HARNESS_DB", "fin-harness.sqlite3"))
    return config


def _engine(config_path: str | None) -> tuple[Store, Engine]:
    config = _config(config_path)
    store = Store(config["database"])
    return store, Engine(store, config.get("metric_registry"))


def _write(value: Any) -> None:
    rendered = canonical_json(value).encode("utf-8")
    if len(rendered) > MAX_RESPONSE_BYTES:
        rendered = canonical_json(error_response(None, "response_too_large", "response exceeds 1 MiB")).encode("utf-8")
    sys.stdout.buffer.write(rendered + b"\n")
    sys.stdout.buffer.flush()


def _read_stdin() -> Any:
    raw = sys.stdin.buffer.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProtocolError("invalid_request", "request exceeds 1 MiB")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid_request", "stdin must contain one UTF-8 JSON object") from exc


def _cancel(*_: object) -> None:
    raise CancelledError("operation cancelled")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "mcp":
            from .mcp_adapter import run_mcp

            return run_mcp(args)
        store, engine = _engine(args.config)
        try:
            if args.command == "invoke":
                signal.signal(signal.SIGTERM, _cancel)
                envelope = validate_envelope(_read_stdin())
                _write(engine.handle(envelope))
                return 0
            if args.command == "import-fixture":
                _write({"status": "ok", "imported": store.import_fixture(args.path)})
                return 0
            if args.command == "import-tushare":
                from .tushare_source import batch_to_source_fixture, fetch_cashflow

                batch = fetch_cashflow(args.ts_code, args.period)
                document = batch_to_source_fixture(
                    batch,
                    entity_id=args.entity_id,
                    aliases=[args.ts_code, *args.alias],
                    ingested_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    license_info={
                        "label": args.license_label,
                        "purposes": ["local-research"],
                        "redistribution": False,
                    },
                )
                imported = store.import_document(document)
                _write(
                    {
                        "status": "ok",
                        "imported": imported,
                        "response_raw_hash": batch.raw_hash,
                        "response_normalized_hash": batch.normalized_hash,
                    }
                )
                return 0
            if args.command == "doctor":
                _write(
                    {
                        "status": "ok",
                        "database": str(Path(store.path).resolve()),
                        "protocol": "fin-harness/v1",
                        "metric_registry": str(engine.metric_path),
                        "formula_hash": engine.formula_hash,
                        "build_digest": engine.build_digest,
                    }
                )
                return 0
            if args.command == "capabilities":
                _write(
                    {
                        "protocols": ["fin-harness/v1"],
                        "operations": ["analyze", "explain"],
                        "tools": ["financial_analyze", "financial_explain"],
                        "metrics": [engine.metric["metric_id"]],
                        "limits": {"targets": 16, "source_calls": 8, "response_bytes": MAX_RESPONSE_BYTES},
                    }
                )
                return 0
            if args.command == "explain":
                request: dict[str, Any] = {"run_id": args.run_id}
                if args.result_ids:
                    request["result_ids"] = args.result_ids
                envelope = validate_envelope(
                    {"protocol": "fin-harness/v1", "operation": "explain", "request_id": "cli-explain", "request": request}
                )
                _write(engine.explain(envelope))
                return 0
            if args.command == "replay":
                _write(engine.replay(args.run_id))
                return 0
        finally:
            store.close()
    except ProtocolError as exc:
        _write(error_response(None, exc.code, exc.message))
        return 2
    except CancelledError:
        _write(error_response(None, "cancelled", "operation cancelled"))
        return 70
    except Exception as exc:
        from .tushare_source import TushareSourceError

        if isinstance(exc, TushareSourceError):
            _write(error_response(None, "source_unavailable", str(exc)))
            return 69
        print(f"fin-harness: {type(exc).__name__}", file=sys.stderr)
        _write(error_response(None, "system_error", "internal system error"))
        return 70
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
