# fin-harness v0.1 交付评估

评估日期：2026-09-04

## 结论

v0.1 的本地插件纵向切片满足 `DESIGN.md` 第 13.3 节发布门槛。交付范围是：中国 A 股/CAS、合并口径 Q2 经营现金流单季同比、JSON/SQLite 本地存储、one-shot CLI、MCP stdio，以及仅限 loopback 的未认证 Streamable HTTP 测试入口。

这不是生产金融数据服务。ChatGPT Web 公网部署、多人租户运营、Tushare 缓存/再分发许可、更多指标和真实宿主 UI 验收仍是明确的外部阶段，不计入 v0.1 已完成声明。

## 发布门槛证据

| 门槛 | 结果 | 自动化证据 |
|---|---:|---|
| PIT 泄漏为 0 | 通过 | 修订前后、`public/system` 分流、日精度同日边界、未来数据与歧义版本测试 |
| Decimal 与期间转换 100% | 通过 | 四输入 Q2 累计转单季同比黄金结果；`ROUND_HALF_EVEN`、负分母、零分母、NaN 对抗测试 |
| keyed 输出无额外/重复键 | 通过 | 单目标、多目标与部分拒算测试 |
| `ok` 血缘完整率 100% | 通过 | explain 验证四个输入的 source/observation/hash/timestamp；tenant 隔离测试 |
| 重放哈希一致率 100% | 通过 | replay 同时验证 request、formula、build、snapshot、observation 与 result hash |
| 缺必要期间不产生数值 | 通过 | 缺 Q1 fixture 返回结构化 `insufficient_data` |

最终回归命令：

```bash
uv run python -m unittest discover -s tests -v
uvx ruff@0.15.15 check .
uv run python -m compileall -q src tests
node --experimental-strip-types --check integrations/pi/fin-harness.ts
uv build
```

测试套件共 23 项，另包括：所有五张权威表的不可变 trigger、非法协议、checked-in JSON Schema、CLI 单行 framing、MCP discovery/metadata、真实 stdio 子进程、loopback HTTP 与 direct core 结果等价、宿主配置可解析且无 secret、Tushare 响应字段漂移 fail-closed，以及缺 token 时不联网的类型化失败。

## 受控真实数据烟测

在 token 持有者本地环境中，使用临时数据库依次获取并导入 `600000.SH` 的 `20260630`、`20260331`、`20250630`、`20250331` 四个期间；四次响应各映射一条记录。随后 `analyze`、`explain` 与 `replay` 均成功，explain 有且仅有四个输入，replay hash 一致。

烟测没有在仓库或本报告记录供应商财务数值，临时数据库已随进程退出删除。使用的许可标签是 `unverified-local-smoke`，只证明技术链路，不代表已取得长期存储、多人服务或再分发权利。

真实烟测还发现并修正了一个 schema 假设：Tushare cashflow 接口接受 `is_calc=0` 作为输入过滤参数，但响应不返回该字段。当前实现将它固定写入请求和来源维度，同时严格校验其余九个响应字段。

## 跨宿主状态

| 宿主 | v0.1 交付状态 | 边界 |
|---|---|---|
| ChatGPT desktop / Codex | 已交付 stdio 配置样例并用真实 MCP 子进程验证 | 用户仍需复制配置并在自己的桌面会话做 UI 验收 |
| OpenCode | 已交付 local MCP 配置样例 | 未在本机安装宿主 |
| DeepSeek Harness | 已交付官方 MCP client 的配置补丁样例 | 未在本机安装宿主 |
| Pi | 已交付两个工具的薄 TypeScript adapter，完成语法与静态契约检查 | 未在真实 Pi runtime 加载 |
| Mosaic | 已交付 controller 侧 capability materialization 接入说明 | 现有零参数 `tools.call` 不适合直接透传动态金融请求，需由 Mosaic 维护方接入 |
| ChatGPT Web | 未交付，按条件推迟 | 需要公网 HTTPS、OAuth/tenant ACL、限流、稳定域名和数据许可 |

## 已知限制与下一门槛

- 只支持一个领域审核指标；新增指标必须有独立定义、黄金 fixture 和适用范围审核。
- 本地 HTTP 明确拒绝非 loopback 地址，不能直接用于 ChatGPT Web。
- 项目代码采用 Apache License 2.0；该许可不覆盖 Tushare 或其他供应商数据。
- Tushare 许可、配额和再分发责任不能由代码测试替代。
- 生产前需执行多日影子运行，并在真实 ChatGPT/Pi/Mosaic 宿主完成消费者验收。

这些限制是发布边界，不是隐藏的降级路径；core 不接受模型传入的来源数值、公式、SQL、token 或任意代码。
