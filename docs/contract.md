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
