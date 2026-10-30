# 投稿初算与正式重算的调度衔接

适用于 `CHONGXI_VERSIONED_CALCULATION=1` 和现有 `CHONGXI_PIPELINE_WORKER=1`。

## 行为

- 品牌/产品/配方审核、单条及批量原料审核、已发布结果的依赖失效、后台“申请正式重算”，统一登记 `pipeline_recalculation_request`。每个任务一行，多次审核合并；身份审核保留比计算输入更早的重跑起点。
- 审核请求不直接清除节点输出或重置运行节点。当前任务仍在处理、等待异步回报或被其他 worker 持锁时，只记录等待原因。
- 当前轮次正常完成后，调度持有与 worker 相同的任务锁和配方锁，刷新输入及原料标准化元数据，确认配方身份、全部有效原料和科学属性审核通过后，再重置下游计算。准备阶段不执行评分或风险脚本。
- 轮次明确失败/停在待审核且无在途节点，也可在上述正式门禁通过后恢复。进程中断造成的“执行中断，等待核验”仍需人工核验，不自动重放。首轮尚未建立计算版本就被身份审核阻塞的任务，允许恢复首轮初算，避免误用正式门禁形成死锁。
- 品牌审核会清除原有产品/配方映射，此时允许先恢复身份整理节点，生成后续审核候选；正式门禁在创建新计算版本前检查，不会提前执行评分。否则旧草稿未确认会阻止新身份生成，形成互相等待。
- 节点重置和删除对应代次的排队记录在同一事务完成；准备期间收到的新审核不会被旧代次吞掉。任务取消后不会再次激活。进程重启后由数据库恢复待处理请求。
- 正式轮次在创建计算版本前和科学特征物化前检查门禁。审核导致正在计算的版本过期时，结束本轮执行、转待审核并登记请求，不立即反复重算；发布时仍沿用输入、依赖和执行令牌校验。
- 未通过检查的请求保留，60 秒后复查；新审核登记立即唤醒调度。规则补扫仅检查已发布版本，不抢先重置正在生成首份结果的任务。

## 部署与观察

应用初始化通过 `orchestrator_service.init_db()` 幂等创建新队列表，不需要删除或重建业务数据。部署时统一更新并重启 Web/worker 进程，避免旧审核接口继续直接重置节点。队列由现有 worker 消费，无需新增定时任务。

后台“初算待转正式”列表显示是否已登记及等待原因。队列中的 `node_code`、`generation`、`waiting_reason`、`next_check_at` 可用于排查。接口原有 `resumed_task_ids` 字段为兼容保留，现在表示已登记调度的任务，不表示计算已经启动。

实现与本地验证完成后，于 2026-10-30 部署生产，见下方发布记录。

## 验证

- `tests/test_pipeline_recalculation.py`：任务终态/在途节点、审核入口、严格门禁、撤销与旧 worker 保护等。
- `tests/test_pipeline_recalculation_mysql.py`：真实 MySQL 锁、重复请求、审核并发代次、事务回滚、取消、首轮恢复、后台列表，以及真实 worker/评分脚本生成正式发布包。
- MySQL 测试显式启用 `CHONGXI_RECALC_MYSQL=1`，强制主库和特征库为 `127.0.0.1:13306`，关闭常驻 worker；需要现有 `鲜鸡肉` 科学属性种子。测试只清理自行创建的任务、配方与结果，不调用 OCR/网络模型。

运行：

```sh
.venv/bin/python -m pytest -q tests/test_pipeline_recalculation.py
# 先使用本地 QA 数据库配置，再显式启用：
CHONGXI_RECALC_MYSQL=1 CHONGXI_PIPELINE_WORKER=0 .venv/bin/python -m pytest -q tests/test_pipeline_recalculation_mysql.py
```

### 2026-10-30 本地验证记录

- 完整回归：766 passed、46 subtests passed；未启用的数据库集成用例按约定跳过。
- 最终修改后专项回归：105 passed、2 subtests passed，包含 30 项新调度单测、11 项真实 MySQL 用例及已有计算/调度回归。
- 真实 worker 从排队接续到正式发布，六维画像和两类风险结果验证通过；品牌身份准备阶段不会在门禁未通过时创建计算版本。
- 管理后台内联 JavaScript 语法检查和 `git diff --check` 通过。未进行浏览器视觉验收。
- 测试身份与待重算记录清理后均为 0；未新增常驻进程，未部署生产。
- 工作区验证日志：`local-dev/formalization-scheduling-tests.log` 和 `local-dev/formalization-scheduling-integration-tests.log`（位于后端仓库的上级工作区）。

## 2026-10-30 生产发布记录

- 修复提交 `ff276ce` 已推送 `fix/submission-formalization-scheduling`，快进合并并推送 `main`，服务器通过 Git 从 `dcc1bf9` 更新到该提交。
- 发布前服务器工作区干净，执行中/待执行/等待异步回报的节点数均为 0。主库、特征库及运行配置备份到服务器私有目录 `/home/admin/backups/chongxi-release-20261030T115642Z`，数据库压缩备份约 176 MB。
- 更新前停止旧 Flask 进程，更新后编译检查、幂等创建待重算队列表并重启后端；保留原配置及其他服务进程。新进程确认开启 `CHONGXI_PIPELINE_WORKER=1`、`CHONGXI_VERSIONED_CALCULATION=1`，并发 1、补扫间隔 60 秒。
- 本机和公网 HTTPS 健康页、品牌接口均为 200；投稿状态接口与正式化管理接口匿名访问均为 401。
- 创建仅用于验证的已取消任务，真实后台调度在 21.09 秒内自动消费对应待重算请求；验证未执行 OCR、评分或用户业务，任务及节点、输出、队列记录均已清理。
- 检查发布后日志，调度扫描错误与重算准备错误均为 0。剩余 3 条历史请求的任务状态均为 `done`，等待原因为“产品、配方或原料身份尚未全部确认”，未提前重算。
- 发布与验证记录位于上级工作区 `local-dev/formalization-scheduling-release/`。新增的生产验证证明后台消费与门禁等待状态；完整评分与正式发布链路已在本地真实 MySQL 测试覆盖。
