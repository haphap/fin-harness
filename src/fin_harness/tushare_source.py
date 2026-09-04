from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from .protocol import decimal_string, normalize_timestamp, require_decimal_string, sha256_bytes, sha256_json

TUSHARE_ENDPOINT = "https://api.tushare.pro"
DEFAULT_FIELDS = (
    "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,end_type,"
    "n_cashflow_act,update_flag"
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_CODE_RE = re.compile(r"^[0-9A-Z.]{1,32}$")
_PERIOD_RE = re.compile(r"^[0-9]{8}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_COMPANY_TYPES = {"1": "industrial", "2": "bank", "3": "insurance", "4": "securities"}
_REPORT_TYPES = {
    "1": ("consolidated", "reported"),
    "4": ("consolidated", "adjusted"),
    "5": ("consolidated", "pre_adjustment"),
}
_PERIODS = {
    "0331": ("Q1", "1"),
    "0630": ("H1", "2"),
    "0930": ("9M", "3"),
    "1231": ("FY", "4"),
}


class TushareSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class TushareBatch:
    fields: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    raw_hash: str
    normalized_hash: str


def fetch_cashflow(
    ts_code: str,
    period: str,
    *,
    token: str | None = None,
    timeout: float = 20.0,
) -> TushareBatch:
    if not _CODE_RE.fullmatch(ts_code):
        raise ValueError("invalid Tushare ts_code")
    if not _PERIOD_RE.fullmatch(period):
        raise ValueError("period must be YYYYMMDD")
    secret = token or os.environ.get("TUSHARE_TOKEN")
    if not secret:
        raise TushareSourceError("TUSHARE_TOKEN is not configured")
    payload = {
        "api_name": "cashflow",
        "token": secret,
        "params": {"ts_code": ts_code, "period": period, "is_calc": 0},
        "fields": DEFAULT_FIELDS,
    }
    request = urllib.request.Request(
        TUSHARE_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            final_url = response.geturl()
            if final_url != TUSHARE_ENDPOINT:
                raise TushareSourceError("unexpected Tushare redirect")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TushareSourceError("Tushare request failed") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TushareSourceError("Tushare response exceeds size limit")
    return parse_cashflow_response(raw)


def parse_cashflow_response(raw: bytes) -> TushareBatch:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TushareSourceError("Tushare response exceeds size limit")
    try:
        document = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TushareSourceError("Tushare returned invalid JSON") from exc
    if not isinstance(document, dict) or document.get("code") != 0:
        raise TushareSourceError("Tushare returned an API error")
    data = document.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("items"), list):
        raise TushareSourceError("Tushare response schema changed")
    fields = tuple(data["fields"])
    expected = tuple(DEFAULT_FIELDS.split(","))
    if fields != expected:
        raise TushareSourceError("Tushare response fields changed")
    rows = []
    for values in data["items"]:
        if not isinstance(values, list) or len(values) != len(fields):
            raise TushareSourceError("Tushare response row shape changed")
        rows.append(_normalize_row(dict(zip(fields, values, strict=True))))
    normalized = {"fields": fields, "rows": rows}
    return TushareBatch(
        fields=fields,
        rows=tuple(rows),
        raw_hash=sha256_bytes(raw),
        normalized_hash=sha256_json(normalized),
    )


def batch_to_source_fixture(
    batch: TushareBatch,
    *,
    entity_id: str,
    aliases: list[str],
    ingested_at: str,
    license_info: dict[str, Any],
    supersedes: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not _SHA256_RE.fullmatch(batch.raw_hash) or not _SHA256_RE.fullmatch(batch.normalized_hash):
        raise ValueError("batch hashes are invalid")
    ingested_at = normalize_timestamp(ingested_at, "ingested_at")
    if not isinstance(entity_id, str) or not entity_id or any(not isinstance(alias, str) or not alias for alias in aliases):
        raise ValueError("entity_id and aliases must be non-empty strings")
    if not isinstance(license_info, dict) or not license_info.get("label"):
        raise ValueError("license metadata with a label is required")
    supersedes = supersedes or {}
    records = []
    for row in batch.rows:
        observation = _map_cashflow_row(row, entity_id=entity_id, ingested_at=ingested_at)
        row_hash = sha256_json(row)
        source_record_id = "src_" + sha256_json(
            {"provider": "tushare-https", "row": row}
        ).removeprefix("sha256:")[:24]
        observation_id = "obs_" + sha256_json(
            {"source_record_id": source_record_id, "metric": "n_cashflow_act"}
        ).removeprefix("sha256:")[:24]
        observation.update(
            observation_id=observation_id,
            supersedes_observation_id=supersedes.get(row_hash),
        )
        locator = {
            "endpoint": TUSHARE_ENDPOINT,
            "api_name": "cashflow",
            "fields": list(batch.fields),
            "params": {"ts_code": row["ts_code"], "period": row["end_date"], "is_calc": "0"},
            "row_key": {
                name: row[name]
                for name in (
                    "ts_code",
                    "end_date",
                    "report_type",
                    "comp_type",
                    "end_type",
                    "ann_date",
                    "f_ann_date",
                    "update_flag",
                )
            } | {"is_calc": "0"},
            "response_raw_hash": batch.raw_hash,
            "response_normalized_hash": batch.normalized_hash,
        }
        records.append(
            {
                "source_record_id": source_record_id,
                "provider": "tushare-https",
                "locator": locator,
                "raw": row,
                "observation": observation,
            }
        )
    return {
        "fixture_version": "fin-harness/source-fixture/v1",
        "license": license_info,
        "entities": [{"entity_id": entity_id, "aliases": aliases}],
        "records": records,
    }


def _map_cashflow_row(row: dict[str, Any], *, entity_id: str, ingested_at: str) -> dict[str, Any]:
    if row.keys() != set(DEFAULT_FIELDS.split(",")):
        raise TushareSourceError("normalized Tushare row fields changed")
    report_type = row["report_type"]
    company_type = row["comp_type"]
    end_date_text = row["end_date"]
    if report_type not in _REPORT_TYPES:
        raise TushareSourceError("unsupported Tushare report_type")
    if company_type not in _COMPANY_TYPES:
        raise TushareSourceError("unsupported Tushare comp_type")
    if not isinstance(end_date_text, str) or not _PERIOD_RE.fullmatch(end_date_text):
        raise TushareSourceError("invalid Tushare end_date")
    period_mapping = _PERIODS.get(end_date_text[4:])
    if period_mapping is None or row["end_type"] != period_mapping[1]:
        raise TushareSourceError("unsupported or inconsistent Tushare end_type")
    disclosure_text = row["f_ann_date"] or row["ann_date"]
    if not isinstance(disclosure_text, str) or not _PERIOD_RE.fullmatch(disclosure_text):
        raise TushareSourceError("Tushare row has no usable disclosure date")
    value = row["n_cashflow_act"]
    require_decimal_string(value, "n_cashflow_act")
    try:
        period_end = datetime.strptime(end_date_text, "%Y%m%d").date()
        disclosure_day = datetime.strptime(disclosure_text, "%Y%m%d").date()
    except ValueError as exc:
        raise TushareSourceError("Tushare row contains an invalid date") from exc
    public_effective = datetime.combine(
        disclosure_day + timedelta(days=1),
        time.min,
        tzinfo=timezone(timedelta(hours=8)),
    )
    scope, reporting_variant = _REPORT_TYPES[report_type]
    return {
        "entity_id": entity_id,
        "metric_id": "statement.cashflow.operating_net",
        "period_start": date(period_end.year, 1, 1).isoformat(),
        "period_end": period_end.isoformat(),
        "period_label": f"{period_end.year}{period_mapping[0]}",
        "basis": "ytd",
        "scope": scope,
        "accounting_standard": "CAS",
        "company_type": _COMPANY_TYPES[company_type],
        "reporting_variant": reporting_variant,
        "source_dimensions": {
            "report_type": report_type,
            "comp_type": company_type,
            "end_type": row["end_type"],
            "is_calc": "0",
        },
        "value": value,
        "unit": "CNY",
        "currency": "CNY",
        "source_ann_date": _iso_date(row["ann_date"]),
        "source_f_ann_date": _iso_date(row["f_ann_date"]),
        "source_time_precision": "day",
        "published_at": public_effective.isoformat(),
        "ingested_at": ingested_at,
        "record_status": "active",
    }


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name, value in row.items():
        if name == "n_cashflow_act":
            if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
                raise TushareSourceError("n_cashflow_act must be a JSON number")
            normalized[name] = decimal_string(Decimal(value))
        elif value is None:
            normalized[name] = None
        elif isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TushareSourceError(f"{name} must be string-like")
        else:
            normalized[name] = str(value)
    return normalized


def _iso_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _PERIOD_RE.fullmatch(value):
        raise TushareSourceError("invalid Tushare disclosure date")
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise TushareSourceError("invalid Tushare disclosure date") from exc


def redacted_request_fingerprint(ts_code: str, period: str) -> str:
    return sha256_json(
        {
            "endpoint": TUSHARE_ENDPOINT,
            "api_name": "cashflow",
            "params": {"ts_code": ts_code, "period": period, "is_calc": 0},
            "fields": DEFAULT_FIELDS,
        }
    )
