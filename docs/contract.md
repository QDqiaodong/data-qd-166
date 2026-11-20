# Time-phased material planning v1

A manufacturing planner supplies as_of, 1..100 item masters, independent demands and scheduled receipts. Items have sku, calendar-day lead_days (0..365), lot_multiple, on_hand and components with positive integer quantities. Components must form a DAG; shared components are aggregated after all their parents are processed. Each item is netted chronologically using on_hand and receipts available on/before the requirement date. Net shortages round upward to lot_multiple. Only newly planned parent supply generates component demand, on its release date, including extra parent lot units. Existing finished stock and scheduled parent receipts do not re-explode.

Input dates are canonical YYYY-MM-DD; external demand/receipt dates lie in [as_of,as_of+3650 days]. Internal releases may precede as_of; past_due is explicitly reported, never clamped to today. Receipts and demands share a day bucket; receipts are usable that day. On-hand is treated as available at every planning bucket, including overdue internal requirements; this is an exception planning calculation, not historical inventory reconstruction. All amounts are integer units, with no yields, capacity, alternate materials, working calendars or order reservation lifecycle.

Every ledger row satisfies opening+scheduled_receipt+planned_receipt-gross=closing. Planned make/buy orders expose receipt/release dates. The sample requires KIT=10 on Jan10 (release Jan8); BOLT gross=30 on Jan8, with 4 on hand and 6 received Jan7, requiring 20 purchased units (release Jan7); all ending balances remain nonnegative. Malformed inputs fail before atomic report replacement.

# Customer order promise book v2

A second, separately persisted capability layers customer order lifecycle on top of the same netting engine. `material_plan.promise` exposes `init_db(db, seed)`, `apply_batch(db, request)` and `snapshot(db)`; `db` accepts `str` or `pathlib.Path`. Every call returns the snapshot dictionary below on success and raises `ValueError` on failure. Storage is one local SQLite file, single operator, standard library only; reopening the file in a new process restores every commitment without any in-process globals. The CLI is `material-promise init|apply|snapshot --db FILE [--source FILE]`: exit 0 with exactly one JSON snapshot plus newline on stdout; exit 2 with an error on stderr and empty stdout on failure. Existing `material-plan` behavior is unchanged.

## Seed and initialization

`seed` has exactly `as_of`, `items`, `receipts`. `items` (1..100) and `receipts` (at most 10000) obey the v1 netting contract and are validated by running the netting with `demands=[]`. After initialization this master data is immutable; version starts at 0 and no customer orders exist. `init` only creates a database path that did not previously exist — an existing path, including an empty file or symlink, is rejected; failed initialization removes the partial file and never leaves a usable half-initialized database. `apply` and `snapshot` only open an initialized database and never create one implicitly.

## Requests, reservations and cancellations

`request` has exactly `request_id`, `expected_version`, `operations`. Identifiers follow the v1 text rules (nonempty, stripped, at most 200 chars, no control characters); `expected_version` is a non-boolean nonnegative integer; `operations` has 1..100 entries executed strictly in array order, which is semantically significant and is never sorted or merged.

- `reserve` has exactly `op="reserve"`, `order_id`, `sku`, `date`, `quantity`. The sku must exist; the date follows the v1 canonical `YYYY-MM-DD` rule and lies in `[as_of, as_of+3650 days]`; quantity is a positive non-boolean integer.
- `cancel` has exactly `op="cancel"`, `order_id` and only succeeds against a currently active order.

Order identities move from never-seen to `active` to `cancelled`; an `order_id` is never reusable in either direction — a cancelled order cannot be reserved again, and an existing identity (any status) cannot be reserved again. A batch that reserves and then cancels a new order still leaves that identity occupying its terminal cancelled state. At most 10000 order identities (including cancelled ones) may ever exist.

After **each** operation, all active orders are fed together as the `demands` of one netting run (each active `order_id` maps to a demand `id`) using the fixed seed's `as_of`, `items` and `receipts`. If any newly planned order has `release_date` earlier than `as_of`, the whole batch is rejected — feasibility is evaluated per intermediate state, never per customer order in isolation and never only on the final batch state. A release date equal to `as_of` is acceptable. Each run reads only the real on-hand stock and scheduled receipts from the seed; planned make/buy orders and ending inventories from previous reports are advice and are never fed back as real supply. Same-day receipts remain usable, parent lot slack still covers later requirements, shared components aggregate before lot rounding, and existing stock/receipts never re-explode. With no active orders the plan still reports the full netting result including scheduled receipts.

