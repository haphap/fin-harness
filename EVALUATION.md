# fin-harness v0.1 交付评估

评估日期：2026-09-04

## 结论

2026-09-05 审核发现旧测试集未覆盖小数秒 PIT、实际期间不符和跨入口取消等问题，因此撤回之前仅凭 23 项测试便认定满足全部发布门槛的表述。修复后的证据是下列有限黄金集与对抗集通过，不等价于所有金融数据或全部真实宿主已获生产验收。交付范围仍是：中国 A 股/CAS、合并口径 Q2 经营现金流单季同比、JSON/SQLite、本地 CLI/MCP，以及仅限 loopback 的未认证 Streamable HTTP。

这不是生产金融数据服务。ChatGPT Web 公网部署、多人租户运营、Tushare 缓存/再分发许可、更多指标和真实宿主 UI 验收仍是明确的外部阶段，不计入 v0.1 已完成声明。

## 发布门槛证据

| 门槛 | 结果 | 自动化证据 |
|---|---:|---|
| 测试集 PIT 泄漏为 0 | 通过 | 修订前后、双 policy、日精度、小数秒双向边界、审核关系生效时间 |
| Decimal 与期间转换 100% | 通过 | 四输入 Q2 累计转单季同比黄金结果；`ROUND_HALF_EVEN`、负分母、零分母、NaN 对抗测试 |
| keyed 输出无额外/重复键 | 通过 | 单目标、多目标与部分拒算测试 |
| `ok` 血缘完整率 100% | 通过 | explain 验证四个输入的 source/observation/hash/timestamp；tenant 隔离测试 |
| 测试集重放哈希一致率 100% | 通过 | request/response、formula/build、manifest、原始 payload、observation 内容与 raw hash；内容故障注入拒绝重放 |
| 缺必要期间不产生数值 | 通过 | 缺 Q1 fixture 返回结构化 `insufficient_data` |

最终回归命令：

```bash
uv run python -m unittest discover -s tests -v
uvx ruff@0.15.15 check .
uv run python -m compileall -q src tests
node --experimental-strip-types --check integrations/pi/fin-harness.ts
uv build
```

测试套件共 39 项，包含七张 append-only 表、非法协议/参数反例、checked-in Schema 与 MCP 发布契约一致性、CLI 单行 framing、stdio/loopback HTTP、Pi 薄 adapter→真实 CLI、Tushare 字段漂移和无 token 时不联网的类型化失败。

### 审核修复回归

- PIT 查询兼容旧库变长时间字符串，按固定微秒 UTC key 比较；实际 Q1/H1 起止日期必须精确匹配。
- 自定义 registry 必须与唯一已审核定义一致，不会给固定计算伪造公式身份。
- replay 从原始导入 payload 校验 observation 和 source 内容，并核对 request/response/snapshot 摘要。
- 已入库版本通过 `link-revision` 追加审核关系；跨事实族、时间倒置、循环和冲突关系均拒绝；system 不在审核前应用后加的关系。
- 取消或超时在 commit gate 前生效时，run/snapshot/audit 全部回滚；测试覆盖 MCP 线程取消、MCP deadline、CLI 未完成 stdin、事务尾部期限失败。
- 同一超大 explain 在 CLI/MCP 都返回 `response_too_large`；CLI 非零退出并保留 request_id。Pi 保留合法的协议错误对象。
- schema v1 旧库升级只追加表，不改写权威行；缺原始证据时拒算，重导入原始 fixture 后恢复；未知数据库版本不降级。
- `uv build --offline` 与 `python tests/check_distribution.py` 验证 wheel/sdist 契约内容，以及仓库外 clean venv 的无依赖安装、schema 加载、analyze/replay。

Ruff 检查命令：`uvx --offline ruff check src tests`。Pi 的 Node 检查使用宿主注册/Schema 构造器 stub，但执行真实 adapter 和 CLI；这不是完整 Pi runtime 验收。本轮未重新访问真实 Tushare 数据。

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
| Pi | 已交付薄 adapter，并验证真实 CLI 成功/错误、可选 signal、取消和 spawn failure | Schema 构造器/宿主注册使用 stub；未在完整 Pi runtime 加载 |
| Mosaic | 已交付 controller 侧 capability materialization 接入说明 | 现有零参数 `tools.call` 不适合直接透传动态金融请求，需由 Mosaic 维护方接入 |
| ChatGPT Web | 未交付，按条件推迟 | 需要公网 HTTPS、OAuth/tenant ACL、限流、稳定域名和数据许可 |

## 已知限制与下一门槛

- 只支持一个领域审核指标；新增指标必须有独立定义、黄金 fixture 和适用范围审核。
- 本地 HTTP 明确拒绝非 loopback 地址，不能直接用于 ChatGPT Web。
- 项目代码采用 Apache License 2.0；该许可不覆盖 Tushare 或其他供应商数据。
- Tushare 许可、配额和再分发责任不能由代码测试替代。
- 生产前需执行多日影子运行，并在真实 ChatGPT/Pi/Mosaic 宿主完成消费者验收。

这些限制是发布边界，不是隐藏的降级路径；core 不接受模型传入的来源数值、公式、SQL、token 或任意代码。
