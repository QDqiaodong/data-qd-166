# data-qd-166 · 多层物料需求净算

已实现可离线运行的业务闭环，字段、算法约定和未支持边界详见 [数据契约](docs/contract.md)。样例均为自制合成数据，来源及许可见 fixtures/SOURCES.md。

## 独立安装

Python >=3.11（本批实测3.12.14），标准库运行与unittest测试。使用已有uv；setuptools/wheel精确锁定。每个仓库建立独立环境，不共享site-packages。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -m unittest discover -s tests -v
```

已有公共缓存时可为两条uv pip install添加 --offline；缓存缺失时需先准备构建工具，不伪称离线成功。修改源码后重新安装。

## 端到端演示

在仓库根目录执行。run/为忽略的运行输出，演示不访问网络、不启动服务或容器。

```sh
.venv/bin/material-plan --source fixtures/input.json --output run/report.json
```

预期：KIT 制造10件，BOLT采购20件；KIT期末4、BOLT期末0。

输入JSON最多8MB，拒绝重复键和非有限数；完整验证成功后原子替换JSON报告。输入输出不得相同，拒绝符号链接输出；失败退出2且保留旧报告。同输入重跑结果一致。

单操作者本地批处理；不承诺并发发布或断电恢复。代码/样例采用MIT许可。

## 有限产能制造排程

在净算之上增加可选的每日产线容量：制造物料可声明 `workcenter` 与 `capacity_per_unit`，顶层 `capacities` 给出各产线每日可用产能。净算仍先决定每个需求桶的短缺量并按 `lot_multiple` 取整；排程随后把该制造量拆成一个或多个批次，在 `[as_of, 需求日-lead_days]` 窗口内占用共享产线产能，每批完工日不晚于需求日，且每批数量仍是 `lot_multiple` 的正整数倍、各批之和恰好等于计划量。只有新制造批次按其**实际投产日**产生子件需求，因此子件净算会随批次日期自动前移；库存与既有到货既不占产能也不爆子件，采购建议不占制造产能。

没有新字段的旧输入行为完全不变。产能不足时整份计划拒绝：`plan` 抛 `ValueError`，CLI 退出 2、stderr 报错、stdout 为空并保留旧报告文件。字段、计算口径与边界详见 [数据契约](docs/contract.md) 的 v3 章节。

可复现验收样例 `fixtures/capacity/acceptance.json`：KIT 于 2026-01-10 交 4 件、lead 2、每件占 1 单位 `line-a` 产能并耗 2 个 BOLT；`line-a` 在 1/6、1/7 各 2 单位、1/8 为 0，BOLT 现货 8。结果拆为 1/6 投产 2 件、1/7 投产 2 件，BOLT 需求随投产日落在 1/6 与 1/7，全部由现货覆盖。

```sh
.venv/bin/material-plan --source fixtures/capacity/acceptance.json --output run/capacity.json
# 产线全为 0 时：拒绝且不覆盖 run/capacity.json
.venv/bin/material-plan --source fixtures/capacity/acceptance_infeasible.json --output run/capacity.json
```

用同一渲染器可核对产能证据：把所有 `kind="make"` 批次的 `quantity * capacity_per_unit` 按 `(workcenter, release_date)` 汇总，须不超过对应 `capacities` 行（未列出即 0）。集成测试 `tests/test_capacity.py` 覆盖共享产线、共享子件合并、同日到货、用满/超一单位、跨日期拆批、多层受限子件、过去投产以及旧输入与 material-promise 兼容。

## 批次可用窗口净算

在净算之上增加可选的在手库存批次窗口：物料行可声明 `inventory_lots`（每批 `lot_id`、`quantity`、`available_from`、`expires_on`），到货行也可声明同样三个窗口字段而成为一整个批次。声明后该物料按批次净算：只有放行不晚于需求日、且需求日不晚于失效日的批次可用；同日放行当天可用、失效当天可用而次日不可用；过期余量当日作废、不结转；多批次按最早失效优先、`lot_id` 升序消耗（FEFO）。因此已收货但未放行，或在需求日前已失效的数量不会再掩盖缺料。

批次模式下账本每行新增 `expired`（当日作废量）与 `lot_consumption`（当日按批次消耗的 `lot_id`/`quantity` 数组），并满足 `opening + scheduled_receipt + planned_receipt - gross - expired = closing`；仅发生放行或过期的日期也会输出账本行。没有批次窗口字段的旧输入行为完全不变，账本不含新字段。字段、口径与边界详见 [数据契约](docs/contract.md) 的 v4 章节。

可复现验收样例 `fixtures/lots/acceptance.json`：`as_of=2026-01-06`，KIT 于 1/10 交 1 件、lead 2、耗 3 个 BOLT；BOLT 无库存，3 个于 1/6 到货但 1/7 失效。KIT 在 1/8 投产，此时该批 BOLT 已过期，故计划于 1/8 补 3 个 BOLT，并把旧到货的 3 个记为作废。

```sh
.venv/bin/material-plan --source fixtures/lots/acceptance.json --output run/lots.json
# 另加 1/7 独立 BOLT 需求 2 个：该批次先覆盖 2 个，余 1 个在 1/8 作废
.venv/bin/material-plan --source fixtures/lots/acceptance_partial_use.json --output run/lots_partial.json
```

在 `run/lots.json` 中可见 BOLT 在 1/8 的 `expired=3`、`planned_receipt=3`；`run/lots_partial.json` 中 1/7 的 `lot_consumption` 为 `L-OLD×2`，1/8 的 `expired=1`。`fixtures/lots/window_boundaries.json` 覆盖放行/失效边界，`fixtures/lots/fefo_cross_date.json` 覆盖 FEFO 与跨日期余额，`fixtures/lots/shared_component.json` 覆盖共享子件合并。集成测试 `tests/test_lots.py` 覆盖既有净算兼容、放行/失效边界、FEFO 消耗、跨日期余额、组件释放日与失败输入。

## 客户订单承诺簿（material-promise）

在同一净算引擎上新增可跨进程恢复的客户订单承诺簿：预约/撤销以批次为单位提交，每个操作后的全部 active 订单都作为同一次净算的 demands；任一补单释放日早于 as_of 即整批拒绝。数据只存本地 SQLite（标准库 `sqlite3`），无服务、无外部依赖；业务口径与字段详见 [数据契约](docs/contract.md) 的 v2 章节。

干净安装后（沿用上面的 venv 步骤，安装后可直接用 `.venv/bin/material-promise`；未安装时用 `PYTHONPATH=src python -m material_plan.promise_cli`），在仓库根目录执行：

```sh
# 以下示例用源码目录直接运行；安装后把 MP 换成 .venv/bin/material-promise
export PYTHONPATH=src
MP="python3 -m material_plan.promise_cli"
DB=run/promise.db
mkdir -p run

