// Thin host stub, real adapter source and real CLI. Does not claim full Pi runtime acceptance.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const python = process.argv[2];
const temporary = await mkdtemp(join(tmpdir(), "fin-pi-contract-"));
process.env.FIN_HARNESS_BIN = join(dirname(python), "fin-harness");
process.env.FIN_HARNESS_DB = join(temporary, "test.sqlite3");
delete process.env.FIN_HARNESS_CONFIG;

// Only the schema constructors and registration API are stubbed; subprocess behavior is real.
globalThis.finHarnessTestType = {
  String: (options = {}) => ({ type: "string", ...options }),
  Literal: (value) => ({ const: value }),
  Union: (values) => ({ anyOf: values }),
  Array: (items, options = {}) => ({ type: "array", items, ...options }),
  Object: (properties) => ({ type: "object", properties }),
  Optional: (value) => value,
};
try {
  const source = (await readFile(join(root, "integrations/pi/fin-harness.ts"), "utf8"))
    .replace('import { Type } from "typebox";', "const Type = globalThis.finHarnessTestType;");
  const { default: register } = await import(
    `data:text/javascript,${encodeURIComponent(stripTypeScriptTypes(source, { mode: "strip" }))}`);
  const tools = new Map();
  register({ registerTool: (tool) => tools.set(tool.name, tool) });
  assert.equal(tools.size, 2);
  const analyze = tools.get("financial_analyze");
  const explain = tools.get("financial_explain");
  assert.match(analyze.description, /derived\.cashflow\.operating\.single_quarter_yoy/);
  assert.match(analyze.description, /public.*system/);
  execFileSync(python, ["-m", "fin_harness.cli", "import-fixture", join(root, "tests/fixtures/source-success.json")],
    { env: process.env });
  const envelope = JSON.parse(await readFile(join(root, "protocol/v1/fixtures/analyze-success.request.json"), "utf8"));
  const result = await analyze.execute("test", envelope.request); // Signal is optional.
  assert.equal(result.details.results[0].value, "0.3333");
  const evidence = await explain.execute("test", { run_id: result.details.run_id }, new AbortController().signal);
  assert.equal(evidence.details.results[0].inputs.length, 4);
  const invalid = await analyze.execute("test", { ...envelope.request, knowledge_policy: [] });
  assert.equal(invalid.details.error.code, "invalid_request"); // Preserved despite CLI exit 2.
  const aborted = new AbortController();
  aborted.abort();
  await assert.rejects(analyze.execute("test", envelope.request, aborted.signal), /cancelled/);
  const active = new AbortController();
  const pending = analyze.execute("test", envelope.request, active.signal);
  active.abort();
  await assert.rejects(pending, /cancelled/);
  process.env.FIN_HARNESS_BIN = join(temporary, "nonexistent");
  await assert.rejects(analyze.execute("test", envelope.request));
  console.log("Pi adapter contract: discovery, analyze/explain, typed errors, optional signal, cancellation, spawn failure passed");
} finally {
  await rm(temporary, { recursive: true, force: true });
  delete globalThis.finHarnessTestType;
}