## Commit and replay

A new `request_id` requires `expected_version` to equal the current version, otherwise the batch fails as a whole. One successful batch adds exactly one version even when it leaves zero active orders. Order terminal identities, effective demands, the version and that batch's original successful response persist together in one SQLite transaction; any operation failure leaves no identity, cancellation, demand, request record or version change from the batch.

Replaying a successfully committed `request_id` returns the original historical snapshot without recomputing against current data and without incrementing the version. Equality of "the same complete request" covers `request_id`, `expected_version` and every field of every operation; JSON object key order is irrelevant, array order matters. A committed id with different content fails, and successful replay is recognized before the `expected_version` check — so a replayed request whose embedded `expected_version` is now stale still succeeds, while changing that field counts as a conflicting payload. Failed batches do not consume their `request_id`; the same id may be retried with corrected content, and a replay must itself remain a structurally valid request. Replay is the recovery mechanism for a committed response whose stdout delivery was interrupted; cross-medium atomicity between stdout and SQLite is not guaranteed.

## Snapshot and boundaries

All three entry points return a snapshot with exactly `version`, `orders`, `plan`. Top-level `orders` lists every customer order ever successfully created (active and cancelled), sorted by `order_id`, each with exactly `order_id`, `sku`, `date`, `quantity`, `status`; `plan` is the v1 netting result for the currently active demands and retains the v1 `orders` (replenishment advice — do not confuse with customer orders), `ledger` and `ending_inventory` fields and ordering. A successful apply returns the snapshot as committed; replay returns the historical snapshot; `snapshot` always returns the current one. The same committed state always serializes to identical JSON — no timestamps or random ids.

Inputs retain the 8MB limit, duplicate-JSON-key rejection and non-finite-number rejection; unknown or missing fields are rejected at every level, as are boolean quantities and boolean `expected_version`; Python callers' objects are never mutated. Infeasible-but-legal requests, version errors, lifecycle errors and malformed requests all fail without business state changes. Concurrency, services and external dependencies are out of scope.

## Reproducible acceptance scenario

With `as_of=2026-01-08`, KIT lead 0 / lot 1 / on hand 0 consuming 3 BOLT each, BOLT lead 1 / lot 1 / on hand 3 and no receipts: reserving one KIT for Jan 8 succeeds; a second independent KIT reservation fails with the version unchanged; cancelling the first and reserving the second within one batch succeeds with only the second active. A batch that cancels A, reserves B and then cancels unknown Z fails entirely — A stays active and both B's order id and the batch request id remain reusable in a corrected request; reserving B before cancelling A also fails even though the final combination would be feasible. After cancelling A, replaying the original reservation request still returns the reservation-era version and plan while the current snapshot shows A cancelled, and A can never be reserved again. Synthetic fixtures and integration tests additionally cover shared-component aggregation, parent lot slack, same-day receipts, past releases, batch rollback, identity conflicts, replay, stale versions and cross-process recovery.

# Finite-capacity manufacturing schedule v3

The v1 netting engine is unchanged in what it decides — *how much* to replenish per sku and requirement bucket, rounded up to `lot_multiple` — but a manufactured item may now declare a workcenter, and the release of its newly planned quantity is scheduled into a shared daily capacity calendar instead of always landing on the nominal `requirement_date - lead_days`. Every finite-capacity report must simultaneously satisfy netting conservation, BOM dependency and the per-workcenter daily ceiling.

## New optional fields

- Item row: `workcenter` and `capacity_per_unit`. They must appear together or both be absent. `workcenter` follows the v1 non-empty text identifier rule. `capacity_per_unit` is a positive, non-boolean integer: each manufactured unit occupies that many capacity units on its release day. These fields take effect only for items with a non-empty `components` list (manufactured items); for purchase items and for manufactured items that omit them, the historical unlimited-capacity behavior is retained.
- Top level: `capacities`, an array (bounded by the existing structured-input limit) whose rows have exactly `workcenter`, `date`, `capacity`. `date` follows the canonical `YYYY-MM-DD` rule and lies in `[as_of, as_of+3650 days]`; `capacity` is a nonnegative, non-boolean integer; at most one row per workcenter/day. An unlisted workcenter/day has zero capacity.
- All original top-level fields, item fields and capacity-row fields reject unknown or missing fields. An input that omits every new field is byte-for-byte compatible with v1 for both `plan(data)` and the `material-plan` CLI.

`capacity_per_unit` is ignored for purchase items (they stay unconstrained), but its presence is still validated.

