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