# 1) 初始化固定主数据（路径已存在即拒绝，包括空文件）
$MP init --db "$DB" --source fixtures/promise/seed.json

# 2) 预约 A：Jan8 交 1 个 KIT（BOLT 现货 3 恰好覆盖），版本 0 -> 1
$MP apply --db "$DB" --source fixtures/promise/request_reserve_a.json

# 3) 再独立预约 B 必然失败：退出 2、stdout 为空、版本保持 1
$MP apply --db "$DB" --source fixtures/promise/request_reserve_b_fail.json; echo "rc=$?"

# 4) 成功重放：相同完整请求（键顺序无关）返回预约当时的历史快照，版本不再增加
$MP apply --db "$DB" --source fixtures/promise/request_reserve_a.json

# 5) 失败回滚：撤销 A、预约 B、再撤销不存在的 Z，整批失败，A 仍 active，无 B 身份残留
$MP apply --db "$DB" --source fixtures/promise/request_rollback.json; echo "rc=$?"
$MP snapshot --db "$DB"   # version 仍为 1，orders 仅 A/active

# 6) 同批先撤销 A 再预约 B：成功，版本 1 -> 2，A=cancelled、B=active
$MP apply --db "$DB" --source fixtures/promise/request_cancel_a_reserve_b.json
$MP snapshot --db "$DB"
```

关闭进程后重新执行 `material-promise snapshot --db "$DB"` 得到与最后已提交状态字节一致的 JSON；重放旧请求仍返回历史快照，而当前快照显示 A 已撤销且 A 的 order_id 终身不可再预约。Python 接口为 `material_plan.promise.init_db / apply_batch / snapshot`，失败一律抛 `ValueError` 且不修改调用方传入对象。

多层共享子件与同日到货的样例见 `fixtures/promise/seed_shared.json` 与 `request_shared_demands.json`（两个父项 P1/P2 共享 SHARED，验证合并取整、父批量余量与同日到货可用）；`request_past_due.json` 验证释放日早于 as_of 的拒绝。集成测试：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 实物批次谱系与召回出库闸（material-trace）

净算与承诺簿给出的是建议/承诺；本模块另建一份独立的本地 SQLite 实物账，记录真实收货、实际完工与发货。召回沿**实际用料批次**传播（不是按 SKU 封禁，也不只看剩余库存），既阻断受影响成品继续投料/出库，也列出此前已发给哪些客户。它不读取、也不回写既有计划器的 `inventory_lots` 与承诺簿，绝不从净算建议自动造实物。完整字段与口径见 [数据契约](docs/contract.md) 的 v5 章节。

```sh
export PYTHONPATH=src
MT="python3 -m material_plan.traceability_cli"
DB=run/trace.db
mkdir -p run