## Scheduling rules

For each capacity-constrained manufacturing shortage, the already-rounded planned quantity may be split into one or more batches. Each batch quantity is a positive multiple of `lot_multiple`; the batch quantities sum **exactly** to the planned quantity. A batch's `release_date` lies in the closed window `[as_of, requirement_date - lead_days]` (for shortages whose nominal release is not earlier than `as_of`); its `receipt_date` is `release_date + lead_days` and is therefore never later than the requirement date. On `release_date` the batch consumes `quantity * capacity_per_unit` capacity units of its workcenter; all batches in the report that share a workcenter and day sum to at most that day's capacity. Purchase advice occupies no manufacturing capacity.

Only a newly planned manufacturing batch explodes to components, on its **actual** `release_date`. Same component, same date demand is merged before the component's own `lot_multiple` rounding. Netting then recurses level by level down the BOM, so a constrained child is scheduled against the residual shared calendar on the dates its parent actually releases. Existing on-hand and scheduled receipts occupy no manufacturing capacity and never re-explode. Parent lot slack, same-day receipts, and shared-component aggregation keep their v1 semantics.

All planning is still parent-first; independent skus at one BOM level share the calendar without double-booking. The report's plan-order rows keep the v1 fields (`sku`, `receipt_date`, `release_date`, `quantity`, `past_due`, `kind`), so a split item simply appears as several make rows. Ordering is the v1 `(release_date, sku)` plus deterministic tie-breaks on `receipt_date` and `quantity`; multiple batches of one sku are ordered by date and never depend on input-row order. Any feasible legal allocation is acceptable; the algorithm need not find a unique optimum, but it must never double-book a shared workcenter.

A shortage whose nominal release is **earlier than `as_of`** keeps the v1 `past_due` semantics: it is reported on that past date, is never clamped to `as_of`, and does not consume the future capacity calendar. If a non-overdue constrained shortage cannot be fully distributed within its release window given the shared calendar, `plan(data)` raises `ValueError` and no report is produced; the CLI exits 2 with an error on stderr, empty stdout, and leaves any existing output file untouched. A report is never returned with only some materials capacity-feasible. Previously reported `planned_receipt`/`ending_inventory` are still never fed back as real supply.

## Reproducible acceptance scenario

Synthetic fixture `fixtures/capacity/acceptance.json`: `as_of=2026-01-06`, KIT demand 4 on 2026-01-10, `lead_days=2`, `lot_multiple=1`, `on_hand=0`, each KIT consumes 2 BOLT, KIT on `line-a` with `capacity_per_unit=1`; `line-a` capacity is 2 on Jan 6, 2 on Jan 7 and 0 on Jan 8. BOLT is purchased, `on_hand=8`, no receipts. A feasible report releases 2 KIT on Jan 6 (receipt Jan 8) and 2 KIT on Jan 7 (receipt Jan 9); each batch explodes 4 BOLT on its own release day, so all 8 BOLT demands are covered by real stock and no BOLT demand falls on Jan 8. Setting all three `line-a` days to 0 makes the plan infeasible: `plan` raises and the CLI exits 2 without overwriting an existing report. Additional fixtures cover a shared workcenter across two skus, shared-component merging on the actual release date, same-day receipts, exact-vs-over capacity, cross-date lot splitting, past-due releases, multi-level constrained children, and legacy inputs.



# 批次可用窗口净算 v4

v1–v3 的净算口径不变；当物料明确声明批次身份与可用窗口时，实体库存按批次净算，使已收货但未放行、或在需求日前已失效的数量不再掩盖缺料。声明新字段的输入进入“批次模式”，没有批次窗口字段的既有输入在 `plan(data)`、`material-plan` 命令与输出上逐字保持原行为。

## 新增可选字段

- 物料行可选 `inventory_lots`：声明该 sku 的在手实物库存批次。若出现，其为数组（沿用 10000 条上限），每个批次行字段**恰为** `lot_id`、`quantity`、`available_from`、`expires_on`。`quantity` 为正整数；日期为规范 `YYYY-MM-DD`；`expires_on` 不得早于 `available_from`。声明时各批次 `quantity` 之和必须**恰等于**该物料 `on_hand`。`inventory_lots: []` 仅在 `on_hand=0` 时合法，并仍将该 sku 标记为批次模式。
- 到货行（顶层 `receipts`）可选地**同时**声明 `lot_id`、`available_from`、`expires_on`；三者要么全部出现，要么全部不出现。声明时该到货是一个**完整批次**，不可拆分；`available_from` 不得早于该到货的 `date`，`expires_on` 不得早于 `available_from`。
- `lot_id` 沿用在手标识文本规则（非空、去空白、≤200 字符、无控制字符），并在**整份输入内唯一**——库存批次与到货批次共用同一命名空间，重复即拒绝。
- 未知或缺失字段在任何层级都拒绝。所有旧字段与容量字段的校验保持不变。

