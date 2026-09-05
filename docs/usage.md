# 使用指南

[README](../README.md) · [宿主接入](integrations.md) · [协议](../PROTOCOL.md) · [验证记录](../EVALUATION.md)

本文对应包含 PR #1 的合并主线。命令从仓库根目录运行，示例面向 Bash/zsh；不依赖模型账户即可完成合成数据测试。

## 安装与数据库配置

优先按 [README](../README.md#快速上手) 使用 uv 和源码。已有 Python 虚拟环境也可从仓库安装：

```bash
python -m pip install -e .          # CLI / SQLite / Tushare 导入
python -m pip install -e '.[mcp]'    # 另外需要 MCP 时
```

上述为二选一的安装方式；不假设同名 PyPI 包已发布。源码包含测试/宿主样例；wheel 携带公共 Schema 和指标定义，但不包含完整测试及宿主示例目录。

`--config PATH` 只接受两个 JSON 字段，见[示例](../examples/config.json)：

| 配置 | 用途 | 未指定时 |
|---|---|---|
| `database` | SQLite 路径，父目录自动创建 | `FIN_HARNESS_DB`，否则当前目录的 `fin-harness.sqlite3` |
| `metric_registry` | 已审核指标定义的路径 | 包自带的固定定义 |

配置中的显式 `database` **优先于** `FIN_HARNESS_DB`。相对路径按进程工作目录解析，不按配置文件目录解析。`--config` 写在子命令之后；不支持 token、provider、timeout 等额外配置键。

不使用配置文件时，可选择独立数据库：

```bash
export FIN_HARNESS_DB="$PWD/var/demo.sqlite3"
uv run fin-harness import-fixture tests/fixtures/source-success.json
uv run fin-harness invoke < protocol/v1/fixtures/analyze-success.request.json
uv run fin-harness doctor --json
```

继续 explain/replay 时保持相同环境。`FIN_HARNESS_CONFIG` 不是 CLI 通用设置，仅由 Pi 扩展转成 `--config`；`FIN_HARNESS_BIN` 也只用于 Pi。宿主进程与手工 CLI 必须指向同一个库。

## 命令与结果

| 命令 | 用途 | 面向模型 |
|---|---|---|
| `invoke` | stdin 收一个 analyze/explain envelope，stdout 返回一行 JSON | 是，供薄适配器调用 |
| `mcp` | 发布 financial_analyze / financial_explain | 是 |
| `import-fixture PATH` | 导入受信任合成/原始来源 fixture | 否 |
| `import-tushare CODE YYYYMMDD` | 固定 cashflow HTTPS 导入 | 否 |
| `link-revision NEW OLD` | 人工核验后的追加修订关联 | 否 |
| `explain RUN_ID` | 查看已保存的计算或拒算 | 操作者也可使用 |
| `replay RUN_ID` | 按保存的证据与构建重放 | 否 |
| `doctor` / `capabilities` | 确认路径、构建、目录和固定限制 | 否 |

`--json` 可用于 explain/replay/doctor/capabilities/import 命令；当前这些命令默认也输出 JSON。`invoke` 没有 `--json` 参数。完整参数用 `uv run fin-harness COMMAND --help` 查看。

一次 analyze 只允许一个实体，最多 16 个不重复 targets。核心所需信息：

| 字段 | 例子 | 含义 |
|---|---|---|
| `entity` | `TEST001.CN` | 已导入的精确代码、canonical ID 或独立别名 |
| `targets[].metric_id` | `derived.cashflow.operating.single_quarter_yoy` | 已审核目录中的指标 |
| `targets[].period` | `2026Q2` | 目标单季度，不是源表累计期间 |
| `targets[].scope` | `consolidated` | 当前唯一适用口径 |
| `as_of` | `2026-08-15T12:00:00+08:00` | 带时区的信息截止时点 |
| `knowledge_policy` | `system` 或 `public` | 信息集定义，必须显式选择 |

不要传入“测试股份（TEST001.CN）”这样的拼接文本，除非它本身已登记为独立别名。未知实体也可能返回 `ambiguous_entity`；该状态不一定意味着查到了多个公司。

消费 analyze 时先读顶层 `status`，再逐项检查 `results[].status`。只有 `ok` 项有 `value`；`partial` 表示成功和拒算并存。金额/比率保持十进制字符串，不用浮点数二次计算展示值，也不要依赖数组位置匹配指标，使用 `result_id` / `key`。

explain 顶层 `ok` 仅表示解释请求成功。成功项有 inputs/formula/steps，拒算项则保留原来的 status/error，不伪造数值；不能仅看顶层 ok 就认为财务可计算。`error.details` 可包含缺失角色或至多 16 对冲突 observation/source ID。`result_ids` 筛选同样适用于拒算。

`invoke` 退出码为：`0` 表示成功生成领域响应（包括拒算），`2` 表示无效请求/不支持的协议，`70` 表示其他请求级或系统错误。非零退出仍可能携带合法 JSON；解析后再决定重试，不从 stderr 猜业务结果。导入上游不可用另返回 `69`；参数解析失败可能只有 argparse 的 stderr。

## 两种时间策略

| 策略 | 选择依据 | 适合回答 |
|---|---|---|
| `system` | 本库实际 `ingested_at <= as_of`，修订审核也受时间约束 | 本系统当时实际能使用什么？ |
| `public` | 保守公开生效时间 `published_at <= as_of` | 现有归档可重建的当时公开信息是什么？ |

导入今天获取的旧财报，不会生成本系统过去已知的记录。历史 system 拒算时，不得回填虚假的入库时间，也不能未经用户同意改成 public 来得到数值。public 是基于已归档版本的公开历史重建，不保证供应商提供了所有历史版本。

Tushare 来源优先采用实际披露日 `f_ann_date`，否则采用 `ann_date`；只有日期时从上海时区次日 00:00 起视为公开。两个披露日期都缺失时当前导入器直接拒绝该行。日精度不能用来声称公告当日盘中已知。

当前仍有非法 offset 分钟溢出的已知问题。调用方应提供经过合法性校验的 RFC3339 时间，优先输出标准 `Z` 或合法 `+08:00`；这只是使用约束，不替代核心缺陷修复。

## Tushare 真实数据

先确认账户有 [cashflow 接口](https://tushare.pro/document/2?doc_id=44)权限，以及本次本地存储、解释输出和交给模型处理的权利。本项目不承诺积分、配额或许可额度；`--acknowledge-license` 只是操作者确认，不会授予数据权利。

当前实现使用标准库直接 POST 固定 HTTPS endpoint，不安装 tushare SDK/pandas，也不调用 Tushare MCP。它先计算响应字节哈希，再按 Decimal 解析所选字段，保存规范化行及导入证据。它不是保留完整 HTTP 响应字节的网络档案服务。

以下以 `600519.SH` 的 2025Q2 为例，四个必需源期间为 `20240331`、`20240630`、`20250331`、`20250630`。token 仅通过进程环境传给导入器；不要把真实 token 写进命令文字、配置文件、聊天、fixture 或 Git。

```bash
export FIN_HARNESS_DB="$PWD/var/tushare.sqlite3"
export TUSHARE_TOKEN
TUSHARE_TOKEN="$(python -c 'import getpass; print(getpass.getpass("Tushare token: "))')"
for period in 20240331 20240630 20250331 20250630; do
  uv run fin-harness import-tushare 600519.SH "$period" \
    --entity-id cn.company.600519 \
    --license-label YOUR_REVIEWED_LICENSE_LABEL \
    --acknowledge-license || break
done
unset TUSHARE_TOKEN
```

替换许可标签；每次命令只获取一个标的的一个期间，没有自动分页或重试。任一步失败会停止此循环；各期间独立提交，前面成功的导入不会回滚。确认四期都成功后才分析，不把空响应当作数据齐全。重复导入相同行保留首次入库时间，冲突值不会静默覆盖。

以下请求使用导入完成后的当前 system 时点，且不携带 token 或源值：

```bash
uv run python -c '
import json
from datetime import datetime, timezone
print(json.dumps({
    "protocol": "fin-harness/v1", "operation": "analyze", "request_id": "local-tushare-01",
    "request": {"entity": "600519.SH", "targets": [{
        "metric_id": "derived.cashflow.operating.single_quarter_yoy",
        "period": "2025Q2", "scope": "consolidated"}],
        "as_of": datetime.now(timezone.utc).isoformat(), "knowledge_policy": "system"}}))
' | uv run fin-harness invoke
```

成功后使用返回的 run_id 调用 explain/replay。若仅 update_flag 不同，内核在 PIT/修订筛选后验证所选财务值、维度、披露时间、许可和原始证据，等价时选择稳定展示代表，同时将其他来源固定进快照。analyze 带 `equivalent_source_records` 警告，explain 在对应输入下列出 `equivalent_sources`，不重复计入金额。

金额、日期或口径不同仍可能 `ambiguous_source_version`。不要删行、盲选 flag=1 或编造修订关系来绕过拒算。一次真实烟测通过不保证后续供应商响应一定可计算。

## 修订、升级和重放

真正的修订关系必须基于独立审核，不能由金额相同或 update_flag 推断。已有两个版本时，由操作者指定新旧 observation ID：

```bash
uv run fin-harness link-revision OBS_NEW OBS_OLD \
  --reviewer YOUR_NAME --reason "verified amended filing"
```

使用前确认当前数据库；命令追加不可变审核关系，不更新来源行。当前并发关联仍可能成环，在修复前不要并发执行 `link-revision`。`system` 从实际审核时间起应用关联；`public` 可使用后验审核来重建已经披露的版本。

升级前备份数据库并保留原代码/registry 构建。打开 schema v1 旧库时会追加 evidence/revision 表升级至 v2，不重写原始事实；缺原始 import payload 的旧行会拒算。只能重新导入**原始** fixture 追加可验证证据；若原始证据丢失，应新建库并明确新导入时间，不能编造旧证据。不要用旧二进制写升级后的库。

`replay` 不会重新查询供应商。成功 run 按固定快照重算，核验全部等价来源及 source/payload/observation、request/response、formula/build 和 manifest；拒算 run 的 `match=true` 只证明保存的拒算结果一致，不等于完成金融数值重算。更换执行构建后，旧 run 可能返回 `replay_artifact_mismatch`；恢复原构建或重新 analyze，不要绕过哈希门禁。

## 常见问题

| 现象 | 检查与处理 |
|---|---|
| 导入过但 insufficient_data | doctor 核对同一数据库；确认四期齐全、system 时点晚于入库；看 missing_roles |
| ambiguous_entity | 使用独立代码或已登记别名，避免名称与代码拼接 |
| not_applicable | 当前指标仅 Q2 / consolidated / CAS |
| ambiguous_source_version | 检查 error.details 候选，核验实际版本，不删除拒算门禁 |
| explain 的 results 为空 | 检查筛选 result_ids；旧构建的 explain 曾跳过拒算项 |
| snapshot_not_found | run_id 不存在，或连接了另一数据库/租户 |
| replay_artifact_mismatch | 代码/公式构建变化或证据不一致；不要修改数据库强行匹配 |
| source_unavailable | 在导入阶段检查环境 token、网络、权限及字段漂移；不要把 token 发给模型 |
| MCP 启动失败 / 找不到 mcp 模块 | 安装 mcp extra，检查绝对执行路径、cwd 和写库权限 |
| Pi spawn failure | FIN_HARNESS_BIN 必须是可执行文件路径，不能是含参数的整条命令 |
| HTTP 非 loopback 启动失败 | 这是预期安全限制；不要用隧道/反向代理公开未认证接口 |

本地权限边界是操作系统、宿主及数据库文件权限；SQLite append-only 触发器不等于能防数据库管理员篡改。多用户远程认证、数据生命周期治理和生产 SLA 未交付。