# 1) 初始化固定配方主数据（路径已存在即拒绝，包括空文件）
$MT init --db "$DB" --source fixtures/trace/master.json

# 2) 收货 R×6、完工 S1/S2/K、发货 OUT1 给 C1：版本 0 -> 1
$MT apply --db "$DB" --source fixtures/trace/request_physical_build.json

# 3) 召回原料 R：blocked_by 覆盖 R/S1/S2/K，exposures 列出 X/OUT1/K×1
$MT apply --db "$DB" --source fixtures/trace/request_recall_x.json

# 4) 独立 RAW 批次 CLEAN 不受影响
$MT apply --db "$DB" --source fixtures/trace/request_receive_clean.json

# 5) X 有效时用 S2 完工再关闭 X：整批失败，退出 2，版本与余额不变
$MT apply --db "$DB" --source fixtures/trace/request_blocked_make_then_close.json; echo "rc=$?"

# 6) 另开 Y 指向 S1 再关闭 X：S2 恢复，K 仍受 Y 影响
$MT apply --db "$DB" --source fixtures/trace/request_recall_y.json
$MT apply --db "$DB" --source fixtures/trace/request_close_x.json

# 7) 用恢复的 S2 完工 K3 并发货 OUT2 给 C2，版本 5 -> 6
$MT apply --db "$DB" --source fixtures/trace/request_resume_shipping.json

# 8) 当前快照：保留 closed X 与 active Y；重放步骤 2 只返回原版本 1 的快照
$MT snapshot --db "$DB"
$MT apply --db "$DB" --source fixtures/trace/request_physical_build.json
```

安装后可直接用 `.venv/bin/material-trace`（把 `MT` 换成它即可）。失败一律退出 2、stdout 为空、stderr 有错误；成功 stdout 仅一份 JSON 快照加换行。Python 接口为 `material_plan.traceability.init_db / apply_batch / snapshot`，`db` 接受 `str` 或 `Path`，失败抛 `ValueError` 且不修改调用方对象。同一成功 `request_id` 以完整请求重放返回首次提交快照、不重复扣料；`snapshot` 永远反映当前召回与余额。集成测试：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 离线材料成本月末关账（material-cost）

工厂用 material-trace 记录实际收货、完工领料与发货；本模块另建一份独立的本地 SQLite 成本账簿，财务月末把采购成本沿**实际领料**分配到成品与已发货明细，交付可对账的期末库存估值与销售成本。发票晚到或更正时旧账单保持原样：已发走部分只在当前月入账差额（`adjustment`，可为负），仍在库部分按最新成本更新期末价值。只核算材料成本，不另计工费；绝不从净算建议或承诺簿构造实物。完整口径见 [数据契约](docs/contract.md) 的 v6 章节。

```sh
export PYTHONPATH=src
MC="python3 -m material_plan.costing_cli"
DB=run/cost.db
mkdir -p run

# 1) 初始化独立成本账簿（路径已存在即拒绝，包括空文件）
$MC init --db "$DB"

# 2) 首月：R 收货 3 件成本 100 分，领 2 件完工 S，向 O1 发 1 件
#    期末 R/S 各剩 1 件、价值 34/33，O1 销售成本 33，期末合计 67
$MC close --db "$DB" --source fixtures/cost/close_2026_01.json

# 3) 次月实物不变，R 更正为 104：期初 67、采购净增 4、O1 调整 +1、期末 70
$MC close --db "$DB" --source fixtures/cost/close_2026_02.json

# 4) 第三月 R 更正为 97，余料分别发至 A 与 O2：
#    A/O2 销售成本 33/32，O1 只补 -2，本期净额 63，期末 0
$MC close --db "$DB" --source fixtures/cost/close_2026_03.json

# 5) 已冻结账单保持原值；输出丢失可用 show 取回（与首次关账输出一致）
$MC show --db "$DB" --period 2026-01