## 批次模式的判定与净算

某 sku 只要声明了 `inventory_lots`（即使为空数组）**或**存在带窗口的到货，就进入批次模式；否则沿用 v1 的无限期可用净算，账本字段与排序完全不变。批次模式下：

- 只有 `available_from` **不晚于** D 且 `expires_on` **不早于** D 的批次数量，在日期 D 的需求净算中可用。放行当天可消费，失效当天（`expires_on`）当天可消费，次日不可消费。
- 批次数量在 `available_from` 记入可用库存（在账本上体现为 `scheduled_receipt` 放行事件）；在首个计划日或之前已放行的在手批次直接计入 opening。未放行数量不得覆盖更早日期的需求。
- 仍可用的多批次按 `expires_on` 最早优先、再按 `lot_id` 升序消耗（FEFO）。同一批次跨多个需求日只消耗其剩余量，不重复消耗。
- 过期后剩余数量作废，不结转到以后日期；作废记在 `expires_on` 的次日（若早于首个计划日，则记在首个计划日）。
- 未声明批次窗口的旧库存和到货（以及本 sku 自身的计划补单）保持无限期可用语义，在 FEFO 批次之后覆盖需求。

净算金额、`lot_multiple` 上取整、BOM 父子释放日与容量排程口径均沿用既有计划口径。计划生成的 make/buy 建议仍是建议，不回灌为下一次输入的实物库存。

## 批次模式账本

批次模式下每行账本除 v1 字段外新增：

- `expired`：当日按批次作废的整数数量；
- `lot_consumption`：当日按批次消耗的数组，每项**恰为** `lot_id` 与 `quantity`（该批次当日被消耗的数量）。

并满足守恒：

```
opening + scheduled_receipt + planned_receipt - gross - expired = closing
```

为展示仅发生放行或过期事件的日期（当日无毛需求、无补单），也须为该 sku 输出相应账本行。账本排序仍为 `(sku, date)`。没有进入批次模式的 sku，其账本行不含任何新字段。

## 验收与边界

合成样例 `fixtures/lots/acceptance.json`：`as_of=2026-01-06`，KIT 于 2026-01-10 需求 1 件、`lead_days=2`、每件耗 3 个 BOLT；BOLT 无库存，另有 3 个 BOLT 于 2026-01-06 到货但 `expires_on=2026-01-07`。KIT 于 2026-01-08 投产，故这批 BOLT 在实际子件需求日已过期；因此计划在 1 月 8 日为 BOLT 补 3 个，并把这批旧到货的 3 个在 1 月 8 日记为过期作废。若在同一输入另加 1 月 7 日独立 BOLT 需求 2 个（`fixtures/lots/acceptance_partial_use.json`），该批次先覆盖 2 个，剩余 1 个在 1 月 8 日记为过期作废。

`fixtures/lots/window_boundaries.json` 覆盖未放行不可消费、放行当天可消费、失效当天可消费而次日不可消费；`fixtures/lots/fefo_cross_date.json` 覆盖 FEFO 与跨日期余额；`fixtures/lots/shared_component.json` 覆盖多父项同日子件需求先合并。集成与单元测试见 `tests/test_lots.py`。

# 实物批次谱系与召回出库闸 v5（material-trace）

净算建议与客户承诺都不是实物。本版新增独立的离线实物账：真实收货、实际完工与发货成为可追溯批次，原料召回沿**实际用料批次**传播，既阻断受影响成品继续出库，也能定位此前已发给哪些客户。原有 `material-plan`、`material-promise` 与 `plan(data)` 的输入、输出和数据库行为完全不变；实物账使用另一份本地 SQLite 文件，与既有计划器的 `inventory_lots` 及承诺簿互不混淆，也不从净算建议自动生成任何实物。

## Python 接口与 CLI

`material_plan.traceability` 提供 `init_db(db, master)`、`apply_batch(db, request)`、`snapshot(db)`；`db` 接受 `str` 或 `pathlib.Path`。每次调用成功返回下述快照字典，失败抛 `ValueError`。CLI 为 `material-trace init|apply|snapshot --db FILE [--source FILE]`：成功退出 0 且 stdout 只输出一份 JSON 快照加一个换行；失败退出 2、stdout 为空、stderr 输出错误。存储为单个本地 SQLite 文件，单操作者、仅标准库，无服务、无网络；跨进程重开恢复全部事实。

