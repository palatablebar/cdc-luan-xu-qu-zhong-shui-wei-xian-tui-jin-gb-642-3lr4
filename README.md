# cdcwater · CDC 乱序去重与水位线推进

订单库变更流（CDC）每天 120 万条事件，8 张表。下游拿到的是补数、重投、乱序混在一起的
导出文件：同一版本可能来两次、可能带不同内容、删除之后还会飘来一条旧更新。这个工具把
一批导出采信进来，按版本折叠成「每个键一个状态」，并给出水位线、迟到条数、冲突与隔离
清单，全部可导出复核。

## 能力

- 采信 JSONL 与 CSV 两种导出格式，保留 `文件名:行号` 与原始文本；
- 编码回退（utf-8 → gb18030），声明与实际不符会留原因码；重复表头、列数不齐、尾行截断各自判定；
- v1/v2 列名演进按别名归一，契约外字段隔离，新旧列名同时出现判歧义；
- 完全重复按「规范化全行签名」去重，时区写法不同但同一时刻也合并且注明；
- 同键同版本不同内容判 `C_VERSION_CONFLICT`，墓碑之后到达的低版本 upsert 判
  `C_TOMBSTONE_RESURRECT`，两条都不应用；
- 时钟保护：`event_time > 参考时刻 + 容差`（默认 10 分钟）的记录整条**暂缓**
  （`W_FUTURE_HELD`），不推进正式水位、不进正式快照、不参与去重冲突，只在核对页
  留证据；参考时钟追上后重新采信即放行，同一来源不重复入库；
- 水位线 = 已应用事件最大 `event_time` − 10 分钟，且不超过参考时刻；严格小于才记迟到，迟到**仍然应用**；
- 快照按 `event_time ≤ as_of` 物化，`as_of` 之后的事件留待定窗口，一并给出合计；
- 发布裁决：有正常数据即发布；整批全被暂缓时状态 `held_only`、空结果时 `empty_only`，
  **正式快照保持上一版**，绝不用空快照覆盖；运行失败同样是 `failed`，旧快照不动；
- 落库核对：`/audit` 逐条比对「已采信内容 vs 已落库快照」，恢复中断导致丢行时显式
  判 `E_RECOVERY_MISMATCH` 并拒绝重算，旧正式快照保留；
- 批次键 = 输入摘要 + 规则版本 + 参数 + 规则参数 + 暂缓集合签名，同一输入重跑命中批次、不新增行；
- 导出事件证据、隔离区、冲突表、快照 CSV/JSON 与运行清单（含 `run_id` 与每个输入的 sha256）。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 |
|---|---|---|---|
| M1 事件采信 | 选来源跑一次采信，看到逐文件诊断 | `POST /api/events/import`、页面 `/events` | `reader.py` `decode.py` `format.py` |
| M2 契约兼容 | 看列契约与别名，试归一一条事件 | `GET /api/schema`、`POST /api/schema/preview`、页面 `/schema` | `contract.py` `coerce.py` `alias.py` |
| M3 去重冲突 | 看重复、冲突、隔离与原因码分布 | `GET /api/conflicts`、页面 `/conflicts` | `dedup.py` `conflict.py` |
| M4 水位迟到 | 看水位时间轴与迟到明细 | `GET /api/watermark`、页面 `/` | `watermark.py` `late.py` |
| M5 快照物化 | 按水位物化快照，看待定键 | `POST /api/snapshot`、页面 `/snapshot` | `apply.py` `materialize.py` |
| M6 批次续跑 | 查历史、看清单、续跑与重跑 | `GET /api/batches`、页面 `/batches` | `state.py` `resume.py` `history.py` |
| M7 审计导出 | 下载证据行、隔离、冲突、快照与清单 | `GET /api/export/*` | `csv_out.py` `json_out.py` `manifest_out.py` |
| M8 发布核对 | 看正式快照归属、被挡下的记录、仍可发布的数据 | `GET /api/audit`、页面 `/audit` | `publish.py` `verify.py` |

模块内统一是 `handler.py`（入口）+ `service.py`（编排）+ 两个纯规则/读写文件；
定义写在 `docs/DATA-DICTIONARY.md`，坑与测试映射写在 `docs/DEPTH.md`。

## 核心规则

