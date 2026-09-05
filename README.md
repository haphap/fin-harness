# fin-harness

`fin-harness` is a small, deterministic financial-analysis boundary for agents.
It accepts identifiers and time semantics, selects point-in-time facts from an
append-only SQLite store, calculates with `Decimal`, and returns keyed results
with snapshots and provenance. It does not accept model-supplied source values,
formulas, SQL, or credentials.

The v0.1 vertical slice supports one China A-share/CAS metric:
`derived.cashflow.operating.single_quarter_yoy` for consolidated Q2 operating
cash flow. The same core is available through a one-shot JSON CLI and MCP stdio.

## Install and verify

Python 3.11+ and `uv` are required for the development workflow:

```bash
uv sync --extra mcp --dev
uv run python -m unittest discover -s tests -v
```

The base package has no runtime dependency. The optional `mcp` extra installs
the official MCP Python SDK v2.

## Run the synthetic slice

```bash
uv run fin-harness import-fixture tests/fixtures/source-success.json --config examples/config.json
uv run fin-harness invoke --config examples/config.json < protocol/v1/fixtures/analyze-success.request.json
uv run fin-harness doctor --config examples/config.json --json
uv run fin-harness capabilities --config examples/config.json --json
```

Copy the returned `run_id` into:

```bash
uv run fin-harness explain RUN_ID --config examples/config.json --json
uv run fin-harness replay RUN_ID --config examples/config.json --json
```

`invoke` reads one UTF-8 JSON envelope and writes exactly one compact JSON line.
The public v1 schemas live in `protocol/v1`; Python and SQLite types are not part
of the compatibility contract.

## Agent and ChatGPT connections

Start the local MCP server with:

```bash
uv run --extra mcp fin-harness mcp --config examples/config.json
```

Ready-to-edit host examples are in `examples/hosts`:

- `chatgpt-codex.toml`: ChatGPT desktop, Codex CLI, and the Codex IDE extension;
- `opencode.jsonc`: OpenCode v2 local MCP;
- `deepseek.cordis.patch.yml`: DeepSeek Harness's official MCP client;
- `integrations/pi/fin-harness.ts`: Pi extension using the one-shot CLI;
- `integrations/mosaic/README.md`: capability-safe Mosaic materialization flow.

Replace `/absolute/path/to/fin-harness` in examples. For ChatGPT desktop, copy
the TOML table into `~/.codex/config.toml`, restart the app, and use `/mcp` to
confirm that exactly `financial_analyze` and `financial_explain` are visible.

The local test-only HTTP profile is available at `http://127.0.0.1:8000/mcp`:

```bash
uv run --extra mcp fin-harness mcp --transport streamable-http --host 127.0.0.1 --port 8000 --config examples/config.json
```

It deliberately refuses non-loopback binding because it has no authentication.
ChatGPT web does not read local MCP configuration; web use requires a stable
public HTTPS deployment, OAuth/tenant authorization, rate limiting, and an
approved Tushare service/redistribution license. Those production assets are not
claimed by this repository.

## Controlled Tushare import

The provider path uses the fixed HTTPS endpoint directly, hashes raw response
bytes, parses JSON numbers as `Decimal` before binary float conversion, and fails
closed on unknown report dimensions. It never logs or snapshots the token.

After confirming your account's local storage rights, set `TUSHARE_TOKEN` in the
process environment and import one period:

```bash
uv run fin-harness import-tushare 600000.SH 20260630 \
  --entity-id cn.company.600000 \
  --license-label YOUR_REVIEWED_LICENSE_LABEL \
  --acknowledge-license \
  --config examples/config.json
```

This command is intentionally operator-only. It never appears as an MCP tool.
Multiple competing report versions remain ambiguous unless an explicit
`supersedes_observation_id` relationship is reviewed and imported. If both versions
are already stored, use the operator-only command (never a model tool):

```bash
fin-harness link-revision OBS_NEW OBS_OLD --reviewer YOUR_NAME --reason "verified amended filing" --config examples/config.json
```

This appends an immutable review record; it never edits either observation.
`system` applies the relationship only from the actual review time. `public`
may use the reviewed relationship for already-disclosed versions; this is public
history reconstruction, not a claim that the local system knew the relationship then.
Reimporting the same raw row preserves its first ingestion time. Conflicting
observation values or supersedes fields are rejected instead of silently ignored.

### Existing databases

Opening a v1 database adds evidence/revision tables without rewriting historical
facts, snapshots or runs. Legacy rows missing the original import payload are
refused until their original fixture is reimported to append verifiable evidence.
If that original evidence is unavailable, use a new database and an explicitly
new import; do not backdate it. Old runs still require their original code/registry
artifacts for replay. Back up the database before upgrading; do not use an older
binary to write an upgraded database.

The registry path may select another copy of the audited v1 definition, not an
arbitrary formula configuration. Different inputs, precision or formulas require
an implementation change and a newly reviewed definition.

## Scope and safety

- Research and evaluation only; no trading or portfolio mutation tools.
- `public` replay uses the conservative public-effective time; `system` replay
  uses the actual local `ingested_at` history.
- Missing, ambiguous, mismatched, or zero-denominator inputs return a structured
  refusal without a numeric value.
- Source records, observations, snapshots, runs, and audit events are append-only
  in SQLite. Replay verifies source, snapshot, formula, and build hashes.
- Project code is licensed under Apache License 2.0. Copyright 2026
  fin-harness contributors. Tushare data rights are separate and must be
  reviewed before multi-user service or redistribution.

See `DESIGN.md` for architectural decisions, `PROTOCOL.md` for the public wire
contract and host mapping, and `EVALUATION.md` for release-gate evidence and
the exact boundaries of the v0.1 claim.

## License

Apache License 2.0. See `LICENSE`. This license covers the fin-harness code and
documentation; it does not grant rights to Tushare or other provider data.