## 主数据与批次

`master` 字段**恰为** `items`，1..100 行，每行**恰为** `sku`、`components`；`components` 每行**恰为** `sku`、`quantity`。标识符沿用 v1 文本规则，数量为正整数，BOM 必须无环并允许共享子件。主数据只提供实际完工时的单位用料配方，初始化后不可修改。

初始化版本为 0，无批次、发货或召回；只创建此前不存在的路径——已有路径（包括空文件或符号链接）一律拒绝；初始化失败不留下可用的半成品。`apply`/`snapshot` 只打开已初始化的数据库，绝不隐式建库。

每个实物批次有终身唯一的 `lot_id`、`sku`、`produced_quantity` 与 `remaining_quantity`；清零后身份与谱系仍保留。批次可按数量拆分领用，拆分不创建新身份。本模块不引入批次日期、效期或质检规则。

## 业务请求

`request` 字段**恰为** `request_id`、`expected_version`、`operations`；`expected_version` 为非布尔非负整数，`operations` 为 1..100 条，严格依数组顺序逐条判定（不是只看批末）。未知或缺失字段在每一层都拒绝。

- `receive`：字段 `op`、`lot_id`、`sku`、`quantity`，`op="receive"`。为任意已知 SKU 记录外部实际收货（制造 SKU 也可外购），创建新批次；不得从配方推断其上游来源。
- `make`：字段 `op`、`lot_id`、`sku`、`quantity`、`inputs`，`op="make"`。只允许有 `components` 的 SKU；`quantity` 为实际完工数量；`inputs` 非空，每行恰为 `lot_id`、`quantity`，单操作内输入身份不可重复。所有输入必须已存在、当前可领用且余额充足；按输入批次的 SKU 汇总后必须与 `quantity` 乘主数据配方**完全相等**——不能缺料、多投、夹入非配方物料，也不能用计划采购量补齐。成功时同时扣减全部输入余额、创建完工批次，并永久记录各实际输入批次及数量。完工批量不沿用计划建议的 `lot_multiple`，原料与成品单位不同也不要求跨 SKU 数量相等，但同一 SKU 内不得凭空增减。新 `lot_id` 不能引用自身或任何将来才创建的批次。任一输入不可用则整次操作失败。
- `ship`：字段 `op`、`shipment_id`、`customer_id`、`sku`、`quantity`、`allocations`，`op="ship"`。`allocations` 非空，每行恰为 `lot_id`、`quantity`，身份不可重复；只能使用该 SKU 已有、当前可领用且余额充足的批次，各分配之和**恰等于**本次发货 `quantity`。成功扣减实物余额并永久保存整笔发货及原始批次分配；`shipment_id` 终身不可复用。不会自动改用同 SKU 的其他干净批次——调用方必须明确给出分配。
- `recall`：字段 `op`、`recall_id`、`lot_id`，`op="recall"`。针对一个已存在批次开启召回，`recall_id` 终身唯一；即使根批次余额已为 0 也允许。多个不同 `recall_id` 可同时指向同一根批次。
- `close_recall`：字段 `op`、`recall_id`，`op="close_recall"`。只允许关闭当前 `active` 的召回；关闭后身份保留且不可重开；不存在或已关闭均失败。

## 召回传播与出库闸

一个 active 召回影响其根批次以及沿**实际 make 输入关系**能到达的所有后代批次。只要某原料批次被使用过，对应完工批次整体受影响，不按投入比例推算所谓干净部分；已发货、余额为 0 或中间层余额为 0 都不得截断传播。一次 make 混用多个来源时保留全部来源；同一祖先经两条路径到达同一批次只计一个 `recall_id`。

当前受一个或多个 active 召回影响的批次不得作为 make 输入，也不得被 ship 领用。召回不删除、不扣减实物，不回滚此前合法完工或发货。关闭一个召回只移除它自己的影响；被其他 active 召回覆盖的批次仍被阻断。一次 make 严格按**该操作当时**的召回状态判断——不能因批末关闭召回而放行前面的非法领用；反过来先合法发货、后开启召回可以成功，并把该历史发货列入当前暴露清单。批次尚未出生时不能被直接召回；外部 `receive` 与其他旧批次即使 SKU 相同也不自动受影响。

## 快照

