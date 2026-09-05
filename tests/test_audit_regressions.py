from __future__ import annotations

import asyncio
import copy
import json
import os
import select
import signal
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

from fin_harness.core import Engine
from fin_harness.mcp_adapter import create_mcp_server
from fin_harness.protocol import ExecutionControl, ProtocolError, load_schema, validate_envelope
from fin_harness.store import Store
from test_vertical_slice import ROOT, SUCCESS_REQUEST, SUCCESS_SOURCE, load


class AuditRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fin-audit-test-")
        self.db = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.db)
        self.request = load(SUCCESS_REQUEST)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def invoke(self, envelope):
        proc = subprocess.run([sys.executable, "-m", "fin_harness.cli", "invoke"],
            input=json.dumps(envelope), text=True, capture_output=True, cwd=ROOT, timeout=10,
            env={**os.environ, "FIN_HARNESS_DB": str(self.db)})
        self.assertEqual(1, len(proc.stdout.splitlines()))
        return proc.returncode, json.loads(proc.stdout)

    def counts(self):
        return [self.store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("runs", "snapshots", "audit_events")]

    def test_pit_fractional_seconds_both_policies_and_directions(self):
        for policy in ("public", "system"):
            for record_time, as_of, expected in (
                ("2026-08-24T00:00:00.500000Z", "2026-08-24T00:00:00Z", "insufficient_data"),
                ("2026-08-24T00:00:00Z", "2026-08-24T00:00:00.500000Z", "ok"),
            ):
                with self.subTest(policy=policy, record_time=record_time), Store(":memory:") as store:
                    document = load(SUCCESS_SOURCE)
                    document["records"] = [r for r in document["records"] if r["observation"]["observation_id"] != "obs_current_h1_v2"]
                    document["records"][0]["observation"].update(
                        published_at=record_time, ingested_at=record_time, source_time_precision="instant")
                    store.import_document(document)
                    self.request["request"].update(as_of=as_of, knowledge_policy=policy)
                    result = Engine(store).handle(self.request)["results"][0]
                    self.assertEqual(expected, result["status"])

    def test_actual_period_and_cross_family_revision_fail_closed(self):
        for field, value in (("period_end", "2026-04-30"), ("period_start", "2025-01-01")):
            with self.subTest(field=field), Store(":memory:") as store:
                document = load(SUCCESS_SOURCE)
                document["records"][2]["observation"][field] = value
                store.import_document(document)
                result = Engine(store).handle(self.request)["results"][0]
                self.assertEqual("validation_failed", result["status"])
                self.assertNotIn("value", result)
        document = load(SUCCESS_SOURCE)
        document["records"][1]["observation"]["period_end"] = "2026-07-31"
        with self.assertRaisesRegex(ValueError, "same fact family"):
            self.store.import_document(document)

    def test_registry_must_match_audited_function(self):
        metric = load(ROOT / "registry/metrics/operating_cashflow_q2_yoy.json")
        for field, value in (("calculation", "unknown"), ("decimal", {"output_scale": 2}), ("inputs", [])):
            modified = dict(metric, **{field: value})
            with self.subTest(field=field), patch.object(Path, "read_text", return_value=json.dumps(modified)):
                with self.assertRaises(ProtocolError) as caught:
                    Engine(self.store)
                self.assertEqual("replay_artifact_mismatch", caught.exception.code)

    def test_unknown_database_version_is_not_downgraded(self):
        self.store.connection.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(ValueError, "unsupported database schema"):
            Store(self.db)
        self.assertEqual(99, self.store.connection.execute("PRAGMA user_version").fetchone()[0])

    def test_revision_cycles_are_rejected(self):
        document = load(SUCCESS_SOURCE)
        for record in document["records"][:2]:
            record["observation"].update(supersedes_observation_id=None, source_time_precision="instant",
                published_at="2026-08-24T00:00:00Z", ingested_at="2026-08-24T00:00:00Z")
        self.store.import_document(document)
        self.store.link_revision("obs_current_h1_v2", "obs_current_h1_v1", reviewer="test", reason="verified")
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.store.link_revision("obs_current_h1_v1", "obs_current_h1_v2", reviewer="test", reason="invalid cycle")

    def test_invalid_requests_agree_with_schema_and_cli(self):
        cases = []
        for field, value in (("operation", []), ("context", None)):
            cases.append(dict(self.request, **{field: value}))
        for field, value in (("knowledge_policy", {}), ("as_of", "2026-08-15 12:00:00+08:00"),
                             ("as_of", "2026-08-15T12:00:00.1234567Z")):
            item = copy.deepcopy(self.request)
            item["request"][field] = value
            cases.append(item)
        for value in ([{}], None, ["x" * 129], ["same", "same"]):
            cases.append({"protocol": "fin-harness/v1", "operation": "explain", "request_id": "bad-id",
                          "request": {"run_id": "test", "result_ids": value}})
        for field, value in (("scope", []), ("metric_id", "x" * 129), ("period", "２０２６Q2")):
            item = copy.deepcopy(self.request)
            item["request"]["targets"][0][field] = value
            cases.append(item)
        validator = Draft202012Validator(load_schema("request"), format_checker=FormatChecker())
        for envelope in cases:
            with self.subTest(envelope=envelope):
                self.assertFalse(validator.is_valid(envelope))
                with self.assertRaises(ProtocolError):
                    validate_envelope(envelope)
                code, result = self.invoke(envelope)
                self.assertEqual(2, code)
                self.assertEqual("invalid_request", result["error"]["code"])
                self.assertEqual(envelope["request_id"], result["request_id"])
        self.assertEqual([0, 0, 0], self.counts())

    def test_replay_verifies_content_not_just_saved_hash_fields(self):
        self.store.import_fixture(SUCCESS_SOURCE)
        engine = Engine(self.store)
        run_id = engine.handle(self.request)["run_id"]
        read_run = self.store.get_run
        def corrupted_run(*args):
            run = read_run(*args)
            run["request"]["request"]["entity"] = "changed"
            return run
        with patch.object(self.store, "get_run", side_effect=corrupted_run):
            self.assertEqual("replay_artifact_mismatch", engine.replay(run_id)["error"]["code"])
        read_observation = self.store.get_observation
        for field, value in (("period_end", "1900-01-01"), ("raw_hash", "sha256:" + "0" * 64),
                             ("raw", {}), ("locator", {"changed": True}), ("value_text", "999")):
            def corrupted_observation(*args):
                return dict(read_observation(*args), **{field: value})
            with self.subTest(field=field), patch.object(self.store, "get_observation", side_effect=corrupted_observation):
                self.assertEqual("replay_artifact_mismatch", engine.replay(run_id)["error"]["code"])
        self.assertTrue(engine.replay(run_id)["match"])

    def test_revision_link_is_append_only_and_system_point_in_time(self):
        document = load(SUCCESS_SOURCE)
        document["records"][1]["observation"]["supersedes_observation_id"] = None
        self.store.import_document(document)
        self.request["request"]["as_of"] = "2026-08-24T00:00:00Z"
        engine = Engine(self.store)
        self.assertEqual("ambiguous_source_version", engine.handle(self.request)["results"][0]["status"])
        changed = copy.deepcopy(document)
        changed["records"][1]["observation"]["supersedes_observation_id"] = "obs_current_h1_v1"
        with self.assertRaisesRegex(ValueError, "link-revision"):
            self.store.import_document(changed)
        with patch("fin_harness.store.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 25, tzinfo=timezone.utc)
            self.store.link_revision("obs_current_h1_v2", "obs_current_h1_v1", reviewer="test", reason="verified amendment")
        self.assertEqual("ambiguous_source_version", engine.handle(self.request)["results"][0]["status"])
        self.request["request"]["knowledge_policy"] = "public"
        self.assertEqual("ok", engine.handle(self.request)["status"])
        self.request["request"].update(as_of="2026-08-25T00:00:00Z", knowledge_policy="system")
        self.assertEqual("ok", engine.handle(self.request)["status"])
        self.assertIsNone(self.store.get_observation("obs_current_h1_v2")["supersedes_observation_id"])
        for table in ("source_payloads", "revision_links"):
            for sql in (f"DELETE FROM {table}", f"UPDATE {table} SET rowid=rowid"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    self.store.connection.execute(sql)

    def test_idempotent_reimport_preserves_first_ingestion_but_rejects_value_changes(self):
        document = load(SUCCESS_SOURCE)
        self.store.import_document(document)
        old = self.store.get_observation("obs_current_q1")
        for record in document["records"]:
            record["observation"]["ingested_at"] = "2026-09-01T00:00:00Z"
        self.assertEqual(0, self.store.import_document(document)["records"])
        self.assertEqual(old, self.store.get_observation("obs_current_q1"))
        document["records"][2]["observation"]["value"] = "999"
        with self.assertRaisesRegex(ValueError, "content mismatch"):
            self.store.import_document(document)

    def test_legacy_upgrade_is_additive_and_requires_original_evidence(self):
        self.store.import_fixture(SUCCESS_SOURCE)
        before = [tuple(r) for r in self.store.connection.execute("SELECT * FROM observations")]
        # Only this disposable test DB: simulate the v1 layout, never migrate by rewriting facts.
        self.store.connection.execute("DROP TABLE source_payloads")
        self.store.connection.execute("DROP TABLE revision_links")
        self.store.connection.execute("PRAGMA user_version=1")
        self.store.close()
        self.store = Store(self.db)
        self.assertEqual(before, [tuple(r) for r in self.store.connection.execute("SELECT * FROM observations")])
        self.assertEqual("validation_failed", Engine(self.store).handle(self.request)["results"][0]["status"])
        self.store.import_fixture(SUCCESS_SOURCE)
        self.assertEqual("ok", Engine(self.store).handle(self.request)["status"])
        self.assertEqual(before, [tuple(r) for r in self.store.connection.execute("SELECT * FROM observations")])

    def test_deadline_and_commit_failure_leave_no_authority_rows(self):
        self.store.import_fixture(SUCCESS_SOURCE)
        control = ExecutionControl()
        control.deadline = time.monotonic() - 1
        self.assertEqual("timeout", Engine(self.store, control=control).handle(self.request)["error"]["code"])
        with patch.object(ExecutionControl, "commit", side_effect=ProtocolError("timeout", "test deadline")):
            self.assertEqual("timeout", Engine(self.store).handle(self.request)["error"]["code"])
        with patch("fin_harness.protocol.MAX_RESPONSE_BYTES", 200):
            self.assertEqual("response_too_large", Engine(self.store).handle(self.request)["error"]["code"])
        self.assertEqual([0, 0, 0], self.counts())

    def test_mcp_schema_invalid_arguments_and_large_explain_match_cli(self):
        document = load(SUCCESS_SOURCE)
        for record in document["records"]:
            record["locator"]["note"] = "x" * (1024 * 1024)
        self.store.import_document(document)
        async def exercise():
            from mcp import Client
            async with Client(create_mcp_server(self.db)) as client:
                for tool in (await client.list_tools()).tools:
                    operation = tool.name.removeprefix("financial_")
                    self.assertEqual(load_schema(f"{operation}-response"), tool.output_schema)
                invalid = copy.deepcopy(self.request["request"])
                invalid["knowledge_policy"] = []
                reply = await client.call_tool("financial_analyze", invalid)
                self.assertTrue(reply.is_error)
                self.assertEqual("invalid_request", reply.structured_content["error"]["code"])
                reply = await client.call_tool("financial_analyze", self.request["request"])
                run_id = reply.structured_content["run_id"]
                reply = await client.call_tool("financial_explain", {"run_id": run_id})
                response = reply.structured_content
                self.assertTrue(reply.is_error)
                self.assertEqual("response_too_large", response["error"]["code"])
                Draft202012Validator(load_schema("explain-response")).validate(response)
                envelope = {"protocol": "fin-harness/v1", "operation": "explain", "request_id": "large-response",
                            "request": {"run_id": run_id}}
                code, response = self.invoke(envelope)
                self.assertEqual(70, code)
                self.assertEqual("response_too_large", response["error"]["code"])
                self.assertEqual("large-response", response["request_id"])
        asyncio.run(exercise())

    def test_mcp_cancel_before_commit_cannot_publish_a_run(self):
        self.store.import_fixture(SUCCESS_SOURCE)
        entered, release, finished, cancelled = (threading.Event() for _ in range(4))
        persist, cancel = Store.persist_run, ExecutionControl.cancel
        def gated(store, **kwargs):
            entered.set()
            release.wait(3)
            try:
                return persist(store, **kwargs)
            finally:
                finished.set()
        def observed_cancel(control):
            cancel(control)
            cancelled.set()
        async def exercise():
            from mcp import Client
            async with Client(create_mcp_server(self.db)) as client:
                task = asyncio.create_task(client.call_tool("financial_analyze", self.request["request"]))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertTrue(await asyncio.to_thread(cancelled.wait, 3))
                finally:
                    release.set()
                    await asyncio.to_thread(finished.wait, 3)
        with patch.object(Store, "persist_run", gated), patch.object(ExecutionControl, "cancel", observed_cancel):
            asyncio.run(exercise())
        self.assertTrue(finished.is_set())
        self.assertEqual([0, 0, 0], self.counts())

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the Pi adapter contract check")
    def test_pi_adapter_against_real_cli(self):
        subprocess.run(["node", str(ROOT / "tests/test_pi_adapter.mjs"), sys.executable],
                       check=True, capture_output=True, text=True, cwd=ROOT, timeout=15)

    @unittest.skipUnless(hasattr(signal, "SIGALRM"), "POSIX CLI deadline check")
    def test_cli_deadline_covers_unfinished_stdin(self):
        process = subprocess.Popen([sys.executable, "-c",
            "from fin_harness import cli; cli.DEFAULT_TIMEOUT_SECONDS=0.05; raise SystemExit(cli.main(['invoke']))"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT,
            env={**os.environ, "FIN_HARNESS_DB": str(self.db)})
        try:
            self.assertTrue(select.select([process.stdout], [], [], 3)[0], "CLI did not enforce its deadline")
            response = json.loads(process.stdout.readline())
            self.assertEqual("timeout", response["error"]["code"])
            self.assertEqual(70, process.wait(timeout=3))
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        self.assertEqual([0, 0, 0], self.counts())

    def test_mcp_deadline_cancels_worker_before_commit(self):
        self.store.import_fixture(SUCCESS_SOURCE)
        entered, release, finished = (threading.Event() for _ in range(3))
        persist = Store.persist_run
        def gated(store, **kwargs):
            entered.set()
            release.wait(3)
            try:
                return persist(store, **kwargs)
            finally:
                finished.set()
        async def exercise():
            from mcp import Client
            async with Client(create_mcp_server(self.db)) as client:
                try:
                    response = await asyncio.wait_for(client.call_tool("financial_analyze", self.request["request"]), 3)
                    self.assertEqual("timeout", response.structured_content["error"]["code"])
                finally:
                    release.set()
                    await asyncio.to_thread(finished.wait, 3)
        with patch.object(Store, "persist_run", gated), patch("fin_harness.mcp_adapter.DEFAULT_TIMEOUT_SECONDS", 0.1):
            asyncio.run(exercise())
        self.assertTrue(entered.is_set())
        self.assertTrue(finished.is_set())
        self.assertEqual([0, 0, 0], self.counts())


if __name__ == "__main__":
    unittest.main()