# 6) 重复关账或跳月：退出 2、stdout 为空、账簿无部分变化
$MC close --db "$DB" --source fixtures/cost/close_2026_03.json; echo "rc=$?"
```

首期可选择任意有效 `YYYY-MM`，以后只能逐月连续关账。每份 close 输入为 `period`、`physical`（material-trace snapshot 的完整当期累计快照）、`cost_updates`（`{lot_id, total_cents, source_ref}`，指向收货批次的整笔采购成本新总额）；首次出现的收货批次当期必须有成本（允许明确为 0），旧批次未更新就沿用前次总额，已用尽批次仍可改价。实物快照整份核对（版本不倒退、旧事实不消失不改写、谱系无环、分配与发货一致、每批数量守恒）通过后才能参与关账；质量状态不参与估值与历史比较。每期账单满足“期初 + 采购净增 - 销售成本净额 = 期末”。

安装后可直接用 `.venv/bin/material-cost`（把 `MC` 换成它即可）。Python 接口为 `material_plan.costing.init_db / close_period / show_period`，失败抛 `ValueError` 且整期回滚、不修改调用方对象。集成测试 `tests/test_costing.py` 覆盖三个月验收、历史分摊顺序（先发 Z 隔月再发字典序更早的 A）、多来源与多层耗尽批次、零金额分配、实物历史改写与整期回滚、期间连续性、跨进程恢复与 CLI。

## 采购到货核销（material-reconcile）

仓库的 material-trace 实物账记下了真实收到的批次，但没有采购单行信息；同一供应商的两张采购单可能同时到货，送货单只写料号。本模块另建一份独立的本地 SQLite 核销库：按收货与验收证据核对哪些采购行可能已履约，并为财务记录每行已授权放行的合格品数量。证据不能唯一区分采购单时，绝不替调用方挑一个匹配就放款，也不会把已放行的量在下次导入时悄悄归到另一行。完整口径见 [数据契约](docs/contract.md) 的 v7 章节。

```sh
export PYTHONPATH=src
MR="python3 -m material_plan.reconcile_cli"
DB=run/reconcile.db
mkdir -p run

# 1) 初始化采购单（路径已存在即拒绝，包括空文件）
#    P1 收货窗 1/1–1/5、P2 窗 1/3–1/8，同供应商同 SKU 各订 5 件
$MR init --db "$DB" --source fixtures/reconcile/orders.json

# 2) 提交完整证据：F 于 1/2 收 3 全合格、A 于 1/4 收 4 其中 2 合格、B 于 1/6 收 3 无合格品
#    F 只能入 P1、B 只能入 P2、A 必须各分 2 件：
#    两行收货范围均 [5,5]，P1 合格 [3,5]、P2 [0,2]，版本 0 -> 1
$MR reconcile --db "$DB" --source fixtures/reconcile/evidence_initial.json

# 3) 同一申请为 P1 放 3 且为 P2 放 1：P2 合格下限为 0，整批失败，退出 2
$MR approve --db "$DB" --source fixtures/reconcile/approve_overreach.json; echo "rc=$?"

# 4) P1 单独放行 3：版本 1 -> 2，输出本次凭证
$MR approve --db "$DB" --source fixtures/reconcile/approve_p1.json

# 5) 新证据认领 A 的 2 件合格品到 P2：P2 合格变为 [2,2]，版本 2 -> 3
$MR reconcile --db "$DB" --source fixtures/reconcile/evidence_claimed.json

# 6) P2 放行这 2 件：版本 3 -> 4
$MR approve --db "$DB" --source fixtures/reconcile/approve_p2.json

# 7) 去掉该认领：完整分配仍存在，但 P2 合格下限 0 < 已放行 2，
#    整份更正失败，旧证据与旧凭证保留
$MR reconcile --db "$DB" --source fixtures/reconcile/evidence_unclaimed.json; echo "rc=$?"

# 8) 旧凭证无论当前版本如何都按原申请重放，不重复放行、不加版本
$MR approve --db "$DB" --source fixtures/reconcile/approve_p2.json
$MR show --db "$DB"
```

安装后可直接用 `.venv/bin/material-reconcile`（把 `MR` 换成它即可）。Python 接口为 `material_plan.reconcile.init_db / reconcile / approve / show`，`db` 接受 `str` 或 `Path`，失败抛 `ValueError` 且不修改调用方对象。`init`、`reconcile`、`show` 返回当前版本、逐采购行核对结果（收货/合格收货上下限、累计已放行、本次可放行）与已入库凭证；`approve` 只返回本次凭证。集成测试 `tests/test_reconcile.py` 覆盖验收全流程、部分认领不排除余量、多批汇总才暴露的容量冲突、同日边界、供应商/SKU 不匹配、清零批次按原收货量、制造批或改写批不可冒充收货、单批大数量、证据更正与放行保证、凭证重放与冲突、整份回滚、跨进程恢复与 CLI，并以固定种子随机实例对拍暴力枚举验证上下界口径。集成测试：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