顶层字段**恰为** `version`、`lots`、`shipments`、`recalls`、`exposures`。

- `lots` 按 `lot_id` 升序，每行**恰为** `lot_id`、`sku`、`produced_quantity`、`remaining_quantity`、`inputs`、`blocked_by`。`receive` 的 `inputs=[]`；`make` 的 `inputs` 保存原始投入行并按 `lot_id` 排序。`blocked_by` 为当前覆盖该批次的 active `recall_id` 去重升序数组。
- `shipments` 按 `shipment_id` 升序，每行**恰为** `shipment_id`、`customer_id`、`sku`、`quantity`、`allocations`；`allocations` 按 `lot_id` 排序，是不可变的已发事实。没有 `date` 字段。
- `recalls` 按 `recall_id` 升序，每行**恰为** `recall_id`、`lot_id`、`status`，`status` 为 `active` 或 `closed`。
- `exposures` 按 `(recall_id, shipment_id, lot_id)` 排序，每行**恰为** `recall_id`、`shipment_id`、`customer_id`、`sku`、`lot_id`、`quantity`。每个 active 召回针对每条受影响的原发货批次分配输出一行，`quantity` 为该分配已发量；同根多路径不重复计数。多项 active 召回可各自列出同一笔已发量，不合并为总召回量。只关闭一项时只撤去该 `recall_id` 的暴露行；原 `shipments` 永远保留。尚未发货的余额不进入 `exposures`。

## 提交、一致性与失败

一次成功的新请求把版本加 1，批内全部扣增、谱系、发货、召回与成功响应一起持久生效；后续任一操作失败则整批回滚，不留前缀消耗、身份或版本。新请求须匹配当前 `expected_version`。成功 `request_id` 以完整请求内容重放：对象键序无关，`operations` 与 `inputs`/`allocations` 数组顺序有意义；相同请求返回首次提交时的原始快照，不重算当前召回、不重复发货、不改变版本。合法结构的成功重放优先于版本检查；同 ID 异内容失败；失败请求不占 `request_id`。`snapshot` 永远反映当前召回与余额，不会误返回历史重放快照；成功响应输出中断后仍可原样重发取回。

所有业务字段逐层拒绝未知/缺失项；标识符沿用 v1 text 规则，数量为 v1 范围的正非布尔整数。`items`/`components` 规模沿用上限，单次 `inputs`/`allocations` 最多 100 条，存量批次/发货/召回身份各最多 10000（含清零与关闭身份）。API 不修改调用方对象；CLI 沿用 8MB、重复 JSON 键及非有限数拒绝规则。输入格式与业务失败都不改变任何已提交事实。并发、跨介质 stdout 原子提交与真实 ERP 接入不在范围内。

## 可复现验收场景

主数据：`RAW` 无子件、`SUB` 每件耗 2 `RAW`、`KIT` 每件耗 1 `SUB`（`fixtures/trace/master.json`）。在一个成功请求（`request_physical_build.json`）中：receive 原料 `R` 数量 6；make `S1`（`SUB` 2，耗 `R` 4）；make `S2`（`SUB` 1，耗 `R` 2）；make `K`（`KIT` 1，耗 `S1` 1）；将 `K` 的 1 件发给客户 `C1`，发货号 `OUT1`。此时 `R` 余额 0、`S1` 余额 1、`S2` 余额 1、`K` 余额 0。

下一请求 `recall X` 指向 `R` 必须成功，`blocked_by` 覆盖 `R`、`S1`、`S2`、`K`，`exposures` 有 `X/OUT1/K` 数量 1——不能因根或末端余额为 0 而漏掉客户。再收到独立 `RAW` 批次 `CLEAN`（`request_receive_clean.json`），不受 `X` 阻断。

在 `X` 有效时先尝试用 `S2` 完工新 `KIT`、再 `close_recall X`（`request_blocked_make_then_close.json`），整批必须失败，版本、余额及新身份均不变化。另分支（`request_recall_y.json` → `request_close_x.json`）：先开启 `Y` 指向 `S1`，再关闭 `X`，此时 `S1`、`K` 仍受 `Y` 影响，`S2` 恢复可领用；`X` 的暴露行消失，`Y` 对 `OUT1` 的暴露行仍在。用恢复的 `S2` 合法完工并发货（`request_resume_shipping.json`）后，重放最初包含 `OUT1` 的成功请求只返回原版本/原快照；当前快照保留已关闭 `X` 和 active `Y`，不重新扣料或抹去召回。

