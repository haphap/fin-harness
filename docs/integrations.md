# 宿主接入指南

[README](../README.md) · [使用指南](usage.md) · [公共协议](../PROTOCOL.md)

本文对应包含 PR #1 的合并主线。先在 CLI 跑通合成数据，再接宿主；先确认两项工具可见，再测试模型选工具。不需要为每个宿主编写一套金融计算。

## 共同约定

所有宿主只向模型开放 `financial_analyze` 和 `financial_explain`。工具参数是 request 内层对象，**不是**完整 CLI envelope；MCP/Pi adapter 会补 protocol、operation 和 request_id。

可用于合成数据的 analyze 参数：

```json
{
  "entity": "TEST001.CN",
  "targets": [{
    "metric_id": "derived.cashflow.operating.single_quarter_yoy",
    "period": "2026Q2",
    "scope": "consolidated"
  }],
  "as_of": "2026-08-15T12:00:00+08:00",
  "knowledge_policy": "system"
}
```

首次试用可以这样提问：

> 请用金融工具计算 TEST001.CN 的 2026Q2 合并口径经营现金流单季同比，as_of=2026-08-15T12:00:00+08:00，knowledge_policy=system。随后用返回的 run_id 解释计算步骤和四个输入来源。如果拒算，报告原因，不猜数。

预期是 analyze 返回 `0.3333` / `33.33%`，explain 引用同一次 run。检查工具结构化结果，不只看模型总结。实体只传独立代码或已登记别名；explain 顶层 ok 不代表底层计算成功。

宿主负责模型认证、用户审批和进程权限；fin-harness 的模型工具不需要 Tushare token。真实来源及 explain 内容可能发送给宿主所用的模型供应商，必须先获得对应数据处理权限。

## Pi

Pi 通过 extension 注册工具；本项目使用[现有薄扩展](../integrations/pi/fin-harness.ts)调用 `fin-harness invoke`，不需要 MCP extra 或另一个 npm 插件包。加载/工具允许列表规则以 [Pi 官方用法](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/usage.md)为准。

先安装并配置好 Pi 的模型账户；本项目实测版本为 0.84.2。完成 README 的合成导入后，在仓库根目录运行：

```bash
export FIN_HARNESS_BIN="$PWD/.venv/bin/fin-harness"
export FIN_HARNESS_CONFIG="$PWD/examples/config.json"
env -u TUSHARE_TOKEN pi \
  --no-session --no-extensions --extension "$PWD/integrations/pi/fin-harness.ts" \
  --no-skills --no-prompt-templates --no-themes --no-context-files \
  --no-builtin-tools --tools financial_analyze,financial_explain \
  --print --mode json \
  "请计算 TEST001.CN 的 2026Q2 合并口径经营现金流单季同比，as_of=2026-08-15T12:00:00+08:00，knowledge_policy=system，然后解释本次计算的四个输入来源。只使用金融工具，拒算时不要猜数。"
```

这里显式隔离其他工具/扩展，不修改 Pi 的全局配置。使用已有默认模型；若要指定模型，用 Pi 的 `--provider` / `--model` 选项并遵守本机账户权限。此命令会调用模型，可能计费；`--no-session` 不影响 fin-harness 持久化本地 run/snapshot。

| 环境变量 | 作用 |
|---|---|
| `FIN_HARNESS_BIN` | 一个可执行文件路径，默认在 PATH 查找 fin-harness；不要填 `uv run ...` |
| `FIN_HARNESS_CONFIG` | Pi 转发为子进程 `--config` 的路径 |
| `FIN_HARNESS_DB` | 未由 config 显式指定 database 时，传给 CLI 的库路径 |

切换独立数据库时先 `unset FIN_HARNESS_CONFIG`，再设置 `FIN_HARNESS_DB`，或修改自己的配置副本。不要同时设置两个冲突路径后期待环境变量覆盖 config。

已验证：Pi 0.84.2 / opencode-go/deepseek-v4-flash 的真实 print 会话，合成数据及授权 Tushare 数据 analyze/explain；真实数据共四输入、七来源，独立 Decimal 和 CLI 数值 replay 一致。交互 UI、其他模型及多日运行未验收。详见 [EVALUATION.md](../EVALUATION.md)。

## MCP：本地 stdio

```bash
uv sync --frozen --extra mcp --dev
uv run --extra mcp fin-harness mcp --config examples/config.json
```

