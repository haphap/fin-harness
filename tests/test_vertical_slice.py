from __future__ import annotations

import copy
import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker
from fin_harness.core import Engine, _calculate
from fin_harness.mcp_adapter import INSTRUCTIONS, create_mcp_server
from fin_harness.protocol import ProtocolError, sha256_bytes, validate_envelope
from fin_harness.store import Store
from fin_harness.tushare_source import (
    DEFAULT_FIELDS,
    TUSHARE_ENDPOINT,
    TushareSourceError,
    batch_to_source_fixture,
    fetch_cashflow,
    parse_cashflow_response,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

SUCCESS_SOURCE = ROOT / "tests" / "fixtures" / "source-success.json"
MISSING_SOURCE = ROOT / "tests" / "fixtures" / "source-missing.json"
SUCCESS_REQUEST = ROOT / "protocol" / "v1" / "fixtures" / "analyze-success.request.json"
MISSING_REQUEST = ROOT / "protocol" / "v1" / "fixtures" / "analyze-missing.request.json"
REQUEST_SCHEMA = ROOT / "protocol" / "v1" / "request.schema.json"
ANALYZE_RESPONSE_SCHEMA = ROOT / "protocol" / "v1" / "analyze-response.schema.json"
EXPLAIN_RESPONSE_SCHEMA = ROOT / "protocol" / "v1" / "explain-response.schema.json"
TUSHARE_RESPONSE = ROOT / "tests" / "fixtures" / "tushare-cashflow-response.json"
EXPECTED_ANALYZE_RESULTS = ROOT / "protocol" / "v1" / "fixtures" / "analyze-success.expected-results.json"
EXPECTED_EXPLAIN_RESULTS = ROOT / "protocol" / "v1" / "fixtures" / "explain-success.expected-results.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class HarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "test.sqlite3"
        self.store = Store(self.db)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_success_explain_and_replay(self) -> None:
        self.store.import_fixture(SUCCESS_SOURCE)
        engine = Engine(self.store)
        response = engine.handle(validate_envelope(load(SUCCESS_REQUEST)))
        self.assertEqual("ok", response["status"])
        result = response["results"][0]
        self.assertEqual(load(EXPECTED_ANALYZE_RESULTS), response["results"])
        self.assertEqual("0.3333", result["value"])
        self.assertEqual("33.33%", result["display_value"])
        self.assertEqual(
            ["current_h1", "current_q1", "prior_h1", "prior_q1"],
            [item["role"] for item in result["provenance"]["inputs"]],
        )
        explanation = engine.handle(
            validate_envelope(
                {
                    "protocol": "fin-harness/v1",
                    "operation": "explain",
                    "request_id": "explain-1",
                    "request": {"run_id": response["run_id"]},
                }
            )
        )
        self.assertEqual("ok", explanation["status"])
        self.assertEqual(load(EXPECTED_EXPLAIN_RESULTS), explanation["results"])
        self.assertEqual("80", explanation["results"][0]["steps"][0]["value"])
        self.assertEqual("synthetic-fixture", explanation["results"][0]["inputs"][0]["source"]["license"]["label"])
        Draft202012Validator(load(ANALYZE_RESPONSE_SCHEMA), format_checker=FormatChecker()).validate(response)
        Draft202012Validator(load(EXPLAIN_RESPONSE_SCHEMA), format_checker=FormatChecker()).validate(explanation)
        self.assertTrue(engine.replay(response["run_id"])["match"])

    def test_formula_edges_are_deterministic(self) -> None:
        half_even_down = _calculate(
            {"current_h1": "20001", "current_q1": "0", "prior_h1": "20000", "prior_q1": "0"}
        )
        half_even_up = _calculate(
            {"current_h1": "20003", "current_q1": "0", "prior_h1": "20000", "prior_q1": "0"}
        )
        negative_prior = _calculate(
            {"current_h1": "-50", "current_q1": "0", "prior_h1": "-100", "prior_q1": "0"}
        )
        self.assertEqual("0.0000", half_even_down["rounded"])
        self.assertEqual("0.0002", half_even_up["rounded"])
        self.assertEqual("0.5000", negative_prior["rounded"])
        with self.assertRaisesRegex(ValueError, "prior single-quarter value is zero"):
            _calculate({"current_h1": "1", "current_q1": "0", "prior_h1": "1", "prior_q1": "1"})
        with self.assertRaisesRegex(ValueError, "canonical decimal string"):
            _calculate({"current_h1": "NaN", "current_q1": "0", "prior_h1": "1", "prior_q1": "0"})

    def test_missing_required_period_refuses_value(self) -> None:
        self.store.import_fixture(MISSING_SOURCE)
        response = Engine(self.store).handle(validate_envelope(load(MISSING_REQUEST)))
        self.assertEqual("rejected", response["status"])
        result = response["results"][0]
        self.assertEqual("insufficient_data", result["status"])
        self.assertNotIn("value", result)
        self.assertIn("current_q1", result["error"]["message"])

    def test_multi_target_response_is_keyed_and_partial(self) -> None:
        self.store.import_fixture(SUCCESS_SOURCE)
        request = load(SUCCESS_REQUEST)
        request["request_id"] = "multi-target"
        request["request"]["targets"].append(
            {
                "metric_id": "derived.unsupported",
                "period": "2026Q2",
                "scope": "consolidated",
            }
        )
        response = Engine(self.store).handle(validate_envelope(request))
        self.assertEqual("partial", response["status"])
        self.assertEqual(["ok", "unsupported_metric"], [item["status"] for item in response["results"]])
        keys = [tuple(item["key"][name] for name in ("entity_id", "metric_id", "period", "scope")) for item in response["results"]]
        self.assertEqual(2, len(set(keys)))
        Draft202012Validator(load(ANALYZE_RESPONSE_SCHEMA), format_checker=FormatChecker()).validate(response)

    def test_revision_is_point_in_time(self) -> None:
        self.store.import_fixture(SUCCESS_SOURCE)
        engine = Engine(self.store)
        before = load(SUCCESS_REQUEST)
        after = copy.deepcopy(before)
        after["request_id"] = "after-revision"
        after["request"]["as_of"] = "2026-08-22T12:00:00+08:00"
        self.assertEqual("0.3333", engine.handle(validate_envelope(before))["results"][0]["value"])
        self.assertEqual("0.5000", engine.handle(validate_envelope(after))["results"][0]["value"])
        fact_keys = self.store.connection.execute(
            "SELECT fact_key_hash FROM observations WHERE period_label='2026H1' ORDER BY observation_id"
        ).fetchall()
        self.assertEqual(1, len({row["fact_key_hash"] for row in fact_keys}))

    def test_public_and_system_knowledge_differ(self) -> None:
        self.store.import_fixture(SUCCESS_SOURCE)
        engine = Engine(self.store)
        public = load(SUCCESS_REQUEST)
        public["request_id"] = "public-before-ingest"
        public["request"]["as_of"] = "2026-08-09T01:00:00+08:00"
        public["request"]["knowledge_policy"] = "public"
        system = copy.deepcopy(public)
        system["request_id"] = "system-before-ingest"
        system["request"]["knowledge_policy"] = "system"
        self.assertEqual("ok", engine.handle(validate_envelope(public))["status"])
        self.assertEqual("insufficient_data", engine.handle(validate_envelope(system))["results"][0]["status"])

    def test_ambiguous_revision_refuses_value(self) -> None:
        document = load(SUCCESS_SOURCE)
        duplicate = copy.deepcopy(document["records"][0])
        duplicate["source_record_id"] = "src_current_h1_competing"
        duplicate["observation"]["observation_id"] = "obs_current_h1_competing"
        duplicate["locator"]["row_key"] += ":competing"
        duplicate["raw"]["n_cashflow_act"] = "131.000000"
        duplicate["observation"]["value"] = "131.000000"
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            json.dump({**document, "records": [duplicate]}, handle)
            path = Path(handle.name)
        try:
            self.store.import_fixture(SUCCESS_SOURCE)
            self.store.import_fixture(path)
        finally:
            path.unlink()
        response = Engine(self.store).handle(validate_envelope(load(SUCCESS_REQUEST)))
        self.assertEqual("ambiguous_source_version", response["results"][0]["status"])
        self.assertNotIn("value", response["results"][0])

    def test_time_anomaly_is_rejected(self) -> None:
        document = load(MISSING_SOURCE)
        document["records"] = [document["records"][0]]
        document["records"][0]["observation"]["published_at"] = "2026-08-10T00:00:00+08:00"
        document["records"][0]["observation"]["ingested_at"] = "2026-08-09T00:00:00+08:00"
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            json.dump(document, handle)
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "provider_time_anomaly"):
                self.store.import_fixture(path)
        finally:
            path.unlink()

    def test_authority_fixture_rejects_float(self) -> None:
        document = load(MISSING_SOURCE)
        document["records"] = [document["records"][0]]
        document["records"][0]["raw"]["n_cashflow_act"] = 1.1
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            json.dump(document, handle)
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "binary float"):
                self.store.import_fixture(path)
        finally:
            path.unlink()

    def test_authority_tables_are_append_only(self) -> None:
        self.store.import_fixture(SUCCESS_SOURCE)
        Engine(self.store).handle(validate_envelope(load(SUCCESS_REQUEST)))
        statements = (
            "UPDATE source_records SET provider=provider",
            "UPDATE observations SET value_text=value_text",
            "UPDATE snapshots SET tenant=tenant",
            "UPDATE runs SET status=status",
            "UPDATE audit_events SET event_type=event_type",
        )
        for statement in statements:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                self.store.connection.execute(statement)

    def test_explain_enforces_tenant_and_replay_artifacts(self) -> None:
        self.store.import_fixture(SUCCESS_SOURCE)
        engine = Engine(self.store)
        response = engine.handle(validate_envelope(load(SUCCESS_REQUEST)), tenant="tenant-a")
        denied = engine.handle(
            validate_envelope(
                {
                    "protocol": "fin-harness/v1",
                    "operation": "explain",
                    "request_id": "tenant-check",
                    "request": {"run_id": response["run_id"]},
                }
            ),
            tenant="tenant-b",
        )
        self.assertEqual("snapshot_not_found", denied["error"]["code"])
        metric = load(ROOT / "registry" / "metrics" / "operating_cashflow_q2_yoy.json")
        metric["version"] = "changed-for-test"
        metric_path = Path(self.tempdir.name) / "changed-metric.json"
        metric_path.write_text(json.dumps(metric), encoding="utf-8")
        with self.assertRaises(ProtocolError) as caught:
            Engine(self.store, metric_path)
        self.assertEqual("replay_artifact_mismatch", caught.exception.code)

    def test_invalid_protocol_is_typed(self) -> None:
        request = load(SUCCESS_REQUEST)
        request["protocol"] = "fin-harness/v2"
        with self.assertRaises(ProtocolError) as caught:
            validate_envelope(request)
        self.assertEqual("unsupported_protocol", caught.exception.code)

    def test_checked_in_schemas_and_request_fixtures(self) -> None:
        schemas = [load(REQUEST_SCHEMA), load(ANALYZE_RESPONSE_SCHEMA), load(EXPLAIN_RESPONSE_SCHEMA)]
        for schema in schemas:
            Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schemas[0], format_checker=FormatChecker())
        validator.validate(load(SUCCESS_REQUEST))
        validator.validate(load(MISSING_REQUEST))

    def test_cli_stdout_is_one_json_line(self) -> None:
        config = Path(self.tempdir.name) / "config.json"
        config.write_text(json.dumps({"database": str(self.db)}), encoding="utf-8")
        environment = {**os.environ, "PYTHONPATH": str(SRC)}
        imported = subprocess.run(
            [sys.executable, "-m", "fin_harness.cli", "import-fixture", str(SUCCESS_SOURCE), "--config", str(config)],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(1, len(imported.stdout.splitlines()))
        invoked = subprocess.run(
            [sys.executable, "-m", "fin_harness.cli", "invoke", "--config", str(config)],
            cwd=ROOT,
            env=environment,
            input=SUCCESS_REQUEST.read_text(encoding="utf-8"),
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(1, len(invoked.stdout.splitlines()))
        cli_response = json.loads(invoked.stdout)
        self.assertEqual("0.3333", cli_response["results"][0]["value"])
        direct = Engine(self.store).handle(validate_envelope(load(SUCCESS_REQUEST)))
        self.assertEqual(direct["results"], cli_response["results"])

    def test_mcp_discovery_and_calls_share_core_contract(self) -> None:
        self.store.import_fixture(SUCCESS_SOURCE)

        async def exercise() -> None:
            from mcp import Client

            async with Client(create_mcp_server(self.db)) as client:
                listed = await client.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                self.assertEqual({"financial_analyze", "financial_explain"}, tools.keys())
                self.assertLessEqual(len(INSTRUCTIONS), 512)
                for tool in tools.values():
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.destructive_hint)
                    self.assertFalse(tool.annotations.open_world_hint)
                analyze_schema = tools["financial_analyze"].input_schema
                self.assertEqual(
                    ["metric_id", "period", "scope"],
                    analyze_schema["$defs"]["target"]["required"],
                )
                self.assertFalse(analyze_schema["$defs"]["target"]["additionalProperties"])
                self.assertEqual(16, analyze_schema["properties"]["targets"]["maxItems"])

                direct_request = load(SUCCESS_REQUEST)
                direct = Engine(self.store).handle(validate_envelope(copy.deepcopy(direct_request)))
                analyzed = await client.call_tool("financial_analyze", direct_request["request"])
                self.assertFalse(analyzed.is_error)
                response = analyzed.structured_content
                self.assertIsNotNone(response)
                assert response is not None
                self.assertEqual("0.3333", response["results"][0]["value"])
                self.assertEqual(direct["results"], response["results"])

                explained = await client.call_tool("financial_explain", {"run_id": response["run_id"]})
                self.assertFalse(explained.is_error)
                explanation = explained.structured_content
                self.assertIsNotNone(explanation)
                assert explanation is not None
                self.assertEqual("80", explanation["results"][0]["steps"][0]["value"])

                missing_request = load(MISSING_REQUEST)["request"]
                missing_request["as_of"] = "2024-01-01T00:00:00+08:00"
                missing = await client.call_tool("financial_analyze", missing_request)
                self.assertFalse(missing.is_error)
                assert missing.structured_content is not None
                self.assertEqual("insufficient_data", missing.structured_content["results"][0]["status"])

        asyncio.run(exercise())

    def test_streamable_http_matches_direct_core(self) -> None:
        self.store.import_fixture(SUCCESS_SOURCE)
        direct = Engine(self.store).handle(validate_envelope(load(SUCCESS_REQUEST)))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = Path(self.tempdir.name) / "http-config.json"
        config.write_text(json.dumps({"database": str(self.db)}), encoding="utf-8")
        environment = {**os.environ, "PYTHONPATH": str(SRC)}
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "fin_harness.cli",
                "mcp",
                "--transport",
                "streamable-http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--config",
                str(config),
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if process.poll() is not None:
                        self.fail("Streamable HTTP server exited during startup")
                    time.sleep(0.05)
            else:
                self.fail("Streamable HTTP server did not start")

            async def exercise() -> None:
                from mcp import Client

                async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                    result = await client.call_tool("financial_analyze", load(SUCCESS_REQUEST)["request"])
                    self.assertFalse(result.is_error)
                    assert result.structured_content is not None
                    self.assertEqual(direct["results"], result.structured_content["results"])

            asyncio.run(exercise())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def test_mcp_stdio_subprocess_smoke(self) -> None:
        self.store.import_fixture(SUCCESS_SOURCE)
        config = Path(self.tempdir.name) / "stdio-config.json"
        config.write_text(json.dumps({"database": str(self.db)}), encoding="utf-8")

        async def exercise() -> None:
            from mcp import Client, StdioServerParameters

            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "fin_harness.cli", "mcp", "--config", str(config)],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(SRC)},
            )
            async with Client(parameters) as client:
                listed = await client.list_tools()
                self.assertEqual(
                    ["financial_analyze", "financial_explain"],
                    sorted(tool.name for tool in listed.tools),
                )
                result = await client.call_tool("financial_analyze", load(SUCCESS_REQUEST)["request"])
                self.assertFalse(result.is_error)
                assert result.structured_content is not None
                self.assertEqual("0.3333", result.structured_content["results"][0]["value"])

        asyncio.run(exercise())

    def test_doctor_and_capabilities_are_machine_readable(self) -> None:
        config = Path(self.tempdir.name) / "cli-config.json"
        config.write_text(json.dumps({"database": str(self.db)}), encoding="utf-8")
        environment = {**os.environ, "PYTHONPATH": str(SRC)}
        for command in ("doctor", "capabilities"):
            completed = subprocess.run(
                [sys.executable, "-m", "fin_harness.cli", command, "--config", str(config), "--json"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(1, len(completed.stdout.splitlines()))
            self.assertTrue(json.loads(completed.stdout))

    def test_host_examples_are_parseable_and_secret_free(self) -> None:
        host_dir = ROOT / "examples" / "hosts"
        with (host_dir / "chatgpt-codex.toml").open("rb") as handle:
            chatgpt = tomllib.load(handle)
        self.assertEqual(
            ["financial_analyze", "financial_explain"],
            chatgpt["mcp_servers"]["fin_harness"]["enabled_tools"],
        )
        opencode_text = (host_dir / "opencode.jsonc").read_text(encoding="utf-8")
        opencode = json.loads("\n".join(line for line in opencode_text.splitlines() if not line.lstrip().startswith("//")))
        self.assertEqual("local", opencode["mcp"]["servers"]["fin-harness"]["type"])
        deepseek = (host_dir / "deepseek.cordis.patch.yml").read_text(encoding="utf-8")
        pi_adapter = (ROOT / "integrations" / "pi" / "fin-harness.ts").read_text(encoding="utf-8")
        self.assertIn("@deepseek-ai/dsh-mcp-client", deepseek)
        self.assertEqual(2, pi_adapter.count("pi.registerTool({"))
        combined = "\n".join((opencode_text, deepseek, pi_adapter, json.dumps(chatgpt)))
        self.assertNotIn("TUSHARE_TOKEN=", combined)
        self.assertNotIn("Bearer ", combined)


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def geturl(self) -> str:
        return TUSHARE_ENDPOINT

    def read(self, _: int) -> bytes:
        return self.payload


class TushareSourceTest(unittest.TestCase):
    def test_decimal_is_parsed_before_float(self) -> None:
        fields = DEFAULT_FIELDS.split(",")
        row = ["TEST001.CN", "20260808", "20260808", "20260630", "1", "1", "2", 1.2300, "0"]
        raw = json.dumps({"code": 0, "data": {"fields": fields, "items": [row]}}, separators=(",", ":")).encode()
        with patch("urllib.request.urlopen", return_value=FakeResponse(raw)):
            batch = fetch_cashflow("TEST001.CN", "20260630", token="never-log-this")
        self.assertEqual("1.23", batch.rows[0]["n_cashflow_act"])
        self.assertEqual(sha256_bytes(raw), batch.raw_hash)
        self.assertNotIn("never-log-this", repr(batch))

    def test_raw_response_maps_to_importable_observation(self) -> None:
        raw = TUSHARE_RESPONSE.read_bytes()
        batch = parse_cashflow_response(raw)
        document = batch_to_source_fixture(
            batch,
            entity_id="cn.company.test001",
            aliases=["TEST001.CN"],
            ingested_at="2026-08-08T12:00:00Z",
            license_info={"label": "synthetic-fixture", "purposes": ["test"], "redistribution": True},
        )
        self.assertEqual("130", document["records"][0]["observation"]["value"])
        self.assertEqual("2026-08-09T00:00:00+08:00", document["records"][0]["observation"]["published_at"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mapped.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with Store(Path(directory) / "mapped.sqlite3") as store:
                self.assertEqual(1, store.import_fixture(path)["records"])
                observation = store.get_observation(document["records"][0]["observation"]["observation_id"])
                assert observation is not None
                self.assertEqual(batch.raw_hash, observation["locator"]["response_raw_hash"])
                self.assertEqual(0, store.import_fixture(path)["records"])
                selection = {
                    "entity_id": "cn.company.test001",
                    "period_label": "2026H1",
                    "scope": "consolidated",
                }
                self.assertIsNotNone(
                    store.select_observation(
                        **selection,
                        as_of="2026-08-08T12:00:01Z",
                        knowledge_policy="system",
                    )
                )
                self.assertIsNone(
                    store.select_observation(
                        **selection,
                        as_of="2026-08-08T15:59:59Z",
                        knowledge_policy="public",
                    )
                )
                self.assertIsNotNone(
                    store.select_observation(
                        **selection,
                        as_of="2026-08-08T16:00:00Z",
                        knowledge_policy="public",
                    )
                )

    def test_unknown_tushare_dimensions_fail_closed(self) -> None:
        raw = json.loads(TUSHARE_RESPONSE.read_text(encoding="utf-8"))
        raw["data"]["items"][0][6] = "unknown"
        batch = parse_cashflow_response(json.dumps(raw).encode())
        with self.assertRaisesRegex(TushareSourceError, "end_type"):
            batch_to_source_fixture(
                batch,
                entity_id="cn.company.test001",
                aliases=["TEST001.CN"],
                ingested_at="2026-08-09T00:00:00Z",
                license_info={"label": "synthetic-fixture"},
            )

    def test_tushare_import_without_token_is_typed_and_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "PYTHONPATH": str(SRC)}
            environment.pop("TUSHARE_TOKEN", None)
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"database": str(Path(directory) / "db.sqlite3")}), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "fin_harness.cli",
                    "import-tushare",
                    "600000.SH",
                    "20260630",
                    "--entity-id",
                    "cn.company.600000",
                    "--license-label",
                    "reviewed-local",
                    "--acknowledge-license",
                    "--config",
                    str(config),
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )
        self.assertEqual(69, completed.returncode)
        response = json.loads(completed.stdout)
        self.assertEqual("source_unavailable", response["error"]["code"])
        self.assertNotIn("token=", (completed.stdout + completed.stderr).lower())


if __name__ == "__main__":
    unittest.main()