测试 `tests/test_traceability.py` 另覆盖：一个成品混用同 SKU 的多个来源、同一原料经两条中间路径汇入同一成品时传播与暴露去重、同 SKU 干净批次可出库而受召回批次不可、某分配不可用导致整批已有合法前缀也回滚、配方计量不等、重复批次分配、未知/终态身份、版本冲突、身份上限、跨进程恢复与 CLI 错误输出。

# 离线材料成本月末关账 v6（material-cost）

工厂已用 material-trace 记录实际收货、完工领料与发货；本版新增独立的离线成本账簿：财务月末把采购成本沿**实际领料**分配到成品与已发货明细，产出可对账的期末库存估值与销售成本。发票晚到或更正时旧账单保持原样：已发走部分只在当前月入账差额，仍在库部分按最新成本更新期末价值。只核算材料成本，不另外计算工费。既有 `material-plan`、`material-promise`、`material-trace` 的输入、输出与数据库行为完全不变；成本账簿是另一份本地 SQLite 文件，不从净算建议或承诺簿构造任何实物。

## Python 接口与 CLI

`material_plan.costing` 提供 `init_db(db)`、`close_period(db, close)`、`show_period(db, period)`；`db` 接受 `str` 或 `pathlib.Path`。成功时 `init_db` 返回 `{'closed_periods': []}`，`close_period`/`show_period` 返回下述账单字典；失败一律抛 `ValueError` 且不修改调用方传入对象。CLI 为 `material-cost init --db FILE`、`material-cost close --db FILE --source FILE`、`material-cost show --db FILE --period YYYY-MM`：成功退出 0 且 stdout 只输出一份 JSON 加一个换行；失败退出 2、stdout 为空、stderr 输出错误。存储为单个本地 SQLite 文件，单操作者、仅标准库，无服务、无网络；跨进程重开恢复全部已关账事实。

`init` 只创建此前不存在的路径——已有路径（包括空文件或符号链接）一律拒绝；初始化失败不留下可用的半成品。`close`/`show` 只打开已初始化的成本账簿，绝不隐式建库，也不接受其他模块的数据库文件。

## 关账输入

`close` 输入字段**恰为** `period`、`physical`、`cost_updates`。

- `period`：规范 `YYYY-MM` 字符串（年 0001–9999，月 01–12）。首期可选择任意有效月份；以后每期必须恰为上一已冻结期的下一月（含跨年）。重复关账、跳月、倒退一律拒绝。
- `physical`：`material-trace snapshot` 输出的完整当期累计快照，字段**恰为** `version`、`lots`、`shipments`、`recalls`、`exposures`，各明细行字段与 v5 快照完全一致。由财务操作者保证其来自同一实物账簿。质量状态（`recalls`、`exposures`、各批次 `blocked_by`）只做结构校验，**不参与估值，也不参与实物历史比较**。
- `cost_updates`：数组（至多 10000 条），每行字段**恰为** `lot_id`、`total_cents`、`source_ref`。`lot_id` 只允许指向快照中 `inputs` 为空的收货批次；同次关账每批最多一条。`total_cents` 为 0 至 10^12 的非布尔整数分，代表该批整笔采购成本的**新总额**（不是变动额）。`source_ref` 为发票或暂估凭证标识（沿用标识符文本规则）。所有**首次出现**的收货批次在当期必须有成本（允许明确为 0）；旧批次未更新就沿用前次总额；已用尽的收货批次仍可改价。金额变动与对应的 `physical` 在同一次关账提交。

未知或缺失字段在每一层都拒绝；标识符沿用 v1 文本规则，数量为正非布尔整数（余额允许 0），`version` 为非负非布尔整数；CLI 沿用 8MB、重复 JSON 键及非有限数拒绝规则。

## 实物快照核对

整份快照核对通过后才能参与关账：

- 身份与引用唯一有效：`lot_id`、`shipment_id` 各自唯一；领料来源与发货分配必须指向快照中已存在的批次；同一批次内领料/分配身份不重复。
- 领料谱系无环（含自环）。
- 发货分配与发货一致：分配批次的 SKU 必须等于发货 SKU，各分配之和恰等于发货数量。
- 每批数量守恒：`produced_quantity` 等于 `remaining_quantity` 加上该批全部被领料量与全部发货分配量之和。
- 各明细数组按身份比较，数组排列不改变事实。

