# Financial Harness 宿主无关协议

状态：已实现的 v1 公共契约；远程生产入口仍为规划
日期：2026-09-05

实现基线：已合并的 [PR #1](https://github.com/haphap/fin-harness/pull/1)，含等价来源和拒算 explain 修复。操作步骤见[使用指南](docs/usage.md)和[宿主接入](docs/integrations.md)；验证与已知缺陷见 [EVALUATION.md](EVALUATION.md)。下文“必须”描述契约要求，不代表已通过任意输入或所有宿主的生产验收。

## 1. 目标

Financial Harness 以**外部可执行插件**而不是某个 Agent SDK 内的代码库形式发布。宿主不导入 Python 类型，也不理解指标计算、PIT 或审计表；它们只依赖版本化 JSON Schema 和两个稳定入口：

```text
任意宿主 ── one-shot JSON ──> fin-harness invoke ──> Core
本地 MCP 宿主 ── stdio ─────> fin-harness mcp ─────> Core
本机 MCP 测试 ── loopback HTTP ─> fin-harness mcp ─> Core
ChatGPT web ── 远程认证/插件部署（尚未交付）
```

`fin-harness invoke` 是最低公分母和合约测试入口；`fin-harness mcp` 是 OpenCode、DeepSeek Harness、ChatGPT 等 MCP 客户端的标准入口。两者调用同一个 core，不通过网络互相转发。操作者可用同一 `--config PATH` 启动参数选择数据目录与非敏感配置；凭据只从宿主受控环境/secret store 注入，不进入请求。

本地 MVP 不提供自定义长期 JSON-RPC server 或宿主 SDK。MCP 默认 stdio，另有同核 loopback Streamable HTTP 测试模式；公开部署所需认证、租户映射与插件包装尚未交付。analyze/explain 只读已导入的金融事实并写本地运行/审计，不在调用期间查询 Tushare。

## 2. 稳定边界

稳定公共面只有：

- `protocol/v1/*.schema.json`；
- `fin-harness {invoke,mcp} [--config PATH]` 的 framing、退出语义和操作名；
- MCP 工具 `financial_analyze`、`financial_explain`；
- Decimal 字符串、状态枚举和 ID/哈希语义。

Python 模块、SQLite schema、指标实现函数和审计表均为内部细节，可以在不破坏 v1 的情况下变化。

### 2.1 版本规则

- envelope 固定携带 `"protocol": "fin-harness/v1"`；
- v1 可增加可选输入字段和输出字段；调用方必须忽略不认识的输出字段；
- 删除字段、改变字段含义或收紧既有合法输入时发布 v2；
- server 不支持请求版本时返回 `unsupported_protocol`，不得猜测降级；
- 每个发布包携带 schemas 和 golden wire fixtures，适配器针对 fixtures 做合约测试。

## 3. One-shot CLI

### 3.1 Framing

```text
command: fin-harness invoke
stdin:   恰好一个 UTF-8 JSON object
stdout:  恰好一个 UTF-8 JSON object + LF
stderr:  日志、诊断、堆栈；不得混入 stdout
```

进程成功启动后应尽力保证 stdout 仍只有一个 JSON response，包括无效输入。退出码：

- `0`：成功生成符合协议的 response；包括 `insufficient_data` 等领域拒算；
- `2`：envelope/JSON/schema 无效；response 的 `error.code` 为 `invalid_request` 或 `unsupported_protocol`；
- `70`：其他请求级错误或内部/进程故障，例如 timeout、snapshot_not_found、response_too_large；可序列化时保留具体 error.code，未知异常脱敏为 system_error，进程启动失败可能没有 stdout。

以上是 invoke 的语义。操作者 import-tushare 遇到上游不可用时另返回 69；argparse 参数解析失败不保证 stdout 有协议对象。

调用方以 JSON response 为业务事实，不从 stderr 或自然语言推断成功。

### 3.2 调用 envelope

```json
{
  "protocol": "fin-harness/v1",
  "operation": "analyze",
  "request_id": "host-req-01",
  "request": {
    "entity": "600519.SH",
    "targets": [
      {
        "metric_id": "derived.cashflow.operating.single_quarter_yoy",
        "period": "2026Q2",
        "scope": "consolidated"
      }
    ],
    "as_of": "2026-08-24T12:00:00+08:00",
    "knowledge_policy": "system"
  },
  "context": {
    "client": "mosaic",
    "correlation_id": "opaque-host-run-id"
  }
}
```

规则：

- `request_id` 由调用方生成，只用于关联；不授予权限，也不承诺幂等缓存。每次重试使用新 ID，确定性由 snapshot/公式/代码版本保证；
- `context` 可省略，只用于审计关联，core 不据此鉴权；禁止放 secret、签名 capability 或用户自然语言；
- `entity` 每次只允许一个；使用已导入的精确代码、canonical ID 或独立别名，不拼接名称和代码；多实体由宿主发多次请求；
- `targets` 支持单值和键控表格；每个 target 只有目录中的 `metric_id/period/scope`，不能携带公式、SQL、输入值或代码；
- 请求不携带 vendor/provider；操作者预先导入固定来源，模型不能切换供应商或触发在线取数；
- `as_of` 必须含时区；`knowledge_policy` 必须显式为 `system` 或 `public`。
- 当前每次分析最多 16 个 targets，序列化领域响应最多 1 MiB，执行期限 60 秒；CLI stdin 和 HTTP request body 另有 1 MiB 输入上限（不把它等同于 MCP stdio 的消息 framing 限制）。这些上限暂不能通过配置文件调整。capabilities 中 source_calls=8 是预留预算，当前分析不产生上游调用；import-tushare 每次发一个独立请求，默认网络 timeout 为 20 秒、上游响应上限 8 MiB。

### 3.3 分析响应

下例数值为示意，ID/hash 已简写，不是实际公司结果或可重放凭证。可运行的完整请求与黄金结果见 [fixtures](protocol/v1/fixtures)。

```json
{
  "protocol": "fin-harness/v1",
  "request_id": "host-req-01",
  "run_id": "run_01",
  "status": "ok",
  "results": [
    {
      "result_id": "result_01",
      "key": {
        "entity_id": "cn.company.600519",
        "metric_id": "derived.cashflow.operating.single_quarter_yoy",
        "period": "2026Q2",
        "scope": "consolidated"
      },
      "status": "ok",
      "value": "0.1234",
      "display_value": "12.34%",
      "semantics": {
        "value_kind": "ratio",
        "unit": "1",
        "currency": null,
        "period_basis": "discrete",
        "accounting_standard": "CAS"
      },
      "as_of": "2026-08-24T12:00:00+08:00",
      "knowledge_policy": "system",
      "source_time_precision": "day",
      "formula": {
        "id": "operating_single_quarter_yoy",
        "version": "1.0.0",
        "content_hash": "sha256:..."
      },
      "snapshot_id": "snap_01",
      "validation": {"status": "passed", "check_ids": ["pit", "same_scope", "nonzero"]},
      "provenance": {
        "calculation_id": "calc_01",
        "inputs": [
          {"role": "current_h1", "observation_id": "obs_01", "source_record_id": "src_01", "transform": "identity"},
          {"role": "current_q1", "observation_id": "obs_02", "source_record_id": "src_02", "transform": "subtract_from_current_h1"},
          {"role": "prior_h1", "observation_id": "obs_03", "source_record_id": "src_03", "transform": "identity"},
          {"role": "prior_q1", "observation_id": "obs_04", "source_record_id": "src_04", "transform": "subtract_from_prior_h1"}
        ]
      },
      "warnings": []
    }
  ]
}
```

顶层 `status`：

- `ok`：所有 result 都是 `ok`；
- `partial`：至少一个 `ok`，至少一个非 `ok`；
- `rejected`：请求有效，但没有可发布的权威值；
- `error`：系统/数据源失败。

analyze result `status` 是封闭枚举；部分状态为后续领域扩展保留，并不代表已有对应功能：

```text
ok | insufficient_data | ambiguous_entity | ambiguous_metric |
ambiguous_source_version |
unsupported_metric | not_applicable | validation_failed |
source_error | system_error
```

只有 `ok` 可带 `value`。所有权威数值都是十进制字符串，不是 JSON number；展示文本从不作为计算输入。无结果也必须返回 `results: []` 和结构化 `error`，不得只返回一段解释。

无法进入领域执行时使用统一错误响应；`message` 必须脱敏且不可作为程序分支条件：

```json
{
  "protocol": "fin-harness/v1",
  "request_id": null,
  "status": "error",
  "results": [],
  "error": {
    "code": "invalid_request",
    "message": "request does not match fin-harness/v1"
  }
}
```

顶层 `error.code` 固定为：

```text
invalid_request | unsupported_protocol | permission_denied | rate_limited |
timeout | cancelled | snapshot_not_found | replay_artifact_mismatch |
source_unavailable | response_too_large | system_error
```

身份/授权/限流/取消/超时不伪装成 result status。CLI 在收到可处理的终止信号时回滚当前事务并尽力返回 `cancelled`；宿主强杀导致无 stdout 时按 transport failure 处理。所有入口共享同一错误映射和原子提交点。

### 3.4 Explain 操作

```json
{
  "protocol": "fin-harness/v1",
  "operation": "explain",
  "request_id": "host-req-02",
  "request": {
    "run_id": "run_01",
    "result_ids": ["result_01"]
  }
}
```

响应返回输入 observation、期间转换、公式引用、重算步骤、校验和 source locator。`explain` 从已保存的 snapshot 与版本化公式重算中间步骤；MVP 不在 audit ledger 永久复制每一个中间 Decimal 值。远程入口必须先把 authenticated principal 映射到 tenant，并验证该 tenant 拥有目标 run；`run_id` 不构成授权。

成功输入若来自已核验的 Tushare 等价记录，`inputs[].equivalent_sources` 列出其余 observation、披露/入库时间和 source locator/hash；四个金融输入不因此变成八个，也不重复计算。原始行及等价证据哈希保留在 snapshot，explain/replay 核验全部证据，不重新查询当前候选版本。

拒算结果也出现在 explain 的 `results` 中：原样返回已记录的 `result_id/status/key/as_of/knowledge_policy/warnings/error`，不伪造公式或数值。`error.details` 可包含 `missing_roles`，或冲突期间 `period`、`candidate_count`、最多 16 对 observation/source ID 的 `candidates` 与 `truncated` 标记。这些信息在 analyze 时按 PIT 固定，后续导入不会改变旧拒算的解释。顶层 `status=ok` 仅表示解释请求成功，消费者仍须检查每项的计算状态；`result_ids` 筛选同时适用于成功与拒算项。

`replay`、`doctor`、`capabilities` 是给操作者的 CLI 命令，不注入模型工具列表：

```text
fin-harness replay RUN_ID --json
fin-harness doctor --json
fin-harness capabilities --json
```

## 4. MCP 接口

`fin-harness mcp` 使用官方 MCP SDK，不自行实现 JSON-RPC 生命周期。本地模式默认使用 stdio，stdout 仅承载 MCP 消息且日志仅写 stderr；loopback HTTP 复用同一工具注册表。未来远程部署也必须复用此表。

模型只看到两个工具：

| 工具 | 输入 | 输出 |
|---|---|---|
| `financial_analyze` | `AnalyzeRequest` | `AnalyzeResponse` |
| `financial_explain` | `ExplainRequest` | `ExplainResponse` |

MCP 初始化 `instructions` 只描述跨工具流程、共同约束和速率限制；工具自身的 description 必须明确适用用户目标、必填参数与不适用场景。两项工具复用 v1 input/output schema，并声明真实 annotations：它们不提供交易、写组合或修改源记录的业务能力，`destructiveHint=false`、`openWorldHint=false`；只有实现保证调用不会改变用户领域状态时才设置 `readOnlyHint=true`。内部 audit/log 写入不应变成可由模型操控的业务副作用。

推荐的首段 server instruction 语义是：需要可审计的金融事实或衍生指标时调用 `financial_analyze`；缺少实体、期间或 `as_of` 时先澄清；只在已有 `run_id` 且用户要追问计算过程/证据时调用 `financial_explain`；永远不要把源数据值、公式、SQL、凭据交给工具。插件 skill 不是 v1 正确调用的前置条件。

MCP server 自己生成 request_id，当前仅记录 context.client=mcp，不透传完整 session metadata。业务拒算作为成功的工具调用返回结构化 result；顶层 status=error 映射为 MCP isError=true，同时保留结构化协议错误。Pi 同样保留协议对象，但宿主事件未必标为 tool error；调用方必须读取 response.status/error。

取消由 MCP SDK 接收并经共享 ExecutionControl 传给 core；同步工作线程在最终事务提交 gate 检查取消和期限。gate 前取消会回滚 run/snapshot/audit；gate 已通过后的取消不能撤销已提交的运行，客户端仍可能收不到响应。不会返回或持久化部分结果集。

## 5. 宿主适配矩阵

| 宿主 | 接入方式 | 适配代码 | 宿主仍负责 |
|---|---|---:|---|
| OpenCode | local MCP：`fin-harness mcp` | 0 | MCP 配置、进程超时、session correlation |
| DeepSeek Harness | 官方 `@deepseek-ai/dsh-mcp-client` 连接 `fin-harness mcp` | 0 | plugin 配置、权限/审批、超时；固定已验证版本 |
| ChatGPT desktop | local MCP：`fin-harness mcp` | 0 | MCP 配置、工具启用/审批、进程环境与超时 |
| ChatGPT web（未交付） | 远程 MCP 与插件分发 | 后续认证/部署和薄清单 | 插件安装、工具审批；服务端鉴权与租户隔离 |
| Pi | TypeScript extension 注册两个工具，spawn `fin-harness invoke` | 已提供薄扩展 | AbortSignal、tool result 渲染、进程权限 |
| Mosaic（待宿主实现） | controller 在 capability 签发前调用 invoke 并物化 bundle | 当前仅接入说明 | signed capability、allowlist、bundle/snapshot、run/node/stage、usage ledger |

适配器只允许做五件事：

1. 把宿主 tool 参数校验并映射为协议 request；
2. 执行宿主自己的认证、授权、审批；
3. 启动/取消进程并设超时；
4. 关联宿主 run/session 与 Harness `request_id/run_id`；
5. 把结构化 response 渲染成宿主需要的 tool result。

适配器不得实现指标、PIT、单位转换、Decimal 计算或重写 result status。需要这些逻辑说明公共协议缺字段，应先改协议，而不是复制业务逻辑。

### 5.1 Mosaic 边界

Mosaic 的 signed capability 不进入 `context`，也不传给 core。现有 `tools.call` 只读取已物化的零参数 bundle，不能被改造成任意参数转发器。首选调用顺序为：

```text
Mosaic controller / prepare_capability 前的数据物化阶段
  -> 根据已批准的 as_of / entity / metric scope 构造 fin-harness/v1 request
  -> fin-harness invoke
  -> 校验 response 并写入不可变 snapshot bundle
  -> Mosaic 签发 capability
  -> Agent 继续用现有 tools.call 读取零参数快照
```

若未来必须让 Mosaic agent 动态选择指标，应由 Mosaic 新增专用 capability-bound handler，在进入 Harness 前验证签名、expires/nonce、allowed tools、`as_of` 和参数 scope；不能绕过或放宽当前 `tools.call`。Mosaic manifest 必须显式授权相应工具，Harness 的 `capabilities` 输出不能扩大 Mosaic 权限。

### 5.2 Pi 边界

Pi 官方不内置 MCP，使用 `pi.registerTool()` 的 extension。extension 从随包携带的 v1 schema/TypeBox 等价定义注册参数，执行时 spawn one-shot CLI，把 `AbortSignal` 映射为子进程终止。它不依赖 Financial Harness 的 Python 包内部 API。

### 5.3 ChatGPT 边界

ChatGPT desktop 直接配置本地 stdio server，不需要额外 adapter。ChatGPT web 不读取本地 MCP 配置，必须通过已安装插件访问远程 MCP；插件是分发描述，不是新的 Harness 实现。

本仓库尚未提供可安装的远程插件。正式进入该阶段时，应按届时的 OpenAI 官方文档完成连接注册、认证和薄清单分发；不把早期设想的技术 ID 或清单字段当作 fin-harness/v1 的公共契约。

MVP 不加入 UI、hooks 或重复一份 MCP schema；schema 的唯一来源仍是 protocol/v1，MCP server 在 tools/list 中发布它。公开部署前必须具备稳定 HTTPS、服务端身份/授权和数据许可；本地 loopback 模式没有这些能力。Tushare token 只交给受控导入进程，不需要进入模型工具进程。

## 6. 包边界

```text
fin-harness (Python package)
├── fin-harness invoke
├── fin-harness mcp        # optional dependency extra
├── protocol/v1/*.schema.json
├── core + store           # private implementation
└── tushare_source         # optional fixed-endpoint HTTPS integration

源码 / sdist 中的宿主材料
├── integrations/pi/fin-harness.ts   # 由 Pi 直接加载，无独立 npm 包
├── integrations/mosaic/README.md   # 待 Mosaic 实现
└── examples/hosts/                 # MCP 配置样例
```

OpenCode 和 DeepSeek Harness 只需要配置，不创建空壳 adapter package。远程 OpenAI 插件包尚未交付。

## 7. 合约测试

每次发布至少运行：

- 所有 `protocol/v1/fixtures/*.request.json` 通过 request schema；
- 所有 fixture response 通过 response schema；
- one-shot stdout 恰好一条 JSON、stderr 不影响解析；
- 同一 fixture 经 direct core、CLI、MCP 得到相同规范化 `results`；
- 启用 HTTP profile 时，同一 fixture 经 MCP stdio 与 Streamable HTTP 得到相同规范化 `results`；
- `insufficient_data`/歧义/校验失败不会变成 transport error；
- Decimal 不以 JSON number 泄漏；
- 小数秒、错误实际期间、原始证据内容损坏、审核后追加修订关系、提交前取消、超限和非法参数均有反例测试；
- 每个适配器对相同 fixtures 只改变 correlation/rendering，不改变金融结果；
- 新增 Mosaic handler 后须验证未授权调用在进入 Harness 前被拒绝（当前未实现该 handler）；现有 CLI/MCP 取消不留下可发布的部分结果。
- MCP instructions、tool descriptions、schema 与 annotations 可被发现，且 metadata、错误、日志和结果均不泄漏凭据。

当前保留合成黄金 fixture 和按真实错误增加的对抗测试，不搭建通用插件框架。成功 explain 项含 inputs/formula/steps，拒算项含 status/error；消费者必须处理两种 Schema 分支，而非假定每项都有数值或 steps。

## 8. 接口依据

- [MCP transports specification](https://modelcontextprotocol.io/specification/draft/basic/transports)：stdio 与 Streamable HTTP、framing 和通道纪律；
- [OpenAI ChatGPT/Codex MCP](https://learn.chatgpt.com/zh-Hans/docs/extend/mcp)：ChatGPT desktop 的本地 MCP 配置、ChatGPT web 的插件边界与 MCP 认证方式；
- [OpenAI plugin MCP server](https://developers.openai.com/plugins/build/mcp-server)：工具 metadata、Streamable HTTP、认证、测试与公网部署要求；
- [OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins)：插件清单、已注册连接的 `.app.json` 映射、`.mcp.json` 与打包边界；
- [OpenCode MCP servers](https://opencode.ai/v2/docs/mcp-servers)：本地 MCP 进程和远程 MCP 配置；
- [DeepSeek Harness 官方 MCP client](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)：stdio/Streamable HTTP client 及工具注册；
- [Pi 使用文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/usage.md)与[扩展文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)：Pi 不内置 MCP，工具由 extension 注册；
- Mosaic 项目边界见[本仓库接入说明](integrations/mosaic/README.md)；不依赖读者拥有某个同级本机仓库。
