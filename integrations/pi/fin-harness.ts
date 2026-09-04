import { spawn } from "node:child_process";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const MAX_BYTES = 1024 * 1024;

const target = Type.Object({
  metric_id: Type.String(),
  period: Type.String({ pattern: "^[0-9]{4}Q[1-4]$" }),
  scope: Type.Union([Type.Literal("consolidated"), Type.Literal("parent")]),
});

export default function finHarness(pi: ExtensionAPI) {
  pi.registerTool({
    name: "financial_analyze",
    label: "Financial Analyze",
    description:
      "Calculate an auditable point-in-time financial metric. Provide identifiers and time semantics, never source values or formulas.",
    parameters: Type.Object({
      entity: Type.String(),
      targets: Type.Array(target, { minItems: 1, maxItems: 16 }),
      as_of: Type.String(),
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
      run_id: Type.String(),
      result_ids: Type.Optional(Type.Array(Type.String(), { minItems: 1, maxItems: 16 })),
    }),
    async execute(_toolCallId, params, signal) {
      const response = await invoke("explain", params, signal);
      return { content: [{ type: "text", text: JSON.stringify(response) }], details: response };
    },
  });
}

function invoke(operation: "analyze" | "explain", request: unknown, signal: AbortSignal): Promise<unknown> {
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
    const timer = setTimeout(() => child.kill("SIGTERM"), 60_000);
    const abort = () => child.kill("SIGTERM");
    signal.addEventListener("abort", abort, { once: true });

    child.stdout.on("data", (chunk: Buffer) => {
      bytes += chunk.length;
      if (bytes > MAX_BYTES) child.kill("SIGTERM");
      else stdout.push(chunk);
    });
    child.on("error", reject);
    child.on("close", (code) => {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      if (signal.aborted) return reject(new Error("fin-harness call cancelled"));
      if (bytes > MAX_BYTES) return reject(new Error("fin-harness response exceeded 1 MiB"));
      const lines = Buffer.concat(stdout).toString("utf8").trim().split(/\r?\n/);
      if (code !== 0 || lines.length !== 1) return reject(new Error("fin-harness invocation failed"));
      try {
        resolve(JSON.parse(lines[0]));
      } catch {
        reject(new Error("fin-harness returned invalid JSON"));
      }
    });
    child.stdin.end(envelope);
  });
}
