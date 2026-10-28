# 需求 38：计算发布与投稿结果

第一阶段沿用现有评分算法和有界 worker，引入输入快照、唯一计算键和发布指针。品牌未知时使用私有草稿身份（brand/product active=0，formula status=draft / is_current=0），资料足够即可初算；审核没有改变有效输入和计算依赖时转正，品牌或营养/原料证据改变则重新计算。

## 部署与回滚

1. 备份主库及特征库。在应用目录、原有 Python 环境下运行 `python scripts/migrate_formula_calculations.py`。脚本是幂等的，不删除旧结果。
2. 所有 Web/worker 进程统一设置 `CHONGXI_VERSIONED_CALCULATION=1`、`CHONGXI_NUTRITION_STANDARD_VERSION=<实际营养标准版本>`；原有 `CHONGXI_PIPELINE_WORKER=1` 调度保持开启。重启进程，使算法文件指纹一致。迁移成功前不得打开开关；不要混跑两种模式的 worker。
3. 独立调度 `python scripts/run_formula_market_updates.py`，推荐每分钟或五分钟一次，并发限制 1。在部署主机运行 `bash scripts/install_formula_market_timer.sh` 可安装五分钟 systemd 调度与 CPU/IO 限额。下面的 systemd 示例需将路径和用户替换为实际部署值。上传 worker 不执行市场重建。未部署这个调度器会导致市场统计停留在旧版本。
4. 先部署后端并确认 `/api/miniprogram/food-submissions/<id>/status` 的鉴权与返回正常，再发布本分支小程序结果页与适配代码。页面刷新读取用户自己的投稿接口，沿用现有鉴权；不暴露管理员流程接口。
5. 小流量先验证未知品牌、已知重复、审核更正、规则变化和失败恢复，再扩大。历史正式粮在比较需要时才排队升级到版本化结果，不在迁移时全量抢占上传队列。
6. 回滚时停止新版本 worker、关闭 `CHONGXI_VERSIONED_CALCULATION` 并重启全部应用进程。保留新表以便恢复；旧工作表与已有正式目录仍可用，私有草稿继续不进入公共目录。新初算结果在旧界面下不可用。

```ini
# /etc/systemd/system/chongxi-market-updates.service（部署示例，尚未安装）
[Unit]
Description=Chongxi coalesced market updates
[Service]
Type=oneshot
User=YOUR_APP_USER
WorkingDirectory=/YOUR/APP/PATH
EnvironmentFile=/YOUR/APP/PATH/.env
ExecStart=/YOUR/APP/PATH/.venv/bin/python scripts/run_formula_market_updates.py
Nice=10
IOSchedulingClass=idle
CPUQuota=50%
TimeoutStartSec=70min
```

```ini
# /etc/systemd/system/chongxi-market-updates.timer
[Unit]
Description=Schedule Chongxi market updates
[Timer]
OnBootSec=2min
OnUnitInactiveSec=5min
Persistent=true
[Install]
WantedBy=timers.target
```

CPU/IO 配额只限制计算进程；数据库仍共用，需要观察慢查询和数据库负载，根据负载调整调度间隔或数据库资源组。

## 一致性与故障恢复

- `formula_calculation_run` 保存输入、数学依赖指纹及完整发布包；评分、营养、画像、风险按一个 result_id 读取。同一 Flask 请求固定选定的发布版本。临时工作表不作为未完成版本的读取回退。
- 唯一键为配方身份＋有序原料/营养证据（含单位、保证值符号、基准）＋品牌＋算法/知识/参考池依赖。同一配方的任务串行计算，不同上传任务可复用同一有效发布。不同未知身份不会跨配方共享风险缓存。
- 算法文件、原料科学属性、评分规则、参考池成员及成员特征、品牌反馈纳入依赖；时间戳和上传批次不影响数学指纹。当前每 60 秒扫描一次，每次最多重置 20 个过期配方。这是保守失效，尚未做原料到配方的精细依赖索引。
- 依赖分为基础评分与风险参考两部分。参考池成员自身重算会改变池内特征：风险计算前，在确认输入、评分规则和当前执行仍一致后，建立新的不可变运行版本，使用已完成的基础分计算风险，避免整条评分重复执行。基础规则变化仍需完整重算；风险发布再次验证依赖。
- `formula_calculation_current` 指针只在输入、依赖、任务状态、节点所有权和目标版本验证通过后切换。被取消或重置的旧任务不能发布。规则在计算中变化，最多自动完整重试一次，持续变化则留待核验。
- 输入变化时旧快照保留用于追溯，当前读取隐藏其结论；可显示新输入中已验证格式的营养保证值，并标识画像/风险缺项。未知科学属性不会补零冒充完整评分。
- 新发布包按维度范围验证分数（蛋白质量/蛋白/脂肪 0～1，纤维/碳水/益生元 0～5）；越界或非有限值在特征、画像分数及文案中均按缺项处理，并保留 `invalid_score:<字段>` 质量标记。原始工作分不改写，方便排查评分数据问题。
- 无内容变化的审核仅更新正式状态，保留分数和风险。身份转为不同品牌可能影响品牌风险，必须重算，不能只更改标题。
- 市场脏记录是持久队列，同配方多次更新合并。重建失败保留记录；成功仅删除本次观察到的 generation，重建途中新增的更新不会丢失。内部市场表仍按既有流程重建，并未整体改成原子多表快照。
- 已有任务中断仍沿用原 worker 的“核验后重跑”处理。没有新增无限重试；发布失败时已有不可变快照不被覆盖。