与账簿基线比较：`version` 不能倒退；版本相同只允许实物事实完全相同（此时可单独更正成本）；版本前进时旧批次、旧发货都不得消失或改写——旧批次唯一可变化的字段是可由新增消耗解释的余额（质量字段除外），该约束由整份快照守恒与旧事实保持共同强制。第一份快照已有的全部事实都记作首期出现；以后以事实首次进入关账快照的月份为其归属，不推测实际业务日期；新增物理记录只能通过后续快照进入。

## 计价与分摊

一律以整数分计价。一批收货的价值为其最新采购成本总额；一批完工的价值为各投入批次转入金额之和。对任一批次，其消耗事实先按**首次入账月份**排序；同月先列领料（按领入批次 `lot_id` 升序），再列发货（按 `shipment_id` 升序）。这一顺序固定，后加入的记录不能挤到旧记录前面。

设整批价值为 V、产出数量为 Q，某条消耗使累计消耗由 a 增到 b，该条取 `floor(V*b/Q) - floor(V*a/Q)` 分；余款为在库价值。分摊舍入逐批发生：混用多个来源时，先在各来源批次上分摊得到转入金额，汇合后的完工批次再按自身产量分摊。数量在上限内可任意大，按区间取整公式计算，不按单件展开。

## 账单

账单字段**恰为** `period`、`physical_version`、`cost_updates`、`inventory`、`cogs`、`totals`：

- `cost_updates`：当期成本更新凭证，按 `lot_id` 升序，每行恰为 `lot_id`、`total_cents`、`source_ref`。
- `inventory`：期末在库明细，按 `lot_id` 升序，仅列剩余量大于 0 的批次，每行恰为 `lot_id`、`sku`、`quantity`、`amount_cents`（按当前成本重估的在库价值）。
- `cogs`：本期销售成本明细，按 `(shipment_id, lot_id)` 升序。首次入账的发货分配列 `category="sale"`，金额为该分配当前估值，即便为零也列；往期发货分配只在累计应确认金额变化时列 `category="adjustment"`，金额为累计应确认减去此前所有时期已确认之和，可为负。每行恰为 `category`、`shipment_id`、`lot_id`、`amount_cents`。
- `totals`：恰为 `opening_cents`（期初库存价值，首期为零，以后为上一期冻结的期末）、`purchases_cents`（采购成本净增额：新收货全额与旧收货改价差额之和，可为负）、`cogs_cents`（本期销售成本净额）、`ending_cents`（期末库存价值）。每期均满足 `opening + purchases - cogs = ending`。

同一月的 `show` 返回该月**首次关账时**冻结的账单，不按最新发票重新出账；已成功关账但 stdout 输出丢失时可用 `show` 取回。关闭的月份不可改写。

## 提交与失败

一次成功关账把期间、成本更新、实物基线、事实归属、已确认金额与冻结账单在同一个 SQLite 事务内持久化；任一校验或业务失败（坏批次引用、非法成本更新、实物历史被改写、期间不连续等）都整期回滚，不留任何部分变化，修正后可重新提交该月。账单只由上述已提交事实决定，无时间戳或随机量，同一冻结账单永远序列化为相同 JSON。

## 可复现验收场景

`fixtures/cost/` 下三个月关账输入（实物来自同一 material-trace 账簿：RAW 无子件，SUB 每件耗 1 RAW）：

- 2026-01（`close_2026_01.json`）：收货 R 数量 3、成本 100 分；领 2 件完工 S 数量 2；从 S 向 O1 发 1 件。期末 R/S 各剩 1 件，价值 34/33；O1 销售成本 33；期初 0、采购净增 100、销售成本净额 33、期末 67。
- 2026-02（`close_2026_02.json`）：实物不变（同版本同事实），R 更正为 104 分。期初 67、采购净增 4、O1 调整 +1、期末 70（R/S 各 35）。
- 2026-03（`close_2026_03.json`）：R 更正为 97 分，同时 R 余料发至 A、S 余料发至 O2。当期 A/O2 销售成本 33/32，O1 只补 -2，销售成本净额 63，期末 0；首月与次月账单保持原值。

集成测试 `tests/test_costing.py` 另覆盖：同一批次先发 Z、隔月再发字典序更早的 A 验证历史分摊顺序（A 不得挤到 Z 前面）；同月先领料后发货、同月领料按领入批次升序；多来源汇合后按完工批自身产量分摊；多层中间耗尽批次的价值传导与已耗尽收货批次改价；零金额分配仍列 `sale`；旧物理记录被改写/消失、版本倒退、同版本事实不同、坏批次引用或非法成本更新导致的整期回滚；期间连续性；跨进程恢复与 CLI 行为。