这是等待 MCP 客户端消息的长驻进程，不会直接显示财务答案。通常由宿主启动，无需另开终端手工启动一份。stdin/stdout 是 MCP 协议通道，不能向其粘贴 one-shot CLI envelope；需要命令行 JSON 时使用 invoke。

编辑样例前，把所有 `/absolute/path/to/fin-harness` 替换成自己的绝对仓库路径，确认 cwd 和数据库一致。只合并需要的配置段，不覆盖已有宿主配置。

### ChatGPT desktop / Codex

使用[样例 TOML](../examples/hosts/chatgpt-codex.toml)。在支持本地 MCP 配置的 ChatGPT 桌面端、Codex CLI 或 IDE 环境中，将对应 table 合入用户 `~/.codex/config.toml` 或受信任项目的 `.codex/config.toml`，重启宿主并检查两项金融工具。[OpenAI 官方 MCP 指南](https://learn.chatgpt.com/zh-Hans/docs/extend/mcp)说明了共享配置、stdio 和桌面/网页边界。

样例设有 `required=true` 和自动工具审批；按自己的启动策略与审批要求审阅后使用，不必为了连通性取消安全策略。工具不改变投资组合或原始事实，但 analyze 会写本地快照和审计。项目只验证了 MCP 子进程，不宣称你所在账户或桌面 UI 已验收。

### OpenCode v2

把[样例](../examples/hosts/opencode.jsonc)合入项目配置。它使用 v2 的 `mcp.servers` 结构、local command 数组和 `codemode=false`，以直接发布两项工具；不要套用 v1 的旧布局。宿主升级后按 [OpenCode 官方 MCP 文档](https://opencode.ai/v2/docs/mcp-servers)核对配置；目前仅交付样例，未做真实宿主端到端验收。

### DeepSeek Harness

使用[配置补丁](../examples/hosts/deepseek.cordis.patch.yml)，按宿主的补丁加载流程合入现有配置；它不是可单独启动的完整配置。`@deepseek-ai/dsh-mcp-client` 负责启动 stdio 进程，`serverName=fin-harness` 为工具加命名空间；模型侧名称可能是 `mcp__fin-harness__financial_analyze`。配置字段和命名规则见[官方 MCP client](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)。目前未完成真实宿主验收。

## Mosaic

当前交付的是[接入约束说明](../integrations/mosaic/README.md)，不是已经实现并安装的 Mosaic adapter。

controller 在 capability 签发前构造已批准的 analyze 请求，执行 CLI 并校验响应，把结果和 snapshot/run 标识物化为 bundle；Agent 继续通过 Mosaic 现有的零参数 `tools.call` 读取。签名、nonce、allowlist 和用户身份留在 Mosaic，不传给通用金融内核。

不要把零参数读取接口改造成任意参数转发器。动态金融请求需要 Mosaic 维护方实现 capability-bound handler，先校验实体、指标、时点和权限，再进入 fin-harness。

## HTTP 与 ChatGPT Web

已提供同核 Streamable HTTP 的 **loopback 测试模式**：

```bash
uv run --extra mcp fin-harness mcp --transport streamable-http \
  --host 127.0.0.1 --port 8000 --config examples/config.json
```

本机 MCP 客户端可访问 `http://127.0.0.1:8000/mcp`；当前实现没有认证，拒绝非 loopback 绑定。不要用公网隧道或反向代理绕过这个边界。

ChatGPT Web 不读取本机配置；远程工具分发与本地 stdio 是不同路径，见 [OpenAI 官方说明](https://learn.chatgpt.com/zh-Hans/docs/extend/mcp)。本项目尚未交付远程插件、公开地址、OAuth、远程 principal→tenant 授权或限流。增加 HTTPS 地址并不足以完成生产接入，还须数据服务许可与真实消费者验收；规划详见 [DESIGN.md](../DESIGN.md)。

## 新增宿主的最小接口

优先让宿主连接现有 MCP。没有 MCP 时，只需启动 CLI、写入一个 envelope、关闭 stdin、读取一行 JSON、传递取消并渲染结果。公共面是 [v1 Schema](../protocol/v1/request.schema.json) 和 [PROTOCOL.md](../PROTOCOL.md)，不是 Python 类或 SQLite 表。

适配器不能选供应商、接收模型金额、重写公式、改变拒算状态或跳过宿主审批。`run_id`、`context` 和 capabilities 输出都不是授权凭据。为新入口复用既有合成契约测试，不新建金融执行路径。
