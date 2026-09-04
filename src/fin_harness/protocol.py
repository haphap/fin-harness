from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

PROTOCOL = "fin-harness/v1"
MAX_TARGETS = 16
MAX_SOURCE_CALLS = 8
MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60
METRIC_ID = "derived.cashflow.operating.single_quarter_yoy"

RESULT_STATUSES = {
    "ok",
    "insufficient_data",
    "ambiguous_entity",
    "ambiguous_metric",
    "ambiguous_source_version",
    "unsupported_metric",
    "not_applicable",
    "validation_failed",
    "source_error",
    "system_error",
}

TOP_ERROR_CODES = {
    "invalid_request",
    "unsupported_protocol",
    "permission_denied",
    "rate_limited",
    "timeout",
    "cancelled",
    "snapshot_not_found",
    "replay_artifact_mismatch",
    "source_unavailable",
    "response_too_large",
    "system_error",
}

_PERIOD_RE = re.compile(r"^(\d{4})Q([1-4])$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CancelledError(RuntimeError):
    pass


def parse_timestamp(value: Any, field: str = "timestamp") -> datetime:
    if not isinstance(value, str):
        raise ProtocolError("invalid_request", f"{field} must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError("invalid_request", f"{field} must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolError("invalid_request", f"{field} must include a timezone")
    return parsed


def normalize_timestamp(value: Any, field: str = "timestamp") -> str:
    return parse_timestamp(value, field).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def require_decimal_string(value: Any, field: str = "value") -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        raise ValueError(f"{field} must be a canonical decimal string")
    return Decimal(value)


def decimal_string(value: Decimal, scale: int | None = None) -> str:
    if not value.is_finite():
        raise ValueError("non-finite Decimal is forbidden")
    if scale is not None:
        return f"{value:.{scale}f}"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", ""} else rendered


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_string(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    def reject_nonfinite(item: Any) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("NaN and Infinity are forbidden")
        if isinstance(item, dict):
            for child in item.values():
                reject_nonfinite(child)
        elif isinstance(item, list):
            for child in item:
                reject_nonfinite(child)

    reject_nonfinite(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _exact_keys(value: dict[str, Any], allowed: set[str], required: set[str], field: str) -> None:
    missing = required - value.keys()
    extra = value.keys() - allowed
    if missing:
        raise ProtocolError("invalid_request", f"{field} missing: {', '.join(sorted(missing))}")
    if extra:
        raise ProtocolError("invalid_request", f"{field} has unknown fields: {', '.join(sorted(extra))}")


def validate_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_request", "request must be a JSON object")
    _exact_keys(
        value,
        {"protocol", "operation", "request_id", "request", "context"},
        {"protocol", "operation", "request_id", "request"},
        "envelope",
    )
    if value["protocol"] != PROTOCOL:
        raise ProtocolError("unsupported_protocol", f"only {PROTOCOL} is supported")
    if value["operation"] not in {"analyze", "explain"}:
        raise ProtocolError("invalid_request", "operation must be analyze or explain")
    request_id = value["request_id"]
    if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
        raise ProtocolError("invalid_request", "request_id must be null or a 1..128 character string")
    context = value.get("context")
    if context is not None:
        if not isinstance(context, dict):
            raise ProtocolError("invalid_request", "context must be an object")
        _exact_keys(context, {"client", "correlation_id"}, set(), "context")
        for name, maximum in (("client", 64), ("correlation_id", 128)):
            if name in context and (not isinstance(context[name], str) or not 1 <= len(context[name]) <= maximum):
                raise ProtocolError("invalid_request", f"context.{name} is invalid")
    if value["operation"] == "analyze":
        _validate_analyze(value["request"])
    else:
        _validate_explain(value["request"])
    return value


def _validate_analyze(request: Any) -> None:
    if not isinstance(request, dict):
        raise ProtocolError("invalid_request", "request must be an object")
    _exact_keys(
        request,
        {"entity", "targets", "as_of", "knowledge_policy"},
        {"entity", "targets", "as_of", "knowledge_policy"},
        "request",
    )
    if not isinstance(request["entity"], str) or not 1 <= len(request["entity"]) <= 128:
        raise ProtocolError("invalid_request", "entity must be a 1..128 character string")
    targets = request["targets"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        raise ProtocolError("invalid_request", f"targets must contain 1..{MAX_TARGETS} entries")
    seen: set[str] = set()
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            raise ProtocolError("invalid_request", f"targets[{index}] must be an object")
        _exact_keys(target, {"metric_id", "period", "scope"}, {"metric_id", "period", "scope"}, f"targets[{index}]")
        if not isinstance(target["metric_id"], str) or not target["metric_id"]:
            raise ProtocolError("invalid_request", f"targets[{index}].metric_id is invalid")
        if not isinstance(target["period"], str) or not _PERIOD_RE.fullmatch(target["period"]):
            raise ProtocolError("invalid_request", f"targets[{index}].period must look like 2026Q2")
        if target["scope"] not in {"consolidated", "parent"}:
            raise ProtocolError("invalid_request", f"targets[{index}].scope is invalid")
        fingerprint = canonical_json(target)
        if fingerprint in seen:
            raise ProtocolError("invalid_request", "targets must be unique")
        seen.add(fingerprint)
    parse_timestamp(request["as_of"], "as_of")
    if request["knowledge_policy"] not in {"system", "public"}:
        raise ProtocolError("invalid_request", "knowledge_policy must be system or public")


def _validate_explain(request: Any) -> None:
    if not isinstance(request, dict):
        raise ProtocolError("invalid_request", "request must be an object")
    _exact_keys(request, {"run_id", "result_ids"}, {"run_id"}, "request")
    if not isinstance(request["run_id"], str) or not 1 <= len(request["run_id"]) <= 128:
        raise ProtocolError("invalid_request", "run_id is invalid")
    result_ids = request.get("result_ids")
    if result_ids is not None:
        if not isinstance(result_ids, list) or not 1 <= len(result_ids) <= MAX_TARGETS:
            raise ProtocolError("invalid_request", f"result_ids must contain 1..{MAX_TARGETS} entries")
        if len(set(result_ids)) != len(result_ids) or any(not isinstance(item, str) or not item for item in result_ids):
            raise ProtocolError("invalid_request", "result_ids must be unique non-empty strings")


def error_response(request_id: str | None, code: str, message: str) -> dict[str, Any]:
    if code not in TOP_ERROR_CODES:
        code = "system_error"
        message = "internal system error"
    return {
        "protocol": PROTOCOL,
        "request_id": request_id,
        "status": "error",
        "results": [],
        "error": {"code": code, "message": message[:512]},
    }
