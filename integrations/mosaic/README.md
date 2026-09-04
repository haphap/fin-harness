# Mosaic integration

Mosaic owns agent identity, signed capabilities, stage allowlists, run scheduling,
and bundle materialization. Financial Harness owns only the financial request and
result contract.

The current Mosaic `tools.call` reads already-materialized, zero-argument bundle
values. Do not bypass it by accepting arbitrary model parameters. Instead, the
Mosaic controller calls this process before `prepare_capability`:

```text
fin-harness invoke --config /absolute/path/to/fin-harness/examples/config.json
```

Write one `fin-harness/v1` envelope to stdin and parse exactly one JSON line from
stdout. Map Mosaic's authenticated graph/run/node metadata only to controller
telemetry; map its signed `as_of` and approved target set into `request.as_of` and
`request.targets`. Materialize the returned keyed `results`, `run_id`,
`snapshot_id`, and hashes in the Mosaic bundle. The model receives the immutable
bundle value through Mosaic's existing `tools.call` path.

Before invoking, Mosaic must enforce:

- the stage allowlist includes `financial_analyze`;
- the signed capability's entity, targets, `as_of`, and expiry match the request;
- the tenant-specific database/config is selected outside model-controlled data;
- a non-`ok` result is materialized as a refusal, never converted to a number.

No Mosaic signature, nonce, tool policy, or tenant credential enters the generic
Financial Harness request schema.