## 测试

普通回归：`.venv/bin/python -m pytest -q`。

真实 MySQL + 评分脚本：`CHONGXI_QA38_MYSQL=1 .venv/bin/python -m pytest -q tests/test_formula_calculation_mysql.py -s`。仅允许连接 `127.0.0.1:13306` 的现有 QA 主库/特征库；要求 OCR source 392 和科学属性种子数据。测试复制自己的输入，验证初算、五个计算节点复用、锁、无变动转正、营养修改与规则升级重算、市场失败隔离、历史基线升级；finally 清理自建数据。它跳过 OCR 网络调用，不是照片识别性能测试。

小程序：`npm run test:calculation-results`、`npm run test:contracts`、`npm run test:guardian-1020`。微信开发者工具固定使用上级工作区的 `local-dev/qa34-miniprogram`，不新建临时项目。

性能验收使用 `scripts/benchmark_formula_calculations.py`，由工作区 `local-dev/qa38-benchmark/run_comparison.py` 顺序驱动。必须准备独立 `127.0.0.1:13308` 数据副本与旧/新源码快照；脚本拒绝其他数据库目标。每次恢复同一数据，已知身份三个样本各三轮，分别测无有效发布与重复上传；另测三个未知身份样本。上传和 OCR 不在计时范围内，不能据此声称生产全链路耗时。目标为首次可读中位耗时减少至少 20%、旧版已有复用路径无明显退化、可计算的未知身份样本 15 秒内初算、六维原始值与有效展示值一致；记录任务结束与后台市场成本。

## 观测和剩余范围

结合 `pipeline_node_finished` 的耗时记录，观察上传→首个结果、转正时间、计算复用率、转人工率、市场队列积压。对不同样本分别测量 P50/P95，不能把单个本地样本当成生产收益承诺。

逐维度独立计算、未知原料下仍输出其他结构分、跨未知身份复用纯结构计算、精细依赖失效、常驻模型进程和独立计算并行留到下一阶段。当前部分输出是有来源的营养值与明确缺项，完整评分仍需现有科学属性门禁通过。尚未部署或开启生产开关。

## 第二阶段性能改动

- `scripts/run_formula_phase.py` 提供固定 `scores` / `risks` 两个阶段入口；单个短生命周期子进程按原顺序运行原脚本，复用库导入，完整链路子进程数8→3。各步骤保留独立耗时、日志尾部及成功/失败状态；失败停止后续步骤，阶段报告不完整则拒绝返回成功。临时报告随调用清理。没有跨任务共享脚本全局变量，也没有在Web线程修改环境/argv。阶段调用沿用600秒超时，发布检查继续阻止过期/取消任务发布。
- 执行器文件纳入算法指纹。部署后现有版本结果按已有机制失效/升级，不能只替换执行器而不重启进程。
- 数据库配置解析按三个配置文件的路径/inode/mtime/ctime/size缓存；环境变量每次读取，返回配置做深复制。实时评分规则仍从数据库读取。环境变量修改仍按原部署方式重启进程。
- 新状态接口只读投稿/发布元数据和节点状态，两条查询（不含鉴权与首次表结构初始化），不读取任务输出或结果JSON。查询限定当前账号；取消/拒绝/失效结果不会标记为可用。跨表任务ID使用二进制比较兼容已有表的排序规则差异。
- 小程序前台计算期间每1秒轻量刷新，状态revision改变才读取完整详情；初算等待人工核对逐步退避至30秒。同result_id的审核转正也会改变revision。正式结果完成、隐藏页面、切换账号时保留停止/隔离规则；网络异常退避，恢复计算时回到1秒。
- 完整结果未就绪时呈现“读取包装文字 / 整理配料与营养 / 生成分析结果”等用户阶段。列表仍轮转检查待处理项，1秒时效针对打开的投稿详情，不是所有列表项同时每秒请求。
- 关闭版本化开关只回滚发布模式；分阶段执行与配置缓存属于代码改动，如需回退这两项，应回退对应代码版本。后端状态接口要覆盖新小程序的使用周期。

新增回归：`tests/test_formula_phase_runner.py`、`tests/test_database_config_cache.py`、`tests/test_food_submission_status.py`。本地42次同轮对照5.98→3.44秒（OCR后首次可读中位数，降低42.5%），并非生产上传全链路数据。连接池、常驻进程、上传压缩和OCR容量拆分仍未实施。

## 生产因子对比兼容

部署前发现服务器已有尚未入库的 `guardian_factor_evidence_service.py` 与对应选粮流程改动。本次保留该功能并纳入版本管理：历史未版本化的配方沿用既有因子解释；已接管的配方只读取请求固定的发布包，连同主风险、支持标签和脂肪细项一并冻结。失效或未发布结果不回退工作表，不同计算版本不做因子比较。实时市场百分位没有纳入快照，因此版本化结果暂不显示该位次；社区观察继续单独标注未经核验。小程序发布前，旧版客户端仍可调用原接口。
