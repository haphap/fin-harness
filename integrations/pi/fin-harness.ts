import { spawn } from "node:child_process";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const MAX_BYTES = 1024 * 1024;

const target = Type.Object({
  metric_id: Type.String({ minLength: 1, maxLength: 128,
    description: "v0.1 supports derived.cashflow.operating.single_quarter_yoy (Q2 only)" }),
  period: Type.String({ pattern: "^[0-9]{4}Q[1-4]$" }),
  scope: Type.Union([Type.Literal("consolidated"), Type.Literal("parent")]),
});

export default function finHarness(pi: ExtensionAPI) {
  pi.registerTool({
    name: "financial_analyze",
    label: "Financial Analyze",
    description:
      "Calculate derived.cashflow.operating.single_quarter_yoy for consolidated Q2. Ask for entity, period and timezone-qualified as_of. public uses disclosure time; system uses local ingestion history. Never supply source values, formulas, SQL or credentials.",
    parameters: Type.Object({
      entity: Type.String({ minLength: 1, maxLength: 128 }),
      targets: Type.Array(target, { minItems: 1, maxItems: 16, uniqueItems: true }),
      as_of: Type.String({ format: "date-time" }),
      knowledge_policy: Type.Union([Type.Literal("system"), Type.Literal("public")]),
    }),
    async execute(_toolCallId, params, signal) {
      const response = await invoke("analyze", params, signal);
      return { content: [{ type: "text", text: JSON.stringify(response) }], details: response };
    },
  });

  pi.registerTool({
    name: "financial_explain",
    label: "Financial Explain",
    description: "Explain calculation steps and provenance for an existing fin-harness run_id.",
    parameters: Type.Object({
      run_id: Type.String({ minLength: 1, maxLength: 128 }),
      result_ids: Type.Optional(Type.Array(Type.String({ minLength: 1, maxLength: 128 }),
        { minItems: 1, maxItems: 16, uniqueItems: true })),
    }),
    async execute(_toolCallId, params, signal) {
      const response = await invoke("explain", params, signal);
      return { content: [{ type: "text", text: JSON.stringify(response) }], details: response };
    },
  });
}

function invoke(operation: "analyze" | "explain", request: unknown, signal?: AbortSignal): Promise<unknown> {
  if (signal?.aborted) return Promise.reject(new Error("fin-harness call cancelled"));
  const executable = process.env.FIN_HARNESS_BIN || "fin-harness";
  const args = ["invoke"];
  if (process.env.FIN_HARNESS_CONFIG) args.push("--config", process.env.FIN_HARNESS_CONFIG);
  const envelope = JSON.stringify({
    protocol: "fin-harness/v1",
    operation,
    request_id: `pi-${Date.now()}`,
    request,
    context: { client: "pi" },
  });

  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { stdio: ["pipe", "pipe", "ignore"] });
    const stdout: Buffer[] = [];
    let bytes = 0;
    let timedOut = false;
    let killTimer: ReturnType<typeof setTimeout> | undefined;
    const stop = () => {
      child.kill("SIGTERM");
      killTimer ??= setTimeout(() => child.kill("SIGKILL"), 1_000);
    };
    const timer = setTimeout(() => { timedOut = true; stop(); }, 60_000);
    signal?.addEventListener("abort", stop, { once: true });

    child.stdout.on("data", (chunk: Buffer) => {
      bytes += chunk.length;
      if (bytes > MAX_BYTES + 1) stop();
      else stdout.push(chunk);
    });
    child.on("error", reject);
    child.stdin.on("error", () => { stop(); reject(new Error("fin-harness input pipe failed")); });
    child.on("close", (code) => {
      clearTimeout(timer);
      clearTimeout(killTimer);
      signal?.removeEventListener("abort", stop);
      if (signal?.aborted) return reject(new Error("fin-harness call cancelled"));
      if (timedOut) return reject(new Error("fin-harness call timed out"));
      if (bytes > MAX_BYTES + 1) return reject(new Error("fin-harness response exceeded 1 MiB"));
      const lines = Buffer.concat(stdout).toString("utf8").trim().split(/\r?\n/);
      if (lines.length !== 1) return reject(new Error("fin-harness invocation failed"));
      try {
        const response = JSON.parse(lines[0]);
        if (response?.protocol !== "fin-harness/v1" || !Array.isArray(response.results)
            || !["ok", "partial", "rejected", "error"].includes(response.status)
            || (response.status === "error" && typeof response.error?.code !== "string")
            || (code !== 0 && response.status !== "error")) {
          return reject(new Error("fin-harness returned an invalid response envelope"));
        }
        // Non-zero CLI exits can still carry valid, actionable protocol errors.
        resolve(response);
      } catch {
        reject(new Error("fin-harness returned invalid JSON"));
      }
    });
    child.stdin.end(envelope);
  });
}
