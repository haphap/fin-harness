# Financial Harness 设计方案

状态：Proposed v0.4
日期：2026-09-04
范围：绿地 MVP，面向可复现、可审计的金融数据分析

## 1. 结论

Financial Harness 应当是 Agent 与金融数据之间的**确定性执行与治理边界**，而不是另一个会写报告的 financial agent。

它接收经过约束的分析请求，完成实体解析、PIT 数据选择、期间转换、版本化公式计算、验证和证据封装，再把结构化结果交还给 Agent 解释。Agent 可以决定“研究什么”，但不能绕开 Harness 自由决定“数字是什么、截至何时可知、怎么算、是否可信”。

共享对话提出的 8 个能力域是合理的能力地图，但不应被实现成 8 个服务。首版采用：

- 一个 Python 模块化单体；
- 一个 SQLite 数据库；
- 一个宿主无关 one-shot JSON CLI；
- 一个标准 MCP 接口：MVP 使用 stdio，ChatGPT 网页部署按需启用 Streamable HTTP；
- 一个版本化指标目录和受信任纯函数计算表；
- 一套黄金案例和对抗测试。

首个纵向切片聚焦**上市公司财报衍生指标**，覆盖累计转单季、同比、存量/流量对齐、PIT、重述、单位、拒算和逐值血缘。Claim Graph、通用工作流平台、图数据库、微服务、在线交易和自动策略演化均推迟到证据表明需要时。

## 2. 背景与证据

