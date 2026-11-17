# 定义 cdcwater

本文件是字段、时间、金额、窗口与去重键的唯一书面定义。页面只显示列名与数值，定义看这里。

## 1 事件信封

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `table` | string | 是 | 表名，必须在 8 张登记表内：orders / order_items / customers / payments / shipments / products / inventory / refunds |
| `pk` | string | 是 | 主键。**全程字符串**：前导零 `00123` 与超过 2^53 的整数都按原样保存 |
| `version` | int | 是 | 版本号，正整数；决定同键内的应用顺序 |
| `op` | enum | 是 | `upsert` / `delete`；`delete` 写墓碑 |
| `event_time` | string | 是 | 源库事务时间。`Z`、`+08:00` 与无偏移三种写法都接受，入库统一 UTC |
| `schema` | enum | 否 | `v1` / `v2`，缺省按 `v2`；只影响列名映射 |
| `payload` | object | 是 | 业务字段，见第 2 节 |

## 2 payload 字段

| 规范列名 | 类型 | 必填 | v1 旧列名 | 说明 |
|---|---|---|---|---|
| `ref` | string | 否 | `ref_no` | 业务引用 |
| `status` | string | 是（upsert） | `st` | 状态；`delete` 可为空 |
| `amount` | decimal | 否 | `amt` | 金额，单位元，刻度 0.01 |
| `qty` | int | 否 | `qt` | 数量 |

别名规则：v1、v2 都接受旧列名（读兼容）。同一个规范列名被新旧两种写法同时赋值且取值不同，
判 `E_ALIAS_AMBIGUOUS` 并隔离；取值相同按一条处理。契约外的列判 `E_UNKNOWN_FIELD`。

## 3 数值与金额

- 金额一律 `Decimal`，量化到 `0.01`，舍入 `ROUND_HALF_UP`；禁止 `float` 累加。
- 输入允许千分位与全角数字：`1,200.50` / `１２００．５０` 都归一为 `1200.50`。
- 空值哨兵（`-`、`--`、`N/A`、`null`、`未知`、`无`、空串…）→ `null`，并记 `W_SENTINEL_NULL`；
  哨兵与 `0.00` 是两种语义：`0.00` 计入合计，哨兵不计入。
- 对外 JSON 里金额是两位小数字符串；CSV 里也是两位。

## 4 时间

- 入库：UTC，ISO8601 带 `Z`（例 `2026-11-15T08:52:00Z`）。
- 显示：`Asia/Shanghai (+08:00)`，页面用 `YYYY-MM-DD HH:MM:SS`。
- 无偏移的时间按 `+08:00` 解释，并记 `W_TIME_NO_OFFSET`；非法时间（`25:10`、斜杠日期）判
  `E_BAD_TIME` 隔离。

## 5 水位线与窗口（关键定义）

- **参考时刻**：本次运行的参考时钟。默认取运行时的 UTC 墙钟；可用参数 `reference_now`
  （环境变量 `CDCWATER_REFERENCE_NOW`）固定成 ISO8601 时刻以复算同一结果。
- **时钟容差**：`future_tolerance_minutes`（默认 10，环境变量 `CDCWATER_FUTURE_TOLERANCE`）。
- **暂缓（held）**：`event_time > 参考时刻 + 容差` → `W_FUTURE_HELD`。整条记录不应用：
  不推进水位、不进正式快照、不参与去重与冲突判定，只在事件表与核对页留证据。
  等于容差线照常应用（与半开口径一致）。参考时钟追上后重新采信即放行。
- **水位线** = 已应用事件的最大 `event_time` − 10 分钟，且**不超过参考时刻**（上限只压水位，
  不改事件本身的迟到比较口径）。只由已应用事件推进：重复、冲突、隔离、暂缓都不推进。
- **迟到**：`event_time < 水位线` 判迟到（`W_LATE_ARRIVAL`），**但仍然应用**；等于水位线不算迟到。
- **快照窗口**：`event_time ≤ as_of`，`as_of` = 本次水位线。含等号一侧。
- **待定窗口**：`(as_of, +inf)`，半开。两窗口互不重叠、合起来覆盖全部已应用事件。
- 同一键既有快照内事件、又有待定事件时，快照取 `≤ as_of` 的最大版本，并记 `drift`。

## 5.1 发布裁决

