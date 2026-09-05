from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .protocol import ExecutionControl, ProtocolError, canonical_json, normalize_timestamp, parse_timestamp, require_decimal_string, sha256_json, timestamp_key


class AmbiguousSourceVersion(RuntimeError):
    pass


_IMMUTABLE_TABLES = (
    "source_records",
    "observations",
    "snapshots",
    "runs",
    "audit_events",
    "source_payloads",
    "revision_links",
)

_FACT_FIELDS = ("entity_id", "metric_id", "period_start", "period_end", "basis",
                "scope", "accounting_standard", "unit", "currency")


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.path = str(Path(path).expanduser()) if str(path) != ":memory:" else ":memory:"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        if self.connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1, 2):
            self.close()
            raise ValueError("unsupported database schema version")
        self.connection.create_function("fin_timestamp", 1, timestamp_key, deterministic=True)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS entity_aliases (
                alias TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL REFERENCES entities(entity_id)
            );
            CREATE TABLE IF NOT EXISTS source_records (
                source_record_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                locator_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                normalized_hash TEXT NOT NULL,
                license_json TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                observation_id TEXT PRIMARY KEY,
                source_record_id TEXT NOT NULL UNIQUE REFERENCES source_records(source_record_id),
                entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                metric_id TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                period_label TEXT NOT NULL,
                basis TEXT NOT NULL,
                scope TEXT NOT NULL,
                accounting_standard TEXT NOT NULL,
                company_type TEXT NOT NULL,
                reporting_variant TEXT NOT NULL,
                source_dimensions_json TEXT NOT NULL,
                value_text TEXT NOT NULL,
                unit TEXT NOT NULL,
                currency TEXT NOT NULL,
                source_ann_date TEXT,
                source_f_ann_date TEXT,
                source_time_precision TEXT NOT NULL,
                published_at TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                supersedes_observation_id TEXT REFERENCES observations(observation_id),
                record_status TEXT NOT NULL CHECK(record_status IN ('active', 'withdrawn')),
                fact_key_hash TEXT NOT NULL,
                normalized_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_payloads (
                source_record_id TEXT PRIMARY KEY REFERENCES source_records(source_record_id),
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revision_links (
                observation_id TEXT PRIMARY KEY REFERENCES observations(observation_id),
                supersedes_observation_id TEXT NOT NULL REFERENCES observations(observation_id),
                reviewed_at TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS observations_pit_idx ON observations(
                entity_id, metric_id, period_label, basis, scope, published_at, ingested_at
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                tenant TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                tenant TEXT NOT NULL,
                request_id TEXT,
                request_json TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                as_of TEXT NOT NULL,
                knowledge_policy TEXT NOT NULL,
                formula_hash TEXT NOT NULL,
                build_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                response_json TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runs_tenant_idx ON runs(tenant, run_id);
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            PRAGMA user_version = 2;
            """
        )
        for table in _IMMUTABLE_TABLES:
            self.connection.executescript(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
                """
            )
        self.connection.commit()

    def import_fixture(self, path: str | Path) -> dict[str, int]:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        return self.import_document(document)

    def import_document(self, document: Any) -> dict[str, int]:
        if not isinstance(document, dict):
            raise ValueError("source fixture must be an object")
        if document.get("fixture_version") != "fin-harness/source-fixture/v1":
            raise ValueError("unsupported source fixture version")
        license_info = document.get("license")
        if not isinstance(license_info, dict) or not license_info.get("label"):
            raise ValueError("fixture license metadata is required")
        entities = document.get("entities")
        records = document.get("records")
        if not isinstance(entities, list) or not isinstance(records, list):
            raise ValueError("fixture entities and records must be arrays")

        imported_entities = 0
        imported_records = 0
        with self.connection:
            for entity in entities:
                entity_id = entity.get("entity_id")
                aliases = entity.get("aliases")
                if not isinstance(entity_id, str) or not entity_id or not isinstance(aliases, list):
                    raise ValueError("invalid fixture entity")
                cursor = self.connection.execute("INSERT OR IGNORE INTO entities(entity_id) VALUES (?)", (entity_id,))
                imported_entities += cursor.rowcount
                for alias in [entity_id, *aliases]:
                    if not isinstance(alias, str) or not alias:
                        raise ValueError("entity alias must be a non-empty string")
                    existing = self.connection.execute(
                        "SELECT entity_id FROM entity_aliases WHERE alias = ?", (alias,)
                    ).fetchone()
                    if existing and existing["entity_id"] != entity_id:
                        raise ValueError(f"alias {alias!r} is ambiguous")
                    self.connection.execute(
                        "INSERT OR IGNORE INTO entity_aliases(alias, entity_id) VALUES (?, ?)",
                        (alias, entity_id),
                    )
            for record in records:
                imported_records += self._import_record(record, license_info)
        return {"entities": imported_entities, "records": imported_records}

    def _import_record(self, record: Any, license_info: dict[str, Any]) -> int:
        if not isinstance(record, dict):
            raise ValueError("fixture record must be an object")
        required = {"source_record_id", "provider", "locator", "raw", "observation"}
        if record.keys() != required:
            raise ValueError("fixture record fields do not match source-fixture/v1")
        _reject_float(record["raw"])
        _reject_secret(record["locator"])
        observation = record["observation"]
        if not isinstance(observation, dict):
            raise ValueError("observation must be an object")
        observation_fields = {
            "observation_id",
            "entity_id",
            "metric_id",
            "period_start",
            "period_end",
            "period_label",
            "basis",
            "scope",
            "accounting_standard",
            "company_type",
            "reporting_variant",
            "source_dimensions",
            "value",
            "unit",
            "currency",
            "source_ann_date",
            "source_f_ann_date",
            "source_time_precision",
            "published_at",
            "ingested_at",
            "supersedes_observation_id",
            "record_status",
        }
        if observation.keys() != observation_fields:
            raise ValueError("observation fields do not match source-fixture/v1")
        require_decimal_string(observation["value"])
        published = parse_timestamp(observation["published_at"], "published_at")
        ingested = parse_timestamp(observation["ingested_at"], "ingested_at")
        published_text = normalize_timestamp(observation["published_at"], "published_at")
        ingested_text = normalize_timestamp(observation["ingested_at"], "ingested_at")
        conservative_day_boundary = False
        source_date = observation.get("source_f_ann_date") or observation.get("source_ann_date")
        if observation.get("source_time_precision") == "day" and isinstance(source_date, str):
            try:
                source_day = parse_timestamp(source_date + "T00:00:00+08:00", "source disclosure date")
                conservative_day_boundary = (
                    published == source_day + timedelta(days=1)
                    and source_day <= ingested < published
                )
            except ProtocolError:
                conservative_day_boundary = False
        if published > ingested and not conservative_day_boundary:
            raise ValueError("provider_time_anomaly: published_at is after ingested_at")
        if observation["source_time_precision"] not in {"instant", "day"}:
            raise ValueError("unsupported source_time_precision")
        if observation["record_status"] not in {"active", "withdrawn"}:
            raise ValueError("invalid record_status")
        source_dimensions = observation["source_dimensions"]
        required_dimensions = {"report_type", "comp_type", "end_type", "is_calc"}
        if not isinstance(source_dimensions, dict) or source_dimensions.keys() != required_dimensions:
            raise ValueError("source dimensions are incomplete")
        for value in source_dimensions.values():
            if not isinstance(value, str) or not value:
                raise ValueError("source dimension must be a non-empty string")

        locator = record["locator"]
        for name in ("response_raw_hash", "response_normalized_hash"):
            if name in locator:
                value = locator[name]
                if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
                    raise ValueError(f"{name} must be a sha256 digest")
        raw_hash = sha256_json(record["raw"])
        normalized_hash = sha256_json(observation)
        payload = {"record": record, "license": license_info}
        existing = self.connection.execute(
            "SELECT raw_hash, normalized_hash FROM source_records WHERE source_record_id = ?",
            (record["source_record_id"],),
        ).fetchone()
        if existing:
            if existing["raw_hash"] != raw_hash:
                raise ValueError("source_record_id already exists with different content")
            stored = self.get_observation(observation["observation_id"])
            if stored is None or stored["source_record_id"] != record["source_record_id"]:
                raise ValueError("source_record_id already has a different observation")
            self._compare_payload(stored, payload, reimport=True)
            if stored["payload_hash"] is None:
                # Recover missing original evidence in legacy databases only from an exact import.
                if normalized_hash != stored["normalized_hash"]:
                    raise ValueError("legacy observation requires its original import payload")
                self._save_payload(record["source_record_id"], payload)
            return 0

        fact_key = {name: observation[name] for name in _FACT_FIELDS}
        if observation["supersedes_observation_id"] is not None:
            self._validate_revision(observation, observation["supersedes_observation_id"])
        self.connection.execute(
            """
            INSERT INTO source_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["source_record_id"],
                record["provider"],
                canonical_json(record["locator"]),
                canonical_json(record["raw"]),
                raw_hash,
                raw_hash,  # Legacy column retained for compatibility; not an independent digest.
                canonical_json(license_info),
                ingested_text,
            ),
        )
        parameters = dict(observation)
        parameters.update(
            source_record_id=record["source_record_id"],
            source_dimensions_json=canonical_json(source_dimensions),
            value_text=observation["value"],
            published_at=published_text,
            ingested_at=ingested_text,
            fact_key_hash=sha256_json(fact_key),
            normalized_hash=normalized_hash,
        )
        self.connection.execute(
            """
            INSERT INTO observations (
                observation_id, source_record_id, entity_id, metric_id,
                period_start, period_end, period_label, basis, scope,
                accounting_standard, company_type, reporting_variant,
                source_dimensions_json, value_text, unit, currency,
                source_ann_date, source_f_ann_date, source_time_precision,
                published_at, ingested_at, supersedes_observation_id,
                record_status, fact_key_hash, normalized_hash
            ) VALUES (
                :observation_id, :source_record_id, :entity_id, :metric_id,
                :period_start, :period_end, :period_label, :basis, :scope,
                :accounting_standard, :company_type, :reporting_variant,
                :source_dimensions_json, :value_text, :unit, :currency,
                :source_ann_date, :source_f_ann_date, :source_time_precision,
                :published_at, :ingested_at, :supersedes_observation_id,
                :record_status, :fact_key_hash, :normalized_hash
            )
            """,
            parameters,
        )
        self._save_payload(record["source_record_id"], payload)
        return 1

    def _save_payload(self, source_record_id: str, payload: dict[str, Any]) -> None:
        self.connection.execute("INSERT INTO source_payloads VALUES (?, ?, ?)",
                                (source_record_id, canonical_json(payload), sha256_json(payload)))

    @staticmethod
    def _compare_payload(stored: dict[str, Any], payload: dict[str, Any], *, reimport: bool = False) -> None:
        record = payload["record"]
        observation = dict(record["observation"])
        for field in ("published_at", "ingested_at"):
            observation[field] = normalize_timestamp(observation[field])
        if reimport:
            observation["ingested_at"] = stored["ingested_at"]  # First sighting stays authoritative.
        for field, expected in observation.items():
            actual = stored["value_text" if field == "value" else field]
            if actual != expected:
                raise ValueError(f"observation content mismatch: {field}; use link-revision for reviewed supersession")
        locator, saved_locator = dict(record["locator"]), dict(stored["locator"])
        if reimport:
            for key in ("response_raw_hash", "response_normalized_hash"):
                locator.pop(key, None)
                saved_locator.pop(key, None)
        if (record["source_record_id"] != stored["source_record_id"] or record["provider"] != stored["provider"]
                or record["raw"] != stored["raw"] or locator != saved_locator or payload["license"] != stored["license"]):
            raise ValueError("source content mismatch")
        if not reimport and observation["ingested_at"] != stored["source_ingested_at"]:
            raise ValueError("source ingestion time mismatch")

    def verify_observation(self, observation: dict[str, Any]) -> None:
        if observation["payload_hash"] is None:
            raise ValueError("legacy source evidence missing; reimport the original fixture before analysis")
        payload = observation["payload"]
        if sha256_json(payload) != observation["payload_hash"]:
            raise ValueError("source payload hash mismatch")
        if sha256_json(payload["record"]["observation"]) != observation["normalized_hash"]:
            raise ValueError("observation hash mismatch")
        if sha256_json(observation["raw"]) != observation["raw_hash"]:
            raise ValueError("source raw hash mismatch")
        self._compare_payload(observation, payload)
        if sha256_json({name: observation[name] for name in _FACT_FIELDS}) != observation["fact_key_hash"]:
            raise ValueError("fact key hash mismatch")

    def _validate_revision(self, successor: dict[str, Any], predecessor_id: str) -> None:
        predecessor = self.get_observation(predecessor_id)
        if predecessor is None or any(successor[key] != predecessor[key] for key in _FACT_FIELDS):
            raise ValueError("revision must supersede an existing observation in the same fact family")
        self.verify_observation(predecessor)
        for field in ("published_at", "ingested_at"):
            if parse_timestamp(successor[field]) < parse_timestamp(predecessor[field]):
                raise ValueError("revision cannot precede its predecessor")
        visited = {successor["observation_id"]}
        current = predecessor_id
        while current:
            if current in visited:
                raise ValueError("revision cycle is forbidden")
            visited.add(current)
            row = self.connection.execute(
                "SELECT COALESCE(r.supersedes_observation_id, o.supersedes_observation_id) AS previous "
                "FROM observations o LEFT JOIN revision_links r USING(observation_id) WHERE o.observation_id = ?",
                (current,),
            ).fetchone()
            current = row["previous"] if row else None

    def link_revision(self, observation_id: str, predecessor_id: str, *, reviewer: str, reason: str) -> None:
        if not reviewer.strip() or not reason.strip() or len(reviewer) > 128 or len(reason) > 512:
            raise ValueError("reviewer (1..128) and reason (1..512) are required")
        with self.connection:
            successor = self.get_observation(observation_id)
            if successor is None:
                raise ValueError("successor observation not found")
            self.verify_observation(successor)
            self._validate_revision(successor, predecessor_id)
            existing = self.connection.execute("SELECT supersedes_observation_id FROM revision_links WHERE observation_id = ?",
                                               (observation_id,)).fetchone()
            previous = successor["supersedes_observation_id"] or (existing[0] if existing else None)
            if previous is not None:
                if previous != predecessor_id:
                    raise ValueError("revision relationship is immutable")
                return
            self.connection.execute("INSERT INTO revision_links VALUES (?, ?, ?, ?, ?)",
                (observation_id, predecessor_id, datetime.now(timezone.utc).isoformat(), reviewer, reason))

    def resolve_entity(self, alias: str) -> str | None:
        row = self.connection.execute(
            "SELECT entity_id FROM entity_aliases WHERE alias = ?", (alias,)
        ).fetchone()
        return None if row is None else str(row["entity_id"])

    def select_observation(
        self,
        *,
        entity_id: str,
        period_label: str,
        scope: str,
        as_of: str,
        knowledge_policy: str,
    ) -> dict[str, Any] | None:
        time_column = "ingested_at" if knowledge_policy == "system" else "published_at"
        rows = self.connection.execute(
            f"""
            SELECT o.observation_id, o.supersedes_observation_id, o.record_status,
                   r.supersedes_observation_id AS reviewed_predecessor, r.reviewed_at
            FROM observations o
            LEFT JOIN revision_links r USING(observation_id)
            WHERE o.entity_id = ?
              AND o.metric_id = 'statement.cashflow.operating_net'
              AND o.period_label = ?
              AND o.basis = 'ytd'
              AND o.scope = ?
              AND o.accounting_standard = 'CAS'
              AND fin_timestamp(o.{time_column}) <= fin_timestamp(?)
            ORDER BY fin_timestamp(o.{time_column}), fin_timestamp(o.ingested_at), o.observation_id
            """,
            (entity_id, period_label, scope, as_of),
        ).fetchall()
        if not rows:
            return None
        by_id = {str(row["observation_id"]): row for row in rows}
        superseded = {
            str(row["supersedes_observation_id"])
            for row in rows
            if row["supersedes_observation_id"] in by_id
        }
        superseded.update(row["reviewed_predecessor"] for row in rows
            if row["reviewed_predecessor"] in by_id
            and (knowledge_policy == "public" or timestamp_key(row["reviewed_at"]) <= timestamp_key(as_of)))
        leaves = [row for row in rows if row["observation_id"] not in superseded]
        if len(leaves) != 1:
            raise AmbiguousSourceVersion(f"{period_label} has {len(leaves)} eligible source versions")
        selected = leaves[0]
        if selected["record_status"] == "withdrawn":
            return None
        return self.get_observation(selected["observation_id"])

    def persist_run(
        self,
        *,
        run: dict[str, Any],
        snapshots: list[dict[str, Any]],
        audit_events: list[dict[str, Any]],
        control: ExecutionControl | None = None,
    ) -> None:
        control = control or ExecutionControl()
        control.check()
        with self.connection:
            for snapshot in snapshots:
                control.check()
                existing = self.connection.execute(
                    "SELECT manifest_hash FROM snapshots WHERE snapshot_id = ?",
                    (snapshot["snapshot_id"],),
                ).fetchone()
                if existing and existing["manifest_hash"] != snapshot["manifest_hash"]:
                    raise ValueError("snapshot_id collision")
                self.connection.execute(
                    "INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?, ?)",
                    (
                        snapshot["snapshot_id"],
                        snapshot["tenant"],
                        canonical_json(snapshot["manifest"]),
                        snapshot["manifest_hash"],
                        snapshot["created_at"],
                    ),
                )
            self.connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run["run_id"],
                    run["tenant"],
                    run["request_id"],
                    canonical_json(run["request"]),
                    run["request_hash"],
                    run["as_of"],
                    run["knowledge_policy"],
                    run["formula_hash"],
                    run["build_digest"],
                    run["status"],
                    canonical_json(run["response"]),
                    run["result_hash"],
                    run["created_at"],
                ),
            )
            for event in audit_events:
                self.connection.execute(
                    "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?)",
                    (
                        event.get("event_id", "evt_" + uuid.uuid4().hex),
                        run["run_id"],
                        event["event_type"],
                        canonical_json(event["payload"]),
                        event["created_at"],
                    ),
                )
            control.commit(self.connection)

    def get_run(self, run_id: str, tenant: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id = ? AND tenant = ?", (run_id, tenant)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        result["response"] = json.loads(result.pop("response_json"))
        return result

    def get_snapshot(self, snapshot_id: str, tenant: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = ? AND tenant = ?",
            (snapshot_id, tenant),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["manifest"] = json.loads(result.pop("manifest_json"))
        return result

    def get_observation(self, observation_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT o.*, s.provider, s.raw_json, s.locator_json, s.raw_hash, s.license_json,
                   s.ingested_at AS source_ingested_at, p.payload_json, p.payload_hash
            FROM observations o JOIN source_records s USING(source_record_id)
            LEFT JOIN source_payloads p USING(source_record_id)
            WHERE o.observation_id = ?
            """,
            (observation_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["raw"] = json.loads(result.pop("raw_json"))
        payload = result.pop("payload_json")
        result["payload"] = json.loads(payload) if payload is not None else None
        result["source_dimensions"] = json.loads(result.pop("source_dimensions_json"))
        result["locator"] = json.loads(result.pop("locator_json"))
        result["license"] = json.loads(result.pop("license_json"))
        return result


def _reject_float(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden in authority fixtures")
    if isinstance(value, dict):
        for child in value.values():
            _reject_float(child)
    elif isinstance(value, list):
        for child in value:
            _reject_float(child)


def _reject_secret(value: Any) -> None:
    secret_words = ("token", "secret", "authorization", "password", "api_key", "apikey")
    if isinstance(value, dict):
        for key, child in value.items():
            if any(word in str(key).lower() for word in secret_words):
                raise ValueError("locator contains a secret-like field")
            _reject_secret(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret(child)
    elif isinstance(value, str) and "token=" in value.lower():
        raise ValueError("locator contains a token")
