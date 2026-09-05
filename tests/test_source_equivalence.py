"""Synthetic regressions for the real-host update_flag duplicate failure."""
import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from fin_harness.core import Engine
from fin_harness.mcp_adapter import create_mcp_server
from fin_harness.protocol import load_schema, sha256_json
from fin_harness.store import Store
from fin_harness.tushare_source import DEFAULT_FIELDS, TushareBatch, batch_to_source_fixture
from test_vertical_slice import ROOT, SUCCESS_REQUEST, SUCCESS_SOURCE, load


def source_document(*, duplicates=True):
    rows = [{k: v for k, v in r["raw"].items() if k != "is_calc"}
            for r in load(SUCCESS_SOURCE)["records"] if r["source_record_id"] != "src_current_h1_v2"]
    if duplicates:
        rows += [dict(row, update_flag="1") for row in rows]
    digest = sha256_json(rows)
    return batch_to_source_fixture(
        TushareBatch(tuple(DEFAULT_FIELDS.split(",")), tuple(rows), digest, digest),
        entity_id="cn.company.test001", aliases=["TEST001.CN"],
        ingested_at="2026-08-09T08:00:00+08:00",
        license_info={"label": "synthetic-equivalence-test", "redistribution": True},
    )


class SourceEquivalenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fin-equivalence-")
        self.db = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.db)
        self.engine = Engine(self.store)
        self.request = load(SUCCESS_REQUEST)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def explain(self, response, **request):
        return self.engine.handle({"protocol": "fin-harness/v1", "operation": "explain",
            "request_id": "explain-equivalence", "request": {"run_id": response["run_id"], **request}})

    def test_duplicates_preserve_all_evidence_and_replay(self):
        self.assertEqual(8, self.store.import_document(source_document())["records"])
        response = self.engine.handle(self.request)
        self.assertEqual("ok", response["status"])
        result = response["results"][0]
        self.assertEqual("0.3333", result["value"])
        self.assertEqual(["equivalent_source_records"], result["warnings"])
        explanation = self.explain(response)
        inputs = explanation["results"][0]["inputs"]
        evidence = []
        for item in inputs:
            self.assertEqual(1, len(item["equivalent_sources"]))
            evidence.extend([item, *item["equivalent_sources"]])
            flags = {source["source"]["locator"]["row_key"]["update_flag"]
                     for source in [item, *item["equivalent_sources"]]}
            self.assertEqual({"0", "1"}, flags)
        self.assertEqual(8, len({item["source"]["source_record_id"] for item in evidence}))
        snapshot = self.store.get_snapshot(result["snapshot_id"], "local")["manifest"]
        self.assertTrue(all("equivalent_observations" in item for item in snapshot["inputs"]))
        self.assertTrue(self.engine.replay(response["run_id"])["match"])
        self.assertEqual(8, self.store.connection.execute("SELECT count(*) FROM source_records").fetchone()[0])
        for name, value in (("analyze", response), ("explain", explanation)):
            Draft202012Validator(load_schema(name + "-response")).validate(value)

    def test_equivalence_is_independent_of_import_order(self):
        document = source_document()
        self.store.import_document(document)
        expected = self.engine.handle(self.request)["results"]
        document["records"].reverse()
        with Store(":memory:") as other:
            other.import_document(document)
            self.assertEqual(expected, Engine(other).handle(self.request)["results"])

    def test_financial_dimensions_and_unknown_flags_do_not_merge(self):
        changes = [
            ("raw", "n_cashflow_act", "131.000000"), ("raw", "report_type", "4"),
            ("raw", "ann_date", "20260807"), ("raw", "f_ann_date", "20260807"),
            ("raw", "comp_type", "2"), ("raw", "end_type", "1"),
            ("raw", "ts_code", "TEST002.CN"), ("raw", "update_flag", "unknown"),
            ("observation", "value", "131.000000"), ("observation", "currency", "USD"),
            ("observation", "unit", "thousand-CNY"), ("observation", "record_status", "withdrawn"),
            ("observation", "period_start", "2025-01-01"),
            ("observation", "published_at", "2026-08-08T00:00:00+08:00"),
            (None, "provider", "another-provider"),
        ]
        for section, field, value in changes:
            with self.subTest(section=section, field=field), Store(":memory:") as store:
                document = source_document()
                record = document["records"][4]
                (record if section is None else record[section])[field] = value
                store.import_document(document)
                result = Engine(store).handle(self.request)["results"][0]
                self.assertEqual("ambiguous_source_version", result["status"])
                self.assertNotIn("value", result)

    def test_pit_limits_equivalent_evidence_and_preserves_old_snapshot(self):
        document = source_document()
        for record in document["records"][4:]:
            record["observation"]["ingested_at"] = "2026-08-20T00:00:00Z"
        self.store.import_document(document)
        before = self.engine.handle(self.request)
        self.assertEqual("ok", before["status"])
        self.assertEqual([], before["results"][0]["warnings"])
        self.assertTrue(all("equivalent_sources" not in i for i in self.explain(before)["results"][0]["inputs"]))
        self.request["request"]["as_of"] = "2026-08-21T00:00:00Z"
        after = self.engine.handle(self.request)
        self.assertEqual(["equivalent_source_records"], after["results"][0]["warnings"])
        self.assertNotEqual(before["results"][0]["snapshot_id"], after["results"][0]["snapshot_id"])
        self.assertTrue(self.engine.replay(before["run_id"])["match"])
        self.request["request"].update(as_of="2026-08-15T00:00:00Z", knowledge_policy="public")
        public = self.engine.handle(self.request)
        self.assertEqual("ok", public["status"])
        self.assertEqual(["equivalent_source_records"], public["results"][0]["warnings"])

    def test_equivalent_evidence_is_verified_at_analysis_and_replay(self):
        document = source_document()
        self.store.import_document(document)
        response = self.engine.handle(self.request)
        snapshot = self.store.get_snapshot(response["results"][0]["snapshot_id"], "local")
        duplicate_id = snapshot["manifest"]["inputs"][0]["equivalent_observations"][0]["observation_id"]
        original = self.store.get_observation

        def corrupted(observation_id):
            item = original(observation_id)
            if observation_id == duplicate_id:
                item["raw"]["update_flag"] = "0" if item["raw"]["update_flag"] == "1" else "1"
            return item

        with patch.object(self.store, "get_observation", side_effect=corrupted):
            self.assertEqual("validation_failed", self.engine.handle(self.request)["results"][0]["status"])
            self.assertEqual("replay_artifact_mismatch", self.explain(response)["error"]["code"])
            self.assertEqual("replay_artifact_mismatch", self.engine.replay(response["run_id"])["error"]["code"])

    def test_rejection_explains_frozen_candidates_not_later_rows(self):
        document = source_document()
        document["records"][4]["raw"]["n_cashflow_act"] = "131.000000"
        document["records"][4]["observation"]["value"] = "131.000000"
        self.store.import_document(document)
        response = self.engine.handle(self.request)
        result = response["results"][0]
        details = result["error"]["details"]
        self.assertEqual("2026H1", details["period"])
        self.assertEqual(2, details["candidate_count"])
        self.assertEqual({r["observation"]["observation_id"] for r in (document["records"][0], document["records"][4])},
                         {r["observation_id"] for r in details["candidates"]})
        later = copy.deepcopy(document["records"][0])
        later["source_record_id"] = "src_later"
        later["observation"].update(observation_id="obs_later", ingested_at="2026-08-30T00:00:00Z")
        self.store.import_document({**document, "records": [later]})
        self.assertEqual(details, self.engine.handle(self.request)["results"][0]["error"]["details"])
        explained = self.explain(response, result_ids=[result["result_id"]])
        self.assertEqual([result], explained["results"])
        Draft202012Validator(load_schema("explain-response")).validate(explained)
        self.assertEqual([], self.explain(response, result_ids=["unknown-result"])["results"])

    def test_rejected_explain_is_delivered_by_cli_and_mcp(self):
        self.store.import_document(source_document())
        self.request["request"]["as_of"] = "2026-01-01T00:00:00Z"
        response = self.engine.handle(self.request)
        self.assertEqual("insufficient_data", response["results"][0]["status"])
        self.assertEqual(4, len(response["results"][0]["error"]["details"]["missing_roles"]))
        expected = self.explain(response)
        envelope = {"protocol": "fin-harness/v1", "operation": "explain", "request_id": "cli-rejection",
                    "request": {"run_id": response["run_id"]}}
        proc = subprocess.run([sys.executable, "-m", "fin_harness.cli", "invoke"], input=json.dumps(envelope),
            capture_output=True, text=True, cwd=ROOT, env={**os.environ, "FIN_HARNESS_DB": str(self.db)}, timeout=10)
        self.assertEqual(0, proc.returncode)
        self.assertEqual(expected["results"], json.loads(proc.stdout)["results"])

        async def check_mcp():
            from mcp import Client
            async with Client(create_mcp_server(self.db)) as client:
                reply = await client.call_tool("financial_explain", {"run_id": response["run_id"]})
                self.assertEqual(expected["results"], reply.structured_content["results"])
                Draft202012Validator(load_schema("explain-response")).validate(reply.structured_content)

        asyncio.run(check_mcp())

    def test_rejection_candidates_are_bounded_and_partial_explain_is_keyed(self):
        document = source_document(duplicates=False)
        for index in range(20):
            other = copy.deepcopy(document["records"][0])
            other["source_record_id"] = f"src_competing_{index}"
            other["observation"].update(observation_id=f"obs_competing_{index}", value=str(131 + index))
            other["raw"]["n_cashflow_act"] = str(131 + index)
            document["records"].append(other)
        self.store.import_document(document)
        rejected = self.engine.handle(self.request)
        details = rejected["results"][0]["error"]["details"]
        self.assertEqual(21, details["candidate_count"])
        self.assertEqual(16, len(details["candidates"]))
        self.assertTrue(details["truncated"])
        self.assertEqual(rejected["results"], self.explain(rejected)["results"])
        with Store(":memory:") as store:
            store.import_document(source_document())
            engine = Engine(store)
            self.request["request"]["targets"].append(dict(self.request["request"]["targets"][0], period="2026Q3"))
            response = engine.handle(self.request)
            self.assertEqual("partial", response["status"])
            explained = engine.handle({"protocol": "fin-harness/v1", "operation": "explain", "request_id": "partial",
                                       "request": {"run_id": response["run_id"]}})
            self.assertEqual([r["result_id"] for r in response["results"]],
                             [r["result_id"] for r in explained["results"]])
            self.assertEqual("not_applicable", explained["results"][1]["status"])
            Draft202012Validator(load_schema("explain-response")).validate(explained)

    def test_equal_values_are_not_equivalent_across_report_types(self):
        document = source_document()
        adjusted = document["records"][4]
        adjusted["raw"]["report_type"] = "4"
        adjusted["observation"]["source_dimensions"]["report_type"] = "4"
        adjusted["observation"]["reporting_variant"] = "adjusted"
        self.store.import_document(document)
        self.assertEqual("ambiguous_source_version", self.engine.handle(self.request)["results"][0]["status"])


if __name__ == "__main__":
    unittest.main()