| 条件 | 批次状态 | 正式快照 |
|---|---|---|
| 有已应用事件 | `done` | 本批快照即正式快照（暂缓记录不参与） |
| 全部记录被暂缓 | `held_only` | **保持上一版**，不写空快照 |
| 无可应用事件且已有正式快照 | `empty_only` | **保持上一版**（恢复中断常表现为读成 0 行） |
| 运行失败 | `failed` | 保持上一版；失败不降级 `done` 批次 |

「正式快照」= 状态为 `done` 的最新批次。页面、`/api/snapshot`、快照导出与清单都以它为准；
`held_only` / `empty_only` / `failed` 只提供证据，供核对页复核。

## 6 去重键与冲突

- **去重键** = `(table, pk, version, op, event_time_utc, 规范化 payload)` 的 sha256。
  用规范化 payload（金额两位、字段排序）而不是原始行哈希，所以 `120.5` 与 `120.50`
  视为同一事件；用 UTC 时刻而不是原始字符串，所以两种时区写法视为同一事件。
- **版本冲突** `C_VERSION_CONFLICT`：同键同版本出现两种以上签名 → 整组不应用。
- **墓碑复活** `C_TOMBSTONE_RESURRECT`：某键存在版本 `d` 的 `delete`，之后到达的 `version < d`
  的 `upsert` 不应用；删除之前到达的低版本照常应用（会被删除覆盖，结果一致）。
- **优先级**：`quarantine > held > conflict > duplicate > apply`，判定由集合运算得出，与遍历顺序无关。
  暂缓在冲突之前分流，所以未来时间不会伪造冲突、也不会把同一键的正常版本拖进冲突表。

## 7 空态三态

| 状态 | 判定 | 结论 |
|---|---|---|
| 0 字节文件 | 没有任何行 | `S_EMPTY_INPUT`，不算失败 |
| 只有表头 | CSV 有表头无数据行 | `S_HEADER_ONLY`，不算失败 |
| 只有空行 | 有行但全空白 | `E_ALL_BLANK`，文件级失败 |

## 8 计数与四账

| 计数 | 含义 |
|---|---|
| `raw_lines` | 数据行（不含注释行、空行、重复表头） |
| `applied` | 通过契约与去重冲突、进入折叠的事件 |
| `duplicates` | 同签名重复到达（保留首条） |
| `conflicts` | 版本冲突 + 墓碑复活 |
| `quarantine` | 契约不通过（字段、类型、必填、表名、时间、截断、列数） |
| `held` | 时间超前参考时钟、整条暂缓的记录（不进水位/快照，证据保留） |
| `held_amount` | 暂缓记录金额合计（仅展示，不进任何正式合计） |
| `keys_total` | 有已应用事件的键 |
| `snapshot_keys` | 进快照的键（含墓碑） |
| `active_keys` / `tombstone_keys` | 快照内活跃 / 墓碑 |
| `pending_only_keys` | 只有 `as_of` 之后事件的键 |
| `amount_snapshot` / `amount_pending` / `amount_drift` / `amount_applied` | 四个金额位，见 DEPTH.md 的金额账 |

## 9 运行清单字段

`run_id`（`CDC-时间戳-随机`）、`batch_id`（`B-批次键`）、`batch_key`（输入摘要 + 规则版本 +
参数 + 规则参数 + 暂缓集合签名 的 sha256 前 10 位）、`input_digest`（每个文件 `name:sha256` 的摘要）、
`inputs[]`（`name/bytes/sha256/lines/encoding/kind/declared_rows/status/code`）、`params`、
`watermark_utc`、`max_event_time_utc`、`reference_utc`、`future_horizon_utc`、`stages[]`、
`counters`、`reconcile`。

同一 `input_digest` 且暂缓集合不变时重跑命中同一 `batch_key`，返回原批次、不新增行；
参考时钟追上导致放行记录时暂缓集合改变，会按新结果落成新批次（旧批次保留证据）。

## 10 落库核对（`/api/audit`）

| 字段 | 含义 |
|---|---|
| `official` | 正式快照所属批次（最新 `status=done`） |
| `attempt` | 最新一次尝试（可能 `held_only` / `failed`） |
| `held[]` | 被暂缓的记录：行号引用、键、事件时间、原始时间、超前时长、可放行标记 |
| `releasable_count` | 按当前参考时刻已可放行的条数（重跑即放行） |
| `audit.consistent` | 落库核对结论；不一致为 `E_RECOVERY_MISMATCH` |
| `audit.snapshot.missing/extra/changed` | 与「按已落库事件重算」相比的差异键 |
