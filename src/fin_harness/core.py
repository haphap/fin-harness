from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from decimal import Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from typing import Any

from . import __version__
from .protocol import (METRIC_ID, PROTOCOL, ExecutionControl, ProtocolError, check_response_size,
                       decimal_string, error_response, exception_response, normalize_timestamp,
                       require_decimal_string, sha256_json, validate_envelope)
from .store import AmbiguousSourceVersion, Store

_ROLE_TRANSFORMS = {
    "current_h1": "identity",
    "current_q1": "subtract_from_current_h1",
    "prior_h1": "identity",
    "prior_q1": "subtract_from_prior_h1",
}


class Engine:
    def __init__(self, store: Store, metric_path: str | Path | None = None, control: ExecutionControl | None = None):
        self.store = store
        self.control = control
        self.metric_path = Path(metric_path) if metric_path else _default_metric_path()
        self.metric = json.loads(self.metric_path.read_text(encoding="utf-8"))
        self.formula_hash = sha256_json(self.metric)
        # One audited definition, not an executable configuration language.
        if self.formula_hash != "sha256:2c164cbe330c93d71c76b85c9f92e686cba7b508ff1e0039b8836b42af4b5fc9":
            raise ProtocolError("replay_artifact_mismatch", "metric registry is not the audited v1 definition")
        self.build_digest = _build_digest(self.formula_hash)

    def handle(self, envelope: dict[str, Any], tenant: str = "local") -> dict[str, Any]:
        request_id = envelope.get("request_id") if isinstance(envelope, dict) else None
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            request_id = None
        try:
            validate_envelope(envelope)
            self._check()
            response = self.analyze(envelope, tenant) if envelope["operation"] == "analyze" else self.explain(envelope, tenant)
            check_response_size(response)
            return response
        except Exception as exc:
            return exception_response(request_id, exc)

    def _check(self) -> None:
        if self.control is not None:
            self.control.check()

    def analyze(self, envelope: dict[str, Any], tenant: str = "local") -> dict[str, Any]:
        control = self.control or ExecutionControl()
        control.check()
        request = envelope["request"]
        request_id = envelope["request_id"]
        as_of = normalize_timestamp(request["as_of"], "as_of")
        entity_id = self.store.resolve_entity(request["entity"])
        created_at = _now()
        run_id = "run_" + uuid.uuid4().hex
        snapshots: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []

        for target in request["targets"]:
            control.check()
            if entity_id is None:
                results.append(self._rejected_result(target, request, "ambiguous_entity", "entity alias is not uniquely resolved"))
                continue
            result, snapshot = self._analyze_target(
                entity_id=entity_id,
                target=target,
                as_of=as_of,
                request_as_of=request["as_of"],
                knowledge_policy=request["knowledge_policy"],
                tenant=tenant,
                created_at=created_at,
            )
            results.append(result)
            if snapshot is not None:
                snapshots.append(snapshot)

        statuses = {result["status"] for result in results}
        if statuses == {"ok"}:
            status = "ok"
        elif "ok" in statuses:
            status = "partial"
        else:
            status = "rejected"
        response = {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "run_id": run_id,
            "status": status,
            "results": results,
        }
        result_hash = sha256_json({"status": status, "results": results})
        check_response_size(response)
        run = {
            "run_id": run_id,
            "tenant": tenant,
            "request_id": request_id,
            "request": envelope,
            "request_hash": sha256_json(envelope),
            "as_of": as_of,
            "knowledge_policy": request["knowledge_policy"],
            "formula_hash": self.formula_hash,
            "build_digest": self.build_digest,
            "status": status,
            "response": response,
            "result_hash": result_hash,
            "created_at": created_at,
        }
        self.store.persist_run(
            run=run,
            snapshots=snapshots,
            audit_events=[
                {
                    "event_type": "run_completed",
                    "payload": {"status": status, "result_hash": result_hash},
                    "created_at": created_at,
                }
            ],
            control=control,
        )
        return response

    def _analyze_target(
        self,
        *,
        entity_id: str,
        target: dict[str, str],
        as_of: str,
        request_as_of: str,
        knowledge_policy: str,
        tenant: str,
        created_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if target["metric_id"] != METRIC_ID:
            return self._rejected_result(target, {"as_of": request_as_of, "knowledge_policy": knowledge_policy}, "unsupported_metric", "metric is not registered", entity_id), None
        if target["scope"] != "consolidated" or not target["period"].endswith("Q2"):
            return self._rejected_result(target, {"as_of": request_as_of, "knowledge_policy": knowledge_policy}, "not_applicable", "v1 supports consolidated Q2 only", entity_id), None

        year = int(target["period"][:4])
        periods = {
            "current_h1": f"{year}H1",
            "current_q1": f"{year}Q1",
            "prior_h1": f"{year - 1}H1",
            "prior_q1": f"{year - 1}Q1",
        }
        observations: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        try:
            for role, period_label in periods.items():
                observation = self.store.select_observation(
                    entity_id=entity_id,
                    period_label=period_label,
                    scope=target["scope"],
                    as_of=as_of,
                    knowledge_policy=knowledge_policy,
                )
                if observation is None:
                    missing.append(role)
                else:
                    observations[role] = observation
        except AmbiguousSourceVersion as exc:
            return self._rejected_result(target, {"as_of": request_as_of, "knowledge_policy": knowledge_policy}, "ambiguous_source_version", str(exc), entity_id), None
        if missing:
            return self._rejected_result(
                target,
                {"as_of": request_as_of, "knowledge_policy": knowledge_policy},
                "insufficient_data",
                "missing required observations: " + ", ".join(missing),
                entity_id,
            ), None

        for role, item in observations.items():
            input_year = year - 1 if role.startswith("prior_") else year
            expected_end = f"{input_year}-03-31" if role.endswith("q1") else f"{input_year}-06-30"
            if item["period_start"] != f"{input_year}-01-01" or item["period_end"] != expected_end:
                return self._rejected_result(target, {"as_of": request_as_of, "knowledge_policy": knowledge_policy},
                    "validation_failed", "input does not match the exact Q1/H1 calendar period", entity_id), None
            try:
                self.store.verify_observation(item)
            except ValueError as exc:
                return self._rejected_result(target, {"as_of": request_as_of, "knowledge_policy": knowledge_policy},
                    "validation_failed", str(exc), entity_id), None

        units = {item["unit"] for item in observations.values()}
        currencies = {item["currency"] for item in observations.values()}
        scopes = {item["scope"] for item in observations.values()}
        if len(units) != 1 or len(currencies) != 1 or scopes != {"consolidated"}:
            return self._rejected_result(target, {"as_of": request_as_of, "knowledge_policy": knowledge_policy}, "validation_failed", "input unit, currency, or scope mismatch", entity_id), None

        try:
            calculation = _calculate({role: item["value_text"] for role, item in observations.items()})
        except (InvalidOperation, DivisionByZero, Overflow, ValueError) as exc:
            return self._rejected_result(target, {"as_of": request_as_of, "knowledge_policy": knowledge_policy}, "validation_failed", str(exc), entity_id), None

        manifest_inputs = []
        provenance_inputs = []
        for role in ("current_h1", "current_q1", "prior_h1", "prior_q1"):
            item = observations[role]
            manifest_inputs.append(
                {
                    "role": role,
                    "observation_id": item["observation_id"],
                    "source_record_id": item["source_record_id"],
                    "normalized_hash": item["normalized_hash"],
                    "raw_hash": item["raw_hash"],
                    "payload_hash": item["payload_hash"],
                    "published_at": item["published_at"],
                    "ingested_at": item["ingested_at"],
                    "value": item["value_text"],
                    "unit": item["unit"],
                    "currency": item["currency"],
                }
            )
            provenance_inputs.append(
                {
                    "role": role,
                    "observation_id": item["observation_id"],
                    "source_record_id": item["source_record_id"],
                    "transform": _ROLE_TRANSFORMS[role],
                }
            )
        manifest = {
            "schema": "fin-harness/snapshot/v1",
            "tenant": tenant,
            "entity_id": entity_id,
            "target": target,
            "as_of": as_of,
            "knowledge_policy": knowledge_policy,
            "inputs": manifest_inputs,
        }
        manifest_hash = sha256_json(manifest)
        snapshot_id = "snap_" + manifest_hash.removeprefix("sha256:")[:24]
        snapshot = {
            "snapshot_id": snapshot_id,
            "tenant": tenant,
            "manifest": manifest,
            "manifest_hash": manifest_hash,
            "created_at": created_at,
        }
        key = {
            "entity_id": entity_id,
            "metric_id": target["metric_id"],
            "period": target["period"],
            "scope": target["scope"],
        }
        result_id = "result_" + sha256_json({"key": key, "snapshot_id": snapshot_id}).removeprefix("sha256:")[:24]
        result = {
            "result_id": result_id,
            "status": "ok",
            "key": key,
            "value": calculation["rounded"],
            "display_value": _percentage(calculation["rounded"]),
            "semantics": {
                "value_kind": "ratio",
                "unit": "1",
                "currency": None,
                "period_basis": "discrete",
                "accounting_standard": "CAS",
            },
            "as_of": request_as_of,
            "knowledge_policy": knowledge_policy,
            "source_time_precision": _time_precision(observations.values()),
            "formula": {
                "id": self.metric["calculation"],
                "version": self.metric["version"],
                "content_hash": self.formula_hash,
            },
            "snapshot_id": snapshot_id,
            "validation": {
                "status": "passed",
                "check_ids": ["pit", "exact_periods", "source_integrity", "same_scope", "same_currency", "same_unit", "nonzero_denominator"],
            },
            "provenance": {
                "calculation_id": "calc_" + result_id.removeprefix("result_"),
                "inputs": provenance_inputs,
            },
            "warnings": [],
        }
        return result, snapshot

    def _rejected_result(
        self,
        target: dict[str, str],
        request: dict[str, Any],
        status: str,
        message: str,
        entity_id: str | None = None,
    ) -> dict[str, Any]:
        key = {
            "entity_id": entity_id or target.get("entity", "unresolved"),
            "metric_id": target["metric_id"],
            "period": target["period"],
            "scope": target["scope"],
        }
        return {
            "result_id": "result_" + sha256_json({"key": key, "status": status}).removeprefix("sha256:")[:24],
            "status": status,
            "key": key,
            "as_of": request["as_of"],
            "knowledge_policy": request["knowledge_policy"],
            "warnings": [],
            "error": {"code": status, "message": message[:512]},
        }

    def explain(self, envelope: dict[str, Any], tenant: str = "local") -> dict[str, Any]:
        control = self.control or ExecutionControl()
        requested_run_id = envelope["request"]["run_id"]
        run = self.store.get_run(requested_run_id, tenant)
        if run is None:
            return error_response(envelope["request_id"], "snapshot_not_found", "run was not found for this tenant")
        if not self._run_intact(run):
            return error_response(envelope["request_id"], "replay_artifact_mismatch", "run content hash mismatch")
        if run["formula_hash"] != self.formula_hash or run["build_digest"] != self.build_digest:
            return error_response(envelope["request_id"], "replay_artifact_mismatch", "current artifacts do not match the recorded run")
        wanted = set(envelope["request"].get("result_ids", []))
        explanations = []
        for result in run["response"]["results"]:
            control.check()
            if wanted and result["result_id"] not in wanted:
                continue
            if result["status"] != "ok":
                continue
            snapshot = self.store.get_snapshot(result["snapshot_id"], tenant)
            if snapshot is None:
                return error_response(envelope["request_id"], "snapshot_not_found", "snapshot was not found for this tenant")
            try:
                calculation, inputs = self._recompute(snapshot)
            except ValueError as exc:
                return error_response(envelope["request_id"], "replay_artifact_mismatch", str(exc))
            explanations.append(
                {
                    "result_id": result["result_id"],
                    "formula": result["formula"],
                    "inputs": inputs,
                    "steps": [
                        {"operation": "current_h1-current_q1", "value": calculation["current"]},
                        {"operation": "prior_h1-prior_q1", "value": calculation["prior"]},
                        {"operation": "(current-prior)/abs(prior)", "value": calculation["unrounded"]},
                        {"operation": "quantize(scale=4,ROUND_HALF_EVEN)", "value": calculation["rounded"]},
                    ],
                    "validation": result["validation"],
                }
            )
        return {
            "protocol": PROTOCOL,
            "request_id": envelope["request_id"],
            "run_id": requested_run_id,
            "status": "ok",
            "results": explanations,
        }

    def replay(self, run_id: str, tenant: str = "local") -> dict[str, Any]:
        control = self.control or ExecutionControl()
        run = self.store.get_run(run_id, tenant)
        if run is None:
            return {"status": "error", "error": {"code": "snapshot_not_found", "message": "run was not found for this tenant"}}
        if not self._run_intact(run):
            return {"status": "error", "error": {"code": "replay_artifact_mismatch", "message": "run content hash mismatch"}}
        if run["formula_hash"] != self.formula_hash or run["build_digest"] != self.build_digest:
            return {"status": "error", "error": {"code": "replay_artifact_mismatch", "message": "current artifacts do not match the recorded run"}}
        replayed = []
        for result in run["response"]["results"]:
            control.check()
            if result["status"] != "ok":
                replayed.append(result)
                continue
            snapshot = self.store.get_snapshot(result["snapshot_id"], tenant)
            if snapshot is None:
                return {"status": "error", "error": {"code": "snapshot_not_found", "message": "snapshot was not found"}}
            try:
                calculation, _ = self._recompute(snapshot)
            except ValueError as exc:
                return {"status": "error", "error": {"code": "replay_artifact_mismatch", "message": str(exc)}}
            copied = dict(result)
            copied["value"] = calculation["rounded"]
            copied["display_value"] = _percentage(calculation["rounded"])
            replayed.append(copied)
        replay_hash = sha256_json({"status": run["status"], "results": replayed})
        return {
            "status": "ok" if replay_hash == run["result_hash"] else "error",
            "run_id": run_id,
            "match": replay_hash == run["result_hash"],
            "recorded_result_hash": run["result_hash"],
            "replay_result_hash": replay_hash,
        }

    @staticmethod
    def _run_intact(run: dict[str, Any]) -> bool:
        return (sha256_json(run["request"]) == run["request_hash"]
                and sha256_json({"status": run["response"]["status"], "results": run["response"]["results"]}) == run["result_hash"]
                and run["status"] == run["response"]["status"])

    def _recompute(self, snapshot: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
        if sha256_json(snapshot["manifest"]) != snapshot["manifest_hash"]:
            raise ValueError("snapshot manifest hash mismatch")
        values: dict[str, str] = {}
        inputs = []
        for saved in snapshot["manifest"]["inputs"]:
            observation = self.store.get_observation(saved["observation_id"])
            if observation is None or observation["normalized_hash"] != saved["normalized_hash"]:
                raise ValueError("snapshot observation hash mismatch")
            self.store.verify_observation(observation)
            if observation["raw_hash"] != saved["raw_hash"] or observation["payload_hash"] != saved.get("payload_hash"):
                raise ValueError("snapshot source hash mismatch")
            values[saved["role"]] = observation["value_text"]
            inputs.append(
                {
                    "role": saved["role"],
                    "observation_id": observation["observation_id"],
                    "value": observation["value_text"],
                    "unit": observation["unit"],
                    "currency": observation["currency"],
                    "published_at": observation["published_at"],
                    "ingested_at": observation["ingested_at"],
                    "source": {
                        "source_record_id": observation["source_record_id"],
                        "locator": observation["locator"],
                        "raw_hash": observation["raw_hash"],
                        "license": observation["license"],
                    },
                }
            )
        return _calculate(values), inputs


def _calculate(values: dict[str, str]) -> dict[str, str]:
    required = {"current_h1", "current_q1", "prior_h1", "prior_q1"}
    if values.keys() != required:
        raise ValueError("formula requires exactly four named inputs")
    context = Context(prec=34, rounding=ROUND_HALF_EVEN)
    for signal in (InvalidOperation, DivisionByZero, Overflow):
        context.traps[signal] = True
    with localcontext(context):
        parsed = {name: require_decimal_string(value, name) for name, value in values.items()}
        current = parsed["current_h1"] - parsed["current_q1"]
        prior = parsed["prior_h1"] - parsed["prior_q1"]
        if prior == 0:
            raise ValueError("prior single-quarter value is zero")
        unrounded = (current - prior) / abs(prior)
        rounded = unrounded.quantize(Decimal("0.0001"))
    return {
        "current": decimal_string(current),
        "prior": decimal_string(prior),
        "unrounded": decimal_string(unrounded),
        "rounded": decimal_string(rounded, 4),
    }


def _percentage(value: str) -> str:
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        return f"{(Decimal(value) * 100).quantize(Decimal('0.01')):.2f}%"


def _time_precision(observations: Any) -> str:
    values = {item["source_time_precision"] for item in observations}
    return values.pop() if len(values) == 1 else "mixed"


def _default_metric_path() -> Path:
    source_path = Path(__file__).resolve().parents[2] / "registry" / "metrics" / "operating_cashflow_q2_yoy.json"
    if source_path.exists():
        return source_path
    installed_path = Path(sys.prefix) / "share" / "fin-harness" / "registry" / "metrics" / "operating_cashflow_q2_yoy.json"
    if installed_path.exists():
        return installed_path
    raise FileNotFoundError("operating cashflow metric registry was not installed")


def _build_digest(formula_hash: str) -> str:
    digest = hashlib.sha256()
    digest.update(__version__.encode("utf-8"))
    digest.update(formula_hash.encode("ascii"))
    for name in ("protocol.py", "store.py", "core.py"):
        digest.update((Path(__file__).with_name(name)).read_bytes())
    return "sha256:" + digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
