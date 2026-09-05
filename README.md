# fin-harness

面向 AI Agent 的可审计金融计算插件：模型决定研究什么，fin-harness 负责按哪个时点取数、如何计算，以及如何验证结果。

*Auditable, point-in-time financial calculations for AI agents via JSON CLI and MCP.*

它不是另一个金融聊天 Agent，也不是 Tushare 的通用查询包装器。它接收实体、指标、期间和时间策略，从已导入的 SQLite 事实中选择版本，以 `Decimal` 执行已审核公式，返回结构化结果、来源和可重放快照。不接受模型提供的金额、公式、SQL、代码或凭据。

> 当前为 v0.1 本地研究版本。文档已对齐 2026-09-05 合并的 [PR #1](https://github.com/haphap/fin-harness/pull/1)，包含真实 Pi 测试后的修复。仍有两项已知 P1，不能把烟测通过视为生产验收，详见 [EVALUATION.md](EVALUATION.md)。

[快速上手](#快速上手) · [使用指南](docs/usage.md) · [宿主接入](docs/integrations.md) · [协议](PROTOCOL.md) · [设计](DESIGN.md) · [验证与限制](EVALUATION.md)

## 为什么需要它

- 时点明确：区分“当时公开了什么”和“本系统当时实际归档了什么”。
- 计算确定：使用版本化公式和十进制算术，禁止模型自由改写计算规则。
- 可追溯：成功结果绑定输入、来源、公式版本和不可变快照；操作者可重放核验。
- 失败显式：缺数据、版本冲突、口径不符或零分母时拒算，不用猜测值填空。
- 宿主无关：JSON CLI 和 MCP 共用一个内核，换模型或宿主无需重写金融逻辑。

```text
操作者 ── fixture / Tushare HTTPS 导入 ──> SQLite 事实与来源
                                               │
Agent ── JSON CLI / MCP ──> PIT 选数 → Decimal 计算 → 结果与快照
                                               │
                           explain / replay <──┘
```

分析时不访问 Tushare；凭据只用于操作者的导入进程。这里的“插件”指可独立运行的外部能力，不是绑定某个 Agent SDK 的金融代码库。

## 当前能做什么

v0.1 只支持中国 A 股、CAS、合并口径的 **Q2 经营现金流单季同比**：

```text
metric_id = derived.cashflow.operating.single_quarter_yoy
本年 Q2 = 本年 H1 累计 − 本年 Q1 累计
上年 Q2 = 上年 H1 累计 − 上年 Q1 累计
同比 = (本年 Q2 − 上年 Q2) / abs(上年 Q2)
```

结果是比率字符串，例如 `"0.3333"`，对应展示值 `"33.33%"`。当前不支持其他季度的该衍生指标、母公司口径、自由公式、自动选股、交易或投资组合修改。Schema 能表达某个请求，不代表当前指标一定适用。

## 快速上手

需要 Git、Python 3.11+ 和 uv。以下命令适用于 Bash/zsh；安装后合成数据演示不需要 Tushare token 或模型账户。

```bash
git clone https://github.com/haphap/fin-harness.git
cd fin-harness
uv sync --frozen --extra mcp --dev
uv run fin-harness import-fixture tests/fixtures/source-success.json --config examples/config.json
uv run fin-harness invoke --config examples/config.json < protocol/v1/fixtures/analyze-success.request.json
```

已在仓库中则跳过 clone/cd，不要覆盖现有工作目录。示例配置在仓库根目录运行时使用 `var/fin-harness.sqlite3`；若已有研究数据库，先按[使用指南](docs/usage.md)选择独立数据库。

预期：首次导入 5 条合成记录（相同数据重复导入返回 0，属正常去重）；分析顶层 `status=ok`，`results[0].value="0.3333"`，`display_value="33.33%"`。完整 JSON 还含 `run_id`、`snapshot_id`、来源和校验信息。`TEST001.CN` 是虚构测试实体，不是真实股票。

把本次响应的 `run_id` 替换下面的 `RUN_ID`，继续使用同一个配置：

```bash
uv run fin-harness explain RUN_ID --config examples/config.json --json
uv run fin-harness replay RUN_ID --config examples/config.json --json
uv run fin-harness doctor --config examples/config.json --json
uv run fin-harness capabilities --config examples/config.json --json
```

成功 explain 应有四个输入及计算步骤；replay 应返回 `match=true`。`doctor` 检查库路径和构建/公式身份，不证明数据完整或具备供应商权限。

## 接入你的 Agent

模型只需两个工具：`financial_analyze` 发起分析，`financial_explain` 根据已返回的 `run_id` 解释计算或拒算。导入、修订审核和 replay 留给操作者。模型通过工具描述和 Schema 获取参数要求；无需先安装一个金融 skill。

| 宿主 | 接入方式 | 已验证边界 |
|---|---|---|
| Pi | [TypeScript 薄扩展](integrations/pi/fin-harness.ts) → JSON CLI | 真实 Pi 0.84.2 + 模型 + Tushare 数据烟测通过 |
| ChatGPT desktop / Codex | [本地 MCP 配置](examples/hosts/chatgpt-codex.toml) | MCP stdio 子进程通过；宿主 UI 待验收 |
| OpenCode v2 | [local MCP 配置](examples/hosts/opencode.jsonc) | 配置样例，未完成真实宿主验收 |
| DeepSeek Harness | [官方 MCP client 配置补丁](examples/hosts/deepseek.cordis.patch.yml) | 配置样例，未完成真实宿主验收 |
| Mosaic | [controller 物化快照](integrations/mosaic/README.md) | 接入约束说明，需宿主实现 |
| ChatGPT Web | 远程 MCP + 认证与租户授权 | 未交付；本地 loopback HTTP 仅用于测试 |

启动步骤、Pi 完整命令和示例提问见[宿主接入指南](docs/integrations.md)。相同宿主名称不等于相同验证程度。

## 接入真实数据前

先阅读[使用指南中的 Tushare 流程](docs/usage.md#tushare-真实数据)：确认账户权限、数据存储和模型处理权利，由操作者导入四个期间，再让 Agent 查询。代码许可不包含供应商数据许可。

固定 cashflow 投影中仅 `update_flag` 不同的等价记录，经严格核验后可共用一个计算输入，原始行全部保留，explain 展示额外来源。真正冲突的版本仍拒算；拒算 explain 返回当次原因及有界诊断，不重新选择当前数据。

## 验证与当前限制

48 项测试覆盖 PIT、小数秒边界、精确期间、Decimal、证据完整性、取消/期限、CLI/MCP 契约、Pi 适配器和等价来源；wheel/sdist 通过隔离安装检查。真实 Pi 的单标的、单指标 print 模式也已跑通，数值经独立复算和 replay 核验。

复审仍有两项 P1：非法 RFC3339 时区 offset 分钟溢出、并发修订关联可能成环。生产使用前必须修复；不要并发执行 `link-revision`。多日运行、更多数据和其他宿主 UI 尚未验收。完整证据以 [EVALUATION.md](EVALUATION.md) 为准。

## 开发与扩展

```bash
uv sync --frozen --extra mcp --dev
uv run --extra mcp python -m unittest discover -s tests -v
uvx ruff check src tests
uv build
uv run python tests/check_distribution.py
```

核心包无第三方运行时依赖；MCP 是可选 extra，Pi 扩展由 Pi 加载。没有独立 npm 包或文档站。

新增指标需要定义、适用范围、确定性实现和合成黄金/拒算测试；不能只改 registry JSON 就宣称支持新公式。新增宿主优先复用 MCP 或 CLI，认证和审批留在宿主侧，禁止复制 PIT/金融计算。问题与贡献请提交到 [Issues](https://github.com/haphap/fin-harness/issues)，附版本、脱敏请求、预期和实际行为；不要上传 token、真实数据库或未获授权的数据。

## 文档地图与许可

- [使用指南](docs/usage.md)：CLI、配置、时间策略、真实数据导入、升级与排错。
- [宿主接入](docs/integrations.md)：Pi、MCP、Mosaic 及远程边界。
- [PROTOCOL.md](PROTOCOL.md)：请求/响应、错误、退出码和兼容性。
- [DESIGN.md](DESIGN.md)：架构决策与明确标注的后续规划。
- [EVALUATION.md](EVALUATION.md)：验证证据、已知问题和未交付能力。

代码与文档采用 [Apache License 2.0](LICENSE)。该许可不授予 Tushare 或其他供应商数据的存储、服务或再分发权利。