[FinIndices](https://arxiv.org/abs/2607.28661) 把长财务报表上的问题分成 Single-Index 与 Table-Index，并测试领域知识、口径对齐和时间推理。论文及其[公开测试集](https://huggingface.co/datasets/Findata/Finindice)表明，模型在缺少公式、处理累计数、对齐存量/流量以及同时生成多指标多期间表格时容易失败；公开数据当前只有 640 条测试样本，适合作为外部压力测试，不足以复现整套训练流程。

[FinQA](https://aclanthology.org/2021.emnlp-main.300/) 为金融问题保留可执行推理程序，[TAT-QA](https://aclanthology.org/2021.acl-long.254/) 将表格与相关文本结合，[DocFinQA](https://aclanthology.org/2024.acl-short.42/) 又把问题放回完整 SEC 报告。这说明评测不能只看最终文本，还应分别衡量检索、结构化取数、程序/公式选择与执行。

血缘模型借鉴 [W3C PROV-DM](https://www.w3.org/TR/prov-dm/) 的 Entity、Activity 与 Derivation 概念。MVP 只保留内部稳定 ID 和关系，不为 [OpenLineage](https://openlineage.io/docs/spec/object-model/) 预埋专用字段；确有企业目录集成时再写 exporter。

## 3. 产品定义

### 3.1 目标用户

- Research Agent：请求指标或一组分析结果，消费结构化证据。
- 量化/研究工程师：登记指标定义、导入数据、运行回测与回归评测。
- 审核者：从结果反查公式版本、源数据、数据时点和校验记录。

### 3.2 核心用例

1. 请求“某公司 2026Q2 单季度经营现金流同比”，指定 `as_of`。
2. Harness 解析实体和指标，确定当前/同期各自所需的 H1 与 Q1 累计值。
3. 只选择 `as_of` 时系统允许看见的版本，完成单位/口径校验。
4. 使用版本化公式和 Decimal 算术计算；必要数据缺失时明确拒算。
5. 返回带来源、公式、步骤、验证和快照哈希的结构化结果。
6. 同一请求、同一快照、同一代码/公式版本可重复得到同一结果。

### 3.3 成功标准

- 数值结果可由非 LLM 执行器重算。
- 任何输出值都绑定 `metric_id + entity_id + period + scope + unit`。
- 历史运行不能读取当时不可知的数据。
- 缺失、歧义、校验失败是显式状态，不伪装成 `0`、`null` 或自然语言猜测。
- 每个结果都能追溯到数据快照、原始来源和公式版本。
- Agent/模型替换不改变底层金融语义。

### 3.4 非目标

- 不直接生成买卖建议或执行交易。
- 不在 MVP 做任意金融领域的统一本体。
- 不允许 LLM 生成任意 Python、SQL 或表达式并直接执行。
- 不做通用 ETL/调度平台、模型网关或报告写作平台。
- 不承诺 OCR、PDF 表格解析、行情、宏观、商品、衍生品和组合风险一次覆盖。
- 不允许自动研究系统修改会计定义、PIT、单位、日历或风险硬规则。

## 4. 设计原则

1. **PIT 默认开启**：每个请求必须有 `as_of`；没有则只允许显式的 live 模式，并记录实际时间。
2. **语义先于数值**：数值必须携带实体、指标、期间、口径、单位、币种和来源。
3. **LLM 不做权威算术**：LLM 只能提出受约束的请求或候选映射。
4. **定义可版本化，不可静默改写**：公式升级创建新版本，旧运行仍可重放。
5. **拒算优于猜测**：缺输入、分母无定义、实体歧义、范围不适用均返回类型化状态。
6. **逐值血缘**：表格不是无位置数字集合；每个单元格以 `(metric_id, period)` 为键。
7. **验证分层**：Schema、硬不变量、领域一致性先于统计异常和 LLM critic。
8. **审计与遥测分离**：审计记录完整且不可变；运行日志/trace 可采样、可过期。
9. **先单体后分布式**：只有并发量、团队边界或隔离需求被测量后才拆服务。

## 5. MVP 边界

### 5.1 支持

- 一个市场/会计准则配置（建议先选中国 A 股/中国会计准则；若首批数据来自 SEC，则替换为 US GAAP，不同时做两套）。
- JSON fixture 与已脱敏的 Tushare HTTPS 响应 fixture；真实远程连接放到受控单用户试点。只有出现真实表格交换需求时才增加 CSV importer。
- 精确 canonical entity ID 和显式 alias 表；模糊匹配只返回候选，不自动落锤。
- 三大财务报表的少量原始科目。
- 5 类衍生能力：累计转单季、同比/环比、TTM、期初期末平均、比率/百分比。
- 第一个纵向切片只实现 1 个代表性指标；闭环通过后扩到 8–12 个。
- 单值与键控多值请求。
- PIT/重述选择、Decimal 计算、单位归一、血缘、硬校验和审计。
- one-shot CLI、optional MCP stdio 与离线评测。

### 5.2 推迟

| 能力 | 推迟原因 | 何时增加 |
|---|---|---|
| HTTP 服务 | 单用户本地库与 ChatGPT desktop 不需要远程边界 | ChatGPT web 成为交付目标，或出现远程多用户需求 |
| DuckDB/Parquet | SQLite 足够验证首批财报闭环 | 数据超过 SQLite 扫描/SLA，或需要大规模 benchmark |
| OpenTelemetry exporter | 单进程 JSON 日志足够定位问题 | 多进程/远程工具调用出现 |
| OpenLineage exporter/backend | 当前无企业目录消费者 | 需要接入企业数据目录，并由 adapter 映射内部 ID |
| 图数据库/Claim Graph | 没有经过验证的 claim 消费者 | claim 开始驱动组合/决策并需跨报告查询 |
| 模糊实体解析/向量检索 | 容易把歧义隐藏成“智能” | alias 表召回成为已测瓶颈 |
| LLM critic | 不能替代硬规则，且引入成本/随机性 | 硬规则后仍有高价值语义错误 |
| 微服务/消息队列 | 当前无并发与隔离证据 | 单体已出现明确资源或团队边界 |

## 6. 总体架构

```text
Mosaic / Pi adapter      OpenCode / DeepSeek Harness
          |                        |
          | one-shot JSON          | MCP stdio
          +------------+-----------+
                       |
                       | versioned JSON Schema
                       v
+---------------- Financial Harness ----------------+
| Resolve -> Plan -> Snapshot -> Compute -> Validate |
|    |         |          |          |          |    |
| Entity    Metric     PIT Data   Decimal     Rules   |
| Catalog   Registry   Gateway    Formula      |      |
|    +---------+----------+----------+----------+      |
|                       Audit Ledger                 |
+----------------------------------------------------+
                       |
                       | structured results + provenance
                       v
              Research Agent / Reviewer
```

这是一个部署单元，不是微服务拓扑。模块边界用于测试和治理，进程边界以后再决定。

### 6.1 宿主无关接口

公共协议详见 [`PROTOCOL.md`](PROTOCOL.md)。其最小接口为：

- `fin-harness invoke`：一次调用、一个 JSON 输入、一个 JSON 输出，供 Mosaic、Pi 和合约测试使用；
- `fin-harness mcp`：同一个标准 MCP server；本地默认 stdio，ChatGPT 网页部署按需启用 Streamable HTTP；
- 模型只看到 `financial_analyze` 与 `financial_explain`；doctor/replay/capabilities 不进入模型上下文；
- 身份、签名、审批、session 和 cancellation 由宿主/adapter 管理，金融语义由 core 管理。

插件在这里指“符合公共协议的外部可执行能力”，不指绑定任一宿主 SDK 的源码插件。OpenAI 插件只是该能力面向 ChatGPT 网页版的一种薄分发包装，不拥有金融逻辑。MVP 不实现第二套常驻 JSON-RPC server；若首发目标仅为本地 Agent 和 ChatGPT desktop，也不实现 HTTP。

### 6.2 内部模块职责

| 模块 | 负责 | 不负责 |
|---|---|---|
| `protocol` | 请求/响应 Schema、状态、CLI/MCP 映射 | 宿主鉴权、业务推理 |
| `core` | 目录、时间转换、Decimal 计算、验证与状态流 | 通用 DAG、任意代码执行 |
| `store` | 导入、PIT 选择、快照、审计与血缘 | 外部抓取框架、宿主 session |
| `cli` | invoke/explain/replay/doctor/capabilities 与进程纪律 | 复制 core 逻辑 |

这是首版物理文件边界，不否认 catalog/data/temporal/compute/validate 等逻辑职责。只有文件明显过长或第二实现出现时才继续拆分。

### 6.3 Tushare HTTPS：首个上游数据源

Tushare 同时提供 [Python SDK/HTTP API](https://tushare.pro/document/1?doc_id=40) 和[官方远程 MCP 服务](https://tushare.pro/document/1?doc_id=463)。对 Agent 仍暴露 Financial Harness 自己的 MCP；权威数据入库默认使用固定 HTTPS endpoint 的薄客户端，而不是 Tushare MCP 或先转换成 pandas float 的 SDK 表格。

```text
Agent Host
  -> fin-harness mcp            # 北向：受治理的金融工具
  -> Core
  -> tushare_source
  -> Tushare HTTPS API          # 南向：原始 JSON bytes + Decimal 解析
```

选择直接 HTTPS 的原因是 Harness 必须在供应商数值变成 binary float 前保留响应字节并按 Decimal 解析，同时明确指定 endpoint/fields、控制分页/重试并冻结 fixture。标准库客户端比 SDK+pandas 更少依赖，也不需要拦截 SDK 内部传输。官方 MCP 当前公开文档未给出稳定的 `tools/list`/output schema，而且面向 Agent 的压缩或格式化不应进入权威入库链。不能让 Agent 先调用 Tushare MCP 再把数值传给 Harness。

`AnalysisRequest` 不新增 `provider=tushare`。MVP 的数据源由操作者配置并固定，Agent 不能选择供应商；出现第二个真实供应商和明确的 fallback 规则后，再在 server 内增加路由。

首个现金流切片只要求 Tushare 上游返回：

| Tushare 字段 | Harness 语义 |
|---|---|
| `ts_code` | entity external ID |
| `end_date/end_type` | 财务期间及类型 |
| `ann_date/f_ann_date` | public knowledge date 候选 |
| `report_type` | 合并/母公司、单季、调整/调整前版本 |
| `comp_type` | 一般工商、银行、保险、证券适用性 |
| `n_cashflow_act` | 经营活动现金流量净额 observation |
| `is_calc` | cashflow 输入过滤参数；v1 固定请求 `0`，进入 locator/source dimensions 和来源身份键（当前响应不回传该字段） |
| `update_flag` | 当前版本提示，不单独充当修订顺序 |
| Harness 抓取时间 | `ingested_at` |

[Tushare cashflow](https://tushare.pro/document/2?doc_id=44) 文档确认这些字段和报表类型。`published_at` 优先使用实际披露日 `f_ann_date`，缺失时才使用 `ann_date`，两者都保留在 SourceRecord；两者均缺失时 `public` policy 拒算。日期只有日精度时，同一公告日内不声称盘中可知：记录 `source_time_precision=day`，从 Asia/Shanghai 下一自然日 `00:00` 起进入 public 信息集。`system` 历史回放只读本地已归档且 `ingested_at <= as_of` 的版本，禁止为了补历史缺口临时查询当前 Tushare。

`tushare_source.py` 只 POST 到固定 `https://api.tushare.pro`，使用系统 CA 校验证书、显式 timeout/响应大小上限，并在解析前保存响应 bytes 哈希。JSON decoder 使用 `parse_float=Decimal`；选定字段再规范化为十进制字符串。source adapter 负责：

- 将结构化原始行映射为 Observation；
- 保存 endpoint、fields、脱敏参数、原始 bytes hash、规范化响应行 hash 和抓取时间；
- 按 source contract 固定每个字段的单位、倍数、维度和 canonical decimal 序列化；
- PIT/重述选择、snapshot、Decimal、校验和审计；
- 显式 fields、分页/限流/重试和部分失败的类型化映射。

token 只从 `TUSHARE_TOKEN` 注入并放入 HTTPS request body；不写入本地配置、请求快照、日志或审计。首版固定 API 名、字段集并保存脱敏响应 fixture；字段缺失或 schema 漂移时 provider fail closed。真实接入先限定为 token 持有者本地单用户用途；账号共享、缓存、解释输出和再分发范围没有书面许可前，不开放多人服务。以后只有 Tushare MCP 能通过同一套 source contract fixtures且稳定返回未摘要结构化行时，才增加可选 MCP source；它不作为 HTTPS source 的静默 fallback。

### 6.4 ChatGPT：两种部署形态，一个 MCP 契约

ChatGPT desktop 与 ChatGPT web 不是同一种部署边界：

```text
ChatGPT desktop ── MCP stdio ────────────────────┐
                                                 ├─> MCP adapter -> Core -> Tushare HTTPS
ChatGPT web ── OpenAI plugin ── HTTPS /mcp ──────┘
                              Streamable HTTP
```

- **desktop/local**：直接配置 `fin-harness mcp`，由本机启动进程。SQLite、snapshot、audit 与 `TUSHARE_TOKEN` 都留在受信任主机；不需要公网服务或 OpenAI 插件包。
- **web/hosted**：ChatGPT 网页版不读取本地 MCP 配置。需要将同一个 MCP adapter 以 Streamable HTTP 暴露在稳定公网 HTTPS `/mcp`，在 ChatGPT Developer Mode 注册该连接，再用一个只包含清单和已注册连接映射的 OpenAI 插件安装到 ChatGPT。
- 两种形态只改变 transport 与部署位置；`financial_analyze`、`financial_explain`、JSON Schema、PIT、Decimal、状态和审计语义必须相同。不得为 ChatGPT 再写一套 Python core 或 TypeScript 金融实现。
- MCP 初始化 `instructions` 只写跨工具规则；工具选择和参数由工具 description、input/output schema 与 annotations 驱动。关键指引放在前 512 字符：何时调用 `financial_analyze`、缺少 entity/period/`as_of` 时先澄清、何时凭 `run_id` 调用 `financial_explain`、不得让模型提交源值或公式。
- 首版不加自定义 UI，也不要求独立 skill；只有代表性对话证明工具 metadata 不足以稳定完成多步工作流时，才给插件增加 skill。
- 远程个人测试可用受控 Bearer 凭据；需要用户身份或对外分发时使用 OAuth 2.1，并由服务端逐请求授权、限流和隔离租户。MCP annotations 与 ChatGPT 的审批不能替代服务端授权。
- 远程部署的 `TUSHARE_TOKEN` 只存在服务端 secret manager。插件清单、MCP metadata、参数、结果、日志和 trace 都不得包含 token。
- web 形态的 `system` knowledge policy 只指远程服务真实归档的 `ingested_at`；它不能冒充本地 SQLite 的历史信息集。若需跨端一致，必须显式同步不可变快照并校验 hash。
- 公开分发前必须确认 Tushare 许可是否允许相应的数据服务方式；这不是 MCP 技术层可以绕过的问题。

## 7. 核心数据模型

### 7.1 时间必须分成三类

- **业务时间**：财报/行情描述的期间，如 `2026-04-01..2026-06-30`。
- **外部可知时间** `published_at`：数据对市场公开的最早可核验时间。
- **系统可知时间** `ingested_at`：当前系统实际获得该版本的时间。

查询策略：

- `knowledge_policy=system`：回放真实系统，要求 `ingested_at <= as_of`。
- `knowledge_policy=public`：研究理论上的公开信息集，要求 `published_at <= as_of`，并在输出中标记并非真实采集回放。
- 同一事实的修订/重述不覆盖旧行；以 `[known_from, known_to)` 建立版本有效区间。

只保留一个模糊的 `date` 字段会让 look-ahead bias 无法审计，因此禁止。

### 7.2 `Observation`

```json
{
  "observation_id": "obs_...",
  "entity_id": "cn.company.600519",
  "metric_id": "statement.cashflow.operating_net",
  "period": {
    "start": "2026-01-01",
    "end": "2026-06-30",
    "fiscal_year": 2026,
    "label": "2026H1",
    "basis": "ytd"
  },
  "scope": "consolidated",
  "accounting_standard": "CAS",
  "company_type": "industrial",
  "reporting_variant": "reported",
  "source_dimensions": {
    "report_type": "1",
    "comp_type": "1",
    "end_type": "2",
    "is_calc": "0"
  },
  "value": "24.310000",
  "unit": "CNY_1e8",
  "currency": "CNY",
  "source_ann_date": "2026-08-08",
  "source_f_ann_date": "2026-08-08",
  "source_time_precision": "day",
  "published_at": "2026-08-09T00:00:00+08:00",
  "ingested_at": "2026-08-09T00:02:11+08:00",
  "source_record_id": "src_...",
  "supersedes_observation_id": null,
  "record_status": "active"
}
```

要求：

- 金额用十进制字符串进入契约，禁止 JSON binary float 作为权威值。
- `basis` 至少区分 `instant | discrete | ytd | ttm`。
- `scope` 至少区分 `consolidated | parent`。
- `source_record_id` 指向不可变来源记录。locator 按来源类型变化：文档可用 URI/页/表/行，API 使用 endpoint、字段、脱敏参数、row key、原始 bytes hash；两者都带许可标签。
- provider 返回维度与固定请求维度先完整保留，再映射到规范语义。未知 `report_type/comp_type/end_type` 一律拒绝；`is_calc` 固定请求 `0` 且不从响应猜测，不猜测合并、期间或调整状态。
- 修订以 `supersedes_observation_id` 或明确撤回记录组成链；`update_flag` 不能独自决定先后。互相竞争且没有确定关系的记录返回 `ambiguous_source_version`。
- 原始值与标准化值分开保存，换算本身也产生血缘活动。

### 7.3 `MetricDefinition`

```json
{
  "metric_id": "derived.cashflow.operating.single_quarter_yoy",
  "version": "1.0.0",
  "accounting_standard": "CAS",
  "value_kind": "ratio",
  "calendar": "CN_CALENDAR_YEAR",
  "supported_target_periods": ["Q2"],
  "inputs": [
    {"name": "current_h1", "metric_id": "statement.cashflow.operating_net", "year_offset": 0, "period": "H1", "basis": "ytd"},
    {"name": "current_q1", "metric_id": "statement.cashflow.operating_net", "year_offset": 0, "period": "Q1", "basis": "ytd"},
    {"name": "prior_h1", "metric_id": "statement.cashflow.operating_net", "year_offset": -1, "period": "H1", "basis": "ytd"},
    {"name": "prior_q1", "metric_id": "statement.cashflow.operating_net", "year_offset": -1, "period": "Q1", "basis": "ytd"}
  ],
  "calculation": "operating_single_quarter_yoy",
  "preconditions": [
    {"check": "nonzero_difference", "refs": ["prior_h1", "prior_q1"]},
    {"check": "same_scope", "refs": ["current_h1", "current_q1", "prior_h1", "prior_q1"]},
    {"check": "same_currency", "refs": ["current_h1", "current_q1", "prior_h1", "prior_q1"]},
    {"check": "same_unit", "refs": ["current_h1", "current_q1", "prior_h1", "prior_q1"]}
  ],
  "applicability": {"scopes": ["consolidated"]},
  "missing_policy": "insufficient_data",
  "decimal": {"precision": 34, "rounding": "ROUND_HALF_EVEN", "output_scale": 4},
  "governance": "immutable"
}
```

公式目录使用 JSON，避免仅为 YAML 新增解析依赖。首个函数严格计算 `current = current_h1-current_q1`、`prior = prior_h1-prior_q1`、`yoy = (current-prior)/abs(prior)`；四个输入必须精确匹配目标年份/上年和 Q1/H1，不做最近期猜测。`calculation` 只能引用 core 内人工审核、白名单注册的纯 Python 函数；外部请求不能定义或上传函数。目录内容哈希、函数版本和应用代码版本共同标识一次计算。修改输入、期间逻辑、函数、舍入或适用范围都创建新版本。

MVP 不实现通用公式 AST。只有出现非代码公式作者或第二个执行运行时时，才根据已出现的公式集合设计 DSL；在此之前，AST 会重复 Python 的解析、类型和错误处理能力。

### 7.4 `AnalysisRequest`

```json
{
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
}
```

模型不能提交源数据值、任意公式、SQL 或代码。若只提交自然语言，宿主只能生成候选请求，经过 Schema 和目录校验后才能执行。完整 wire envelope、批量状态和版本规则以 [`PROTOCOL.md`](PROTOCOL.md) 为准；这里的对象只表达金融语义。

### 7.5 `ResultCell`

```json
{
  "result_id": "result_...",
  "status": "ok",
  "key": {
    "entity_id": "cn.company.600519",
    "metric_id": "derived.cashflow.operating.single_quarter_yoy",
    "period": "2026Q2",
    "scope": "consolidated"
  },
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
  "formula": {"id": "operating_single_quarter_yoy", "version": "1.0.0", "content_hash": "sha256:..."},
  "snapshot_id": "snap_...",
  "validation": {"status": "passed", "check_ids": ["pit", "same_scope", "nonzero_denominator"]},
  "provenance": {
    "calculation_id": "calc_...",
    "inputs": [
      {"role": "current_h1", "observation_id": "obs_1", "source_record_id": "src_1", "transform": "identity"},
      {"role": "current_q1", "observation_id": "obs_2", "source_record_id": "src_2", "transform": "subtract_from_current_h1"},
      {"role": "prior_h1", "observation_id": "obs_3", "source_record_id": "src_3", "transform": "identity"},
      {"role": "prior_q1", "observation_id": "obs_4", "source_record_id": "src_4", "transform": "subtract_from_prior_h1"}
    ]
  }
}
```

`status` 是封闭枚举：

- `ok`
- `insufficient_data`
- `ambiguous_entity`
- `ambiguous_metric`
- `ambiguous_source_version`
- `unsupported_metric`
- `not_applicable`
- `validation_failed`
- `source_error`
- `system_error`

只有 `ok` 可以携带权威 `value`。多值结果中每个单元格独立有 key/status/provenance，外层以 `ok | partial | rejected | error` 汇总。领域拒算始终是结构化业务响应，不伪装成 transport error。

## 8. 受约束执行模型

### 8.1 状态机

```text
received
  -> resolved
  -> planned
  -> snapshotted
  -> computed
  -> validated
  -> completed
```

任何阶段都可终止为类型化状态。失败不能被后续 LLM 文本改写成成功。

### 8.2 执行计划

计划不是自由代码，而是由 Harness 根据 MetricDefinition 展开的只读步骤：

1. resolve entity/metric/scope；
2. expand required periods；
3. query observations under PIT policy；
4. freeze snapshot manifest；
5. normalize units/currency；
6. run temporal transforms；
7. call the registered formula function with Decimal；
8. validate；
9. append audit events；
10. render keyed result。

### 8.3 公式执行

首版将每个 `calculation` 映射到人工审核的纯函数。函数只接收已规范化的 typed inputs 和 `decimal.Context`，返回 Decimal 或类型化领域错误；无 I/O、全局状态、动态导入、`eval()` 或宿主回调。

累计转单季、TTM、期间 shift 等通用时间操作可作为普通内部函数复用，但不为它们先建一门解释语言。每个指标显式设置 precision、rounding 和输出 scale。除零、缺失、溢出、单位冲突均返回结构化错误。

MVP 只做同币种计算与单位倍数归一，不自动做汇率转换；跨币种请求返回 `not_applicable` 或要求调用方显式请求一个版本化的 FX 转换指标。

## 9. PIT 与快照

### 9.1 选择算法

原始来源记录先使用不会丢维度的 identity key：

```text
(provider, ts_code, end_date, report_type, comp_type, end_type,
 is_calc, ann_date, f_ann_date, update_flag, raw_row_hash)
```

provider mapper 只有在完整识别上述维度后，才产生规范事实族键。`company_type`、`reporting_variant` 与完整 source dimensions 属于来源版本元数据，不拆分规范事实族；否则“调整后版本显式 supersede 原版本”会成为跨事实键关系：

```text
(entity_id, metric_id, period.start, period.end, basis, scope,
 accounting_standard, unit, currency)
```

每个 Observation 保留原始 source dimensions 和显式 `supersedes_observation_id`/撤回记录。对某一 policy，`known_from` 分别取 `published_at` 或 `ingested_at`；`known_to` 在查询时由同一修订链下一条记录的相应 `known_from` 推导，不通过更新旧行持久化。先筛选 `known_from <= as_of`，再沿当时已知的修订链选择叶节点；存在多个无确定 supersedes 关系的候选叶节点时返回 `ambiguous_source_version`，不得按行号、`update_flag` 或抓取顺序猜测。

把匹配 observation ID、输入 role、两个 knowledge timestamps、查询参数和内容哈希写入 snapshot manifest。首个指标只接受 `CN_CALENDAR_YEAR`、consolidated、YTD Q1/H1 精确期间；单季源行、母公司行、未知/不兼容 report type 均不参与计算。

### 9.2 数据库约束

- 逻辑事实版本不能原地更新或删除。
- 正常公开数据要求 `published_at <= ingested_at`；历史回填仍应满足该关系。唯一窄例外是来源只有日期精度时，`published_at` 表示保守的次日公开生效边界，因而同日抓取可能使它晚于真实 `ingested_at`；只有该时间能被严格验证为 `source_f_ann_date/ann_date + 1 day 00:00 Asia/Shanghai` 才可入库。其他违反关系的记录标记 `provider_time_anomaly` 并隔离。
- `as_of` 与匹配到的 `published_at/ingested_at` 必须同时出现在审计记录中。
- SourceRecord、Observation、Snapshot、ResearchRun 和 audit event 均通过 SQLite trigger 禁止 `UPDATE/DELETE`；Calculation/Result 作为运行响应中的哈希化内容持久化，不另建空泛实体表。修订只追加新行。
- 对 PIT 查询键和 knowledge time 建复合索引。
- 修复版追加 `source_payloads` 保存原始导入对象及摘要，追加 `revision_links` 保存事后审核的 supersedes 关系、审核人、原因和时间；两表同样禁止 UPDATE/DELETE。旧事实行不被改写；缺原始导入证据时拒算，重导入原始 fixture 仅追加可验证证据。
- PIT 比较使用固定微秒 UTC 查询键，兼容旧行的变长时间字符串；不依赖原始 TEXT 字典序。首个指标必须验证实际 Jan 1–Mar 31 / Jan 1–Jun 30 起止日期，不仅匹配 label。

规模增长后可把事实快照迁到 Parquet，用 [DuckDB ASOF JOIN](https://duckdb.org/docs/current/guides/sql_features/asof_join) 做批量 PIT 连接；但金额除法仍留在 Decimal 执行器，因为 DuckDB 文档说明 [DECIMAL 除法会返回近似浮点](https://duckdb.org/docs/current/sql/data_types/numeric)。

## 10. 校验与拒算

### 10.1 校验顺序

1. **Contract**：类型、枚举、必填字段、十进制格式。
2. **PIT**：没有使用 `as_of` 后的数据或修订。
3. **Semantic**：实体、期间、合并/母公司口径、币种、单位一致。
4. **Formula**：版本存在、适用范围正确、precondition 成立。
5. **Accounting**：资产≈负债+权益等可适用恒等式。
6. **Output**：每个值键唯一、无额外值、精度和展示符合定义。
7. **Anomaly warning**：极端同比、异常单位或跨期跳变，只告警不擅自改值。

### 10.2 错误等级

- `error`：PIT 泄漏、口径冲突、公式不适用、来源缺失；阻止输出。
- `warning`：统计异常、弱质量来源、接近容差边界；可输出但显式携带警告。
- `info`：使用了 public 而非 system knowledge policy 等研究元数据。

LLM critic 若以后增加，只能创建 warning/建议，不能覆盖确定性 error 或直接修改数值。

### 10.3 跨入口错误、取消与资源边界

结果状态只表达金融领域结论。未授权、限流、超时、取消、无此 snapshot 和协议错误使用顶层错误码：

```text
invalid_request | unsupported_protocol | permission_denied | rate_limited |
timeout | cancelled | snapshot_not_found | replay_artifact_mismatch |
source_unavailable | system_error
```

CLI、MCP stdio 与未来 HTTP 使用同一映射表。one-shot 进程收到终止信号时回滚当前事务；若宿主直接强杀而无法返回 JSON，调用方按无响应 transport failure 处理。取消/超时在最终事务 gate 前生效时不得提交 Result 或可发布 snapshot；gate 已通过后的取消不能撤销已提交运行，且客户端可能收不到响应。领域 `source_error/system_error` 只用于已进入分析后的单元格失败；请求级故障使用顶层 error。

v1 固定上限：单请求一个实体、最多 16 个 targets、最多 8 次上游调用、默认 60 秒执行期限、序列化响应不超过 1 MiB。操作者可以调低，模型不能调高；超过上限返回结构化顶层错误且不提交部分结果。

## 11. 血缘、审计与可复现

最小关系：

```text
SourceRecord -> Observation -> Snapshot
Snapshot + MetricDefinition -> Calculation -> Result
Agent/CLI -> ResearchRun -> StepRun
ValidationResult -> Calculation/Result
```

每次运行保存：

- `run_id/request_id/parent_run_id`；
- 调用者 correlation、package version 与不可变 build digest（模型/提示版本由宿主保存）；
- `as_of` 与 knowledge policy；
- 规范化请求哈希；
- snapshot manifest 及内容哈希；
- 公式/目录版本与哈希；
- 校验结果；
- 最终 keyed results；
- 开始/结束时间、资源用量与错误分类。

中间 Decimal 不在 MVP 永久逐步复制。`explain/replay` 使用已冻结 snapshot、公式版本、package version 和 build digest 确定性重算步骤；当前工件、registry 或 snapshot hash 不一致时返回 `replay_artifact_mismatch`，不得用新代码伪装历史重放。只有监管留痕或无法重算的外部操作出现时，才把完整 execution trace 升为审计数据。

哈希输入统一使用 UTF-8、对象键排序、紧凑分隔符、禁止 NaN/Infinity、Decimal 只用规范字符串；数组顺序保留业务语义。哈希算法及 canonicalization 版本与摘要一同保存。

重复执行相同的请求、代码、目录和 snapshot 时，规范化结果哈希必须一致。若不一致，评测直接失败。

SQLite trigger 保证的是应用运行边界内的 append-only，不等同于对数据库管理员防篡改。只有出现监管级防篡改要求时，再增加哈希链、签名或外部 WORM 存储。

运行 telemetry 先用结构化 JSON 日志与计时器，通过 `run_id/step_id` 关联 audit。出现多进程后再映射到 [OpenTelemetry Trace/Span](https://opentelemetry.io/docs/specs/otel/trace/api/)；telemetry 可以采样，audit 不可以。

## 12. 安全与治理

- 数据源、许可和可使用目的进入 source contract；无许可标签的数据不可进入受管运行。
- 外部文档全部视为数据，不把其中的自然语言指令当作系统指令执行。
- 公式只允许调用 core 的白名单纯函数；无文件、网络、进程和动态导入能力。
- 数据适配器不能直接写结果表，只能追加 SourceRecord/Observation。
- 基础指标、PIT、单位、日历、公司行动和风险规则标记 `governance=immutable`，变更需要人工评审和新版本。
- mutable 参数（如 lookback、阈值、权重、路由）必须有明确允许列表、范围和回滚版本。
- 密钥不进入请求、快照、日志或审计 payload；审计只保存 secret reference。
- 远程入口把 authenticated principal 映射为 tenant；run/snapshot/source 均带 tenant 归属，`financial_explain(run_id)` 必须逐请求检查 tenant/run ACL。`run_id` 的不可猜性不构成授权。
- 所有外部 JSON 请求采用机器校验契约，可用 [JSON Schema 2020-12](https://json-schema.org/draft/2020-12/json-schema-core) 发布。

## 13. 评测设计

### 13.1 三层测试

| 层级 | 内容 | 目的 |
|---|---|---|
| 组件 | Decimal、时间转换、单位、PIT 版本选择、注册公式、状态流 | 证明确定性基础正确 |
| 黄金/对抗 | 人工核验的完整财报案例；缺期、相邻期干扰、重述、母/合并混淆、零分母 | 证明领域闭环与拒算 |
| 外部基准 | FinIndices；随后按范围接 FinQA/TAT-QA/DocFinQA | 与公开任务比较并发现盲点；抽取器与 Harness 分阶段计分 |

### 13.2 指标

- entity resolution accuracy 与 ambiguity recall；
- source-cell extraction accuracy（仅在被测配置包含抽取器时）；
- period selection accuracy；
- formula selection accuracy；
- keyed cell exact/numeric-tolerance accuracy；
- whole-table exact accuracy；
- extra/missing cell rate；
- insufficient-data precision、recall、F1；
- PIT leakage count；
- provenance completeness；
- deterministic replay rate；
- validation false-positive/false-negative rate；
- 每次请求延迟、数据调用数、模型 token/成本（如使用模型）。

### 13.3 MVP 发布门槛

- PIT 泄漏：`0`。
- 黄金集 Decimal 计算与期间转换：`100%`。
- keyed 输出无额外/重复键：`100%`。
- `ok` 结果的公式、快照、源记录血缘完整率：`100%`。
- 相同输入/版本/快照重放哈希一致率：`100%`。
- 缺失必要期间的对抗集不得产生权威数值：`100%`。
- 外部 benchmark 先建立基线，不用单一总分作为发布门槛；按能力轴跟踪回归。

## 14. 最小技术方案

### 14.1 技术栈

- Python：领域逻辑、`decimal`、`datetime/zoneinfo`、CLI。
- 标准库 dataclass/显式校验：首个固定契约无需运行时模型框架；公共契约是 checked-in JSON Schema。
- SQLite：实体/指标索引、Observation 版本、快照 manifest、审计 ledger。
- JSON：指标定义和 fixture；不先引入 YAML。
- 官方 MCP Python SDK：只承载下游 server；one-shot/fixture core 不依赖 MCP。
- 标准库 `urllib.request` + `json(parse_float=Decimal)`：仅用于固定 Tushare HTTPS endpoint；解析前校验大小并保存 raw hash。
- `unittest`：首版测试；达到参数化测试痛点后再引入 pytest。

不先安装 tushare SDK、pandas、Polars、DuckDB、Airflow、Dagster、MLflow、Kafka、Neo4j、OpenLineage backend 或 FastAPI。

### 14.2 建议目录

```text
fin-harness/
├── pyproject.toml
├── src/fin_harness/
│   ├── protocol.py
│   ├── core.py
│   ├── store.py
│   ├── tushare_source.py        # 唯一首版 vendor HTTPS integration
│   └── cli.py
├── registry/metrics/*.json
├── protocol/v1/*.schema.json
├── tests/
│   ├── test_vertical_slice.py
│   └── fixtures/
└── adapters/mcp.py              # 安装 optional extra 后启用
```

这是目标布局，不应一次生成空壳文件。按纵向切片创建实际需要的文件。

## 15. 实施路线图

### M0：冻结公共协议与一个样例

交付：

- 固定中国 A 股/CAS、`CN_CALENDAR_YEAR` 和本地单用户 fixture 范围；建立许可矩阵，未确认真实数据许可前不得进入 live/多人服务；
- 选择首个代表性指标和 2 个 fixture：成功、缺数据拒算；
- checked-in `fin-harness/v1` request/response JSON Schema；
- 冻结 Observation、MetricDefinition、Request/Result 状态枚举；
- 为 fixture 人工记录来源、计算程序和拒算条件。

退出条件：领域审核者能根据契约独立得到相同答案；JSON fixtures 可机器校验。

### M1：确定性内核

交付：

- 标准库内部 contracts 与 `fin-harness invoke`；
- 受信任纯函数 + Decimal；
- 仅实现 Q2 的四输入精确期间展开、YTD→单季与同比；
- 指标 JSON 加载与内容哈希；
- 单元测试。

退出条件：黄金公式/期间测试 100%，没有 `eval` 或自由代码路径。

### M2：PIT 数据与快照

交付：

- SQLite schema 与 JSON fixture importer；
- Tushare HTTPS response fixture、原始 bytes/规范行 hash 与 cashflow 字段映射；
- observation 版本、重述和两个 knowledge policy；
- snapshot manifest；
- PIT 对抗测试。

退出条件：所有未来数据陷阱被阻断；回填/live 语义均有明确测试。

### M3：运行、验证与最小审计

交付：

- 执行状态机；
- hard validator、结构化错误和 warnings；
- append-only audit 与逐值 provenance，不复制完整中间 trace；
- CLI `invoke/explain/replay`。

退出条件：任一 `ok` 结果可从 CLI 反查到源记录并重放；审计更新/删除被 DB 拒绝。

### M4：通用插件接口与评测

交付：

- MCP stdio adapter，只暴露两个模型工具；
- OpenCode、DeepSeek Harness 与 ChatGPT desktop 配置样例；
- 首先完成真实 MCP stdio 子进程 smoke test；ChatGPT desktop 复用同一 stdio 配置，宿主 UI 验收不阻塞可发布内核；随后做 Mosaic/Pi 薄 adapter 的 fixture 合约测试；
- 单值/多值 keyed result；
- 黄金/对抗评测报告；
- FinIndices loader/scorer 和额外 transform 不属于首切片发布门槛。

后续若加入 FinIndices，adapter 只提供 dataset loader、请求映射和 scorer；端到端表格抽取需要另接被测 extractor/agent，不能把 `gt` 作为输入。退出条件：达到第 13.3 节发布门槛；模型替换不影响固定结构化请求的确定性结果。

### M5：受控试点

交付：

- 只有许可矩阵通过后，才使用 token 持有者受控凭据接入 Tushare HTTPS API；
- 先在不保留数值的临时数据库中完成一次四期间真实全链路烟测；生产使用前再执行多日影子运行；
- 记录歧义、失败、延迟和人工复核结果；
- 用测量结果决定是否增加 DuckDB、OTel 或第二市场；由产品目标决定是否进入 ChatGPT web 发布阶段。

v0.1 退出条件：真实四期间导入、分析、解释与重放闭环成功，不在仓库或报告中保留供应商数值，所有失败可归类并定位。生产服务退出条件另加：多日影子运行无 PIT 泄漏和不可解释的权威值。

### M6：ChatGPT web 分发（按需）

仅当 ChatGPT web 是明确交付目标时执行：

- 为已有 MCP tool registry 增加 Streamable HTTP transport，不创建第二套工具实现；
- 部署稳定公网 HTTPS `/mcp`，配置 secret manager、限流、日志脱敏和所需 OAuth 2.1；
- 固定 MCP SDK/协议兼容版本，并用 negative/interop tests 覆盖 Origin、协议版本、OAuth discovery、session 过期和断线取消；
- 在 ChatGPT Developer Mode 注册 MCP 连接，并生成只含 manifest 与 `.app.json` 映射的薄插件包；
- 用同一组 fixtures 验证 stdio/HTTP 结果等价，再完成 workspace 或公开分发测试；
- 在公开发布前确认 Tushare 数据许可、配额归属和服务条款。

退出条件：代表性 ChatGPT 对话能稳定选对两项工具，stdio/HTTP 规范化结果一致，未授权请求在 core 之前被拒绝，任何输出和日志均不泄漏凭据。

## 16. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 指标名称相同但定义不同 | 数值看似合理、实际不可比 | metric_id + version + accounting standard + applicability |
| 公告/入库时间缺失 | PIT 回测不可信 | 禁止默认猜时间；返回 insufficient 或标记 public-only replay |
| 重述覆盖原数据 | 无法复现历史结论 | append-only observation versions + snapshot |
| LLM 选错实体/口径 | 错误被自然语言掩盖 | 候选解析、歧义拒绝、严格 key |
| Decimal/单位处理错误 | 金额和比例偏差 | 十进制字符串、单位类型、显式舍入、黄金测试 |
| 整表全对率掩盖原因 | 无法定位模型/系统弱点 | 逐单元格、键控、阶段指标同时报告 |
| 过早平台化 | 交付慢、边界难改 | 模块化单体；根据测量触发升级 |
| 数据许可不清 | 无法合法复现/共享 | source contract 强制许可和用途标签 |
| audit 与日志混用 | 采样/清理导致证据丢失 | 两套保留策略，以 run_id 关联 |
| Tushare 字段漂移或 token 泄漏 | 取数错误或 secret 暴露 | 显式 fields、fixture 合约、fail closed、环境注入与日志脱敏 |
| ChatGPT web 远程化混淆本地/服务端信息集 | `system` 回放名义相同、实际快照不同 | 分离存储命名空间；只按各部署真实 `ingested_at` 声称 system knowledge |

## 17. 已作出的默认决策

| 决策 | 默认选择 | 理由 |
|---|---|---|
| 首要场景 | 离线研究与评测，不含交易执行 | 最先验证可靠性和可复现性 |
| 首个任务族 | 上市公司财报衍生指标 | 一条切片覆盖共享对话的关键风险 |
| 首个数据源 | Tushare 固定 HTTPS API；fixture 先行 | 在 float 前保存原始响应并按 Decimal 解析；MCP 保留给 Agent-facing 边界 |
| 部署 | Python 单体 + one-shot CLI + MCP stdio；ChatGPT web 按需加同 server 的 HTTP profile | 通用宿主边界，不拆金融 core |
| 存储 | SQLite | 标准库、事务、约束、足够的首版规模 |
| 权威算术 | Python Decimal | 可显式控制精度与舍入 |
| 公式格式 | JSON metadata + 版本化白名单纯函数 | 少一门 DSL；仍无自由代码入口 |
| 项目代码许可 | Apache License 2.0 | 允许跨宿主复用并保留专利授权；供应商数据许可独立处理 |
| 实体解析 | canonical ID + alias + 歧义拒绝 | 先保证正确，再提高召回 |
| 血缘 | 内部关系表/JSON；标准映射留给 exporter | 支持审计且不为假想消费者预留字段 |
| Claim Graph | 不进入 MVP | 尚无已验证的下游决策闭环 |

## 18. 开工前必须确认的决策

1. Tushare 许可是否允许真实数据的本地长期快照？多人服务、缓存和再分发分别需要什么书面许可？
2. 后续指标扩展由谁承担领域审核？
3. 生产审计保留期限和组织级访问控制是什么？
4. Mosaic 与 Pi adapter 的长期维护归属和发布节奏是什么？
5. ChatGPT 首个目标是 desktop 本地使用、workspace web 使用，还是公开插件？后两者需要远程部署、认证与数据许可决策。

首版已固定中国 A 股/CAS、Q2 经营现金流单季同比和显式 `knowledge_policy`。其余问题未确认前，可以安全完成 fixture-only 的 M1–M4；不得伪造真实许可、多人服务或生产 SLO。

## 19. 下一步

先做一个薄而完整的演示：

```text
fixture 财报
  -> fin-harness/v1 analyze request
  -> PIT snapshot
  -> 2026Q2/2025Q2 单季度经营现金流
  -> YoY Decimal 计算
  -> 缺 Q1 时拒算
  -> explain/replay 展示完整血缘
```

同一 fixture 先通过 direct core 与 `fin-harness invoke`，再接 MCP，并首先用 ChatGPT desktop 验证本地 stdio。只有明确需要 ChatGPT web 时才进入 M6；不要为“未来也许要用”预先增加公网服务、OAuth、UI 或多租户平台。