1. **版本序应用**：每个 `(table, pk)` 按 `version` 升序折叠，`delete` 写墓碑；
2. **冲突不猜**：同键同版本两种内容 → 整组不应用；删除后到达的低版本 upsert 不复活；
3. **水位单调**：`max(event_time) − 10min` 且不超过参考时刻，只由已应用事件推进，迟到只标记不丢弃；
4. **未来时间暂缓**：超前参考时刻超过容差的记录整条挡下，不进水位、不进快照，可复核、可放行；
5. **快照半开**：`[-, as_of]` 进快照、`(as_of, +inf)` 待定，两窗口不重叠；
6. **发布不降级**：全暂缓或失败批次不覆盖已发布的正式快照；
7. **金额 Decimal**：`0.01` 量化、`ROUND_HALF_UP`，合计可独立复算。

## 启动

```bash
docker compose up --build          # 服务在 http://127.0.0.1:18201
docker compose exec -T app python -m pytest -q
docker compose down -v             # 连数据库卷一起删
```

首次启动会用自带样例跑一次采信，页面直接有水位刻度、事件流、冲突与快照；
再点「采信」或「重跑」命中同一批次键，只复用结果、不新增行。

## 演示步骤

1. 首页看水位时间轴：竖线是水位线，黄色刻度是迟到，空心红条是暂缓（不进水）；
2. `/events` 按分类筛选，看每行的行号引用与原因码；
3. `/conflicts` 看冲突分组、隔离区与优先级表，导出隔离 CSV；
4. `/snapshot` 点「物化」，看活跃 / 墓碑 / 待定与三条对账；
5. `/batches` 看 `run_id`、输入 sha256、阶段耗时，导出运行清单；
6. `/audit` 看正式快照归属、被挡下的记录（含超前时长）与「已可放行」，以及落库核对差异。

## API 与页面

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 健康检查与水位数 |
| POST | `/api/events/import` | 采信（`source=all/main/v1_csv/late`） |
| GET | `/api/events` | 事件流（可按分类、表过滤） |
| GET | `/api/schema` | 契约、别名、版本 |
| POST | `/api/schema/preview` | 单条试归一 |
| GET | `/api/conflicts` | 冲突、隔离、重复、分布 |
| GET | `/api/watermark` | 水位、刻度、迟到表 |
| GET | `/api/snapshot` / POST `/api/snapshot` | 读快照 / 重新物化（默认正式批次；非正式批次 409） |
| GET | `/api/batches`、`/api/batches/{id}`、`/api/batches/{id}/manifest` | 批次与清单 |
| GET | `/api/audit` | 发布裁决、被挡下的记录、落库核对 |
| POST | `/api/batches/{id}/resume` | 续跑 |
| GET | `/api/export/{events,quarantine,conflicts,snapshot}.csv` | 证据导出 |
| GET | `/api/export/{snapshot,manifest}.json` | 快照与运行清单 |

页面共 7 个：`/`、`/events`、`/schema`、`/conflicts`、`/snapshot`、`/batches`、`/audit`。

## 目录

```text
app/
  main.py config.py errors.py reasons.py manifest.py store.py web.py bootstrap.py
  modules/m1_events/ … m8_audit/        # 每模块 handler + service + 规则文件
  templates/                             # base + 7 页
  static/                                # theme / layout / components + 3 个 js
docs/  DEPTH.md  DATA-DICTIONARY.md  RUNBOOK.md
sample_data/  events_main.jsonl  events_v1_gb.csv  events_late.jsonl  events_clean.jsonl  schema_contract.json
tests/  test_m1_events.py … test_m8_audit.py  test_e2e.py  test_invariants.py  fixtures/
```

## 已知不覆盖

- 只处理文件型导出，不连接 Kafka / Debezium 这类在线流；
- schema 演进只覆盖列改名与新增字段，不支持类型收窄；
- 同一批次内不做跨批次键级合并（每个批次独立折叠，快照只反映当前批次）；
- 事件级事务边界（同一事务的多个变更原子可见）未建模；
- 时钟保护只做「超前」一个方向：参考时刻取运行墙钟或显式传入的固定值，不自动探测源库时区偏移；
  被暂缓的记录必须等参考时钟追上后重新采信，不会在后台自动回填。
