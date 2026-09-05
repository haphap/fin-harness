# fin-harness v0.1 交付评估

评估日期：2026-09-05

代码基线：合并提交 `1e3940a`（[PR #1](https://github.com/haphap/fin-harness/pull/1)），包含原审核修复 `07f30f2` 和真实 Pi 回归修复 `ddd0753`。后述 48 项测试与真实数据成功记录对应这条已合并代码主线，不是仅本地未提交能力。操作步骤见[使用指南](docs/usage.md)，连接方式见[宿主指南](docs/integrations.md)。

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
uv sync --frozen --extra mcp --dev
uv run --extra mcp python -m unittest discover -s tests -v
uvx ruff check src tests
uv run python -m compileall -q src tests
node --experimental-strip-types --check integrations/pi/fin-harness.ts
uv build
uv run python tests/check_distribution.py
```

测试套件共 48 项，包含七张 append-only 表、非法协议/参数反例、checked-in Schema 与 MCP 发布契约一致性、CLI 单行 framing、stdio/loopback HTTP、Pi 薄 adapter→真实 CLI、Tushare 字段漂移和无 token 时不联网的类型化失败，以及等价来源和拒算 explain 的回归。

### 审核修复回归

- PIT 查询兼容旧库变长时间字符串，按固定微秒 UTC key 比较；实际 Q1/H1 起止日期必须精确匹配。
- 自定义 registry 必须与唯一已审核定义一致，不会给固定计算伪造公式身份。
- replay 从原始导入 payload 校验 observation 和 source 内容，并核对 request/response/snapshot 摘要。
- 已入库版本通过 `link-revision` 追加审核关系；测试覆盖顺序操作时跨事实族、时间倒置、循环和冲突关系拒绝；system 不在审核前应用后加的关系。并发成环缺陷仍待修复，见下文。
- 取消或超时在 commit gate 前生效时，run/snapshot/audit 全部回滚；测试覆盖 MCP 线程取消、MCP deadline、CLI 未完成 stdin、事务尾部期限失败。
- 同一超大 explain 在 CLI/MCP 都返回 `response_too_large`；CLI 非零退出并保留 request_id。Pi 保留合法的协议错误对象。
- schema v1 旧库升级只追加表，不改写权威行；缺原始证据时拒算，重导入原始 fixture 后恢复；未知数据库版本不降级。
- `uv build --offline` 与 `python tests/check_distribution.py` 验证 wheel/sdist 契约内容，以及仓库外 clean venv 的无依赖安装、schema 加载、analyze/replay。

Ruff 检查命令：`uvx --offline ruff check src tests`。自动化 Pi adapter 测试使用宿主注册/Schema 构造器 stub，但执行真实 adapter 和 CLI；完整 Pi runtime 的独立烟测见下文，不混同于测试套件。

### 等价来源与拒算 Explain 修复

- 仅对本项目固定 Tushare cashflow 投影中、经原始证据与映射复核后仅 `update_flag` 不同的记录视为等价；先执行 PIT/修订筛选，不按 flag 推断新旧版本。金额、披露时间、报表口径或来源维度不同仍拒算。
- 保留所有权威行；快照固定全部等价来源的引用/哈希，explain 展示四个计算输入及附加等价证据，replay 逐条验证。不新增表、依赖或宿主专用执行路径。
- 拒算 explain 返回当次持久化的结果、原因及有界候选标识/缺失角色，不再返回空数组；后续导入不会改变历史诊断。CLI/MCP、部分拒算与 result_ids 筛选均有回归。
- 既有 Tushare 原始行无需重新导入。执行构建摘要包含等价规则：旧 run 仍要求原构建解释/重放，应新建 analyze run 使用修复，不能绕过构建一致性校验。

## 文档验证（合并后）

README、操作指南、宿主指南及协议/设计文档已按合并主线核对，未修改金融内核或增加运行时依赖：

- 在仓库外导出合并代码、创建全新虚拟环境和数据库，按 README 执行 frozen sync → 合成导入 → analyze → explain → replay → doctor/capabilities：首次 5 条记录、`0.3333` / `33.33%`、四输入和 `match=true` 均通过。
- 独立数据库示例、当前时间请求生成器及请求/响应 Schema 校验通过；本轮不调用模型或访问 Tushare。
- 58 个本地 Markdown 链接/锚点、9 段 JSON 及 12 段 Shell 示例通过检查；Shell 以 Bash/zsh 语法校验，不把配置样例当作真实宿主验收。
- 48 项回归、Ruff、wheel/sdist 构建和 clean-wheel 无依赖 analyze/replay 通过；源码包包含两份新指南。

## 受控真实数据烟测

在 token 持有者本地环境中，使用临时数据库依次获取并导入 `600000.SH` 的 `20260630`、`20260331`、`20250630`、`20250331` 四个期间；四次响应各映射一条记录。随后 `analyze`、`explain` 与 `replay` 均成功，explain 有且仅有四个输入，replay hash 一致。

烟测没有在仓库或本报告记录供应商财务数值，临时数据库已随进程退出删除。使用的许可标签是 `unverified-local-smoke`，只证明技术链路，不代表已取得长期存储、多人服务或再分发权利。

真实烟测还发现并修正了一个 schema 假设：Tushare cashflow 接口接受 `is_calc=0` 作为输入过滤参数，但响应不返回该字段。当前实现将它固定写入请求和来源维度，同时严格校验其余九个响应字段。

### 2026-09-05 真实 Pi + Tushare 回归

经用户确认临时本地存储和现有模型分析权限，四期真实 cashflow 共导入 7 行，三对仅 `update_flag` 不同。修复前实际 Pi 调用触发 `ambiguous_source_version`，未通过数值验收；修复后在原库的 SQLite backup 副本上复测，未删行、改值或再次访问 Tushare。

Pi 0.84.2 / `opencode-go/deepseek-v4-flash` 实际加载原薄扩展，在无内置工具、无 token 环境下完成两次 analyze 和两次 explain：当前 system 时点得到成功数值，四输入/七来源完整，独立 Decimal 复算与 CLI 数值 replay 一致；早于入库的历史 system 时点仍拒算，explain 原样返回四个缺失角色。原始 source/observation/payload 表逐行比较保持不变，四次工具响应符合 Schema。

这是单标的、单指标、单模型的 print 模式烟测，非交互 UI 或生产验收。模型文字总结曾将三期等价来源泛化为每期均有，工具结构化证据则正确；消费者仍应以 keyed 结构化结果为准。本仓库不收录真实财务数值、原始数据或凭据；临时证据仅留在用户本地，不宣称供应商长期缓存或再分发许可。

## 跨宿主状态

| 宿主 | v0.1 交付状态 | 边界 |
|---|---|---|
| ChatGPT desktop / Codex | 已交付 stdio 配置样例并用真实 MCP 子进程验证 | 用户仍需复制配置并在自己的桌面会话做 UI 验收 |
| OpenCode | 已交付 local MCP 配置样例 | 未在本机安装宿主 |
| DeepSeek Harness | 已交付官方 MCP client 的配置补丁样例 | 未在本机安装宿主 |
| Pi | 薄 adapter 自动化测试通过；Pi 0.84.2 完整 runtime print 模式已加载，真实模型/数据 analyze→explain 通过 | 单模型/单指标烟测；交互 UI、其他模型与多日运行未验收 |
| Mosaic | 已交付 controller 侧 capability materialization 接入说明 | 现有零参数 `tools.call` 不适合直接透传动态金融请求，需由 Mosaic 维护方接入 |
| ChatGPT Web | 未交付，按条件推迟 | 需要公网 HTTPS、OAuth/tenant ACL、限流、稳定域名和数据许可 |

## 已知限制与下一门槛

- Luna 复审的两项 P1 尚未纳入本次等价来源修复：RFC3339 非法 offset 分钟溢出被接受；并发相反 revision link 可能成环。未宣称满足生产正确性门槛。
- 只支持一个领域审核指标；新增指标必须有独立定义、黄金 fixture 和适用范围审核。
- 本地 HTTP 明确拒绝非 loopback 地址，不能直接用于 ChatGPT Web。
- 项目代码采用 Apache License 2.0；该许可不覆盖 Tushare 或其他供应商数据。
- Tushare 许可、配额和再分发责任不能由代码测试替代。
- 生产前需修复上述 P1、执行多日影子运行，并补齐真实 ChatGPT/Pi/Mosaic 的消费者/UI 验收。

这些限制是发布边界，不是隐藏的降级路径；core 不接受模型传入的来源数值、公式、SQL、token 或任意代码。
