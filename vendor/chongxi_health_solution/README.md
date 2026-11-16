# 场景解决方案结构

`init.sql` 是用户提供的原始脚本副本，包含 5 条示例及查询语句，仅供人工初始化参考。采集流程不会执行示例 INSERT。

`schema.sql` 是运行时建表结构，由项目的独立模型知识库连接执行，默认目标为 `chongxi_model_knowledge.health_solution`。若原表已经按 `init.sql` 创建，初始化会补齐来源审计字段，不改写原始内容。

五维唯一键：`species + breed + life_stage + need + scenario_code`。保留场景、原因数组、动作数组及四项验证条件，默认 `pending_review`。来源记录在 `sources`，包括批次、模型、问题及各来源完整方案；来源数和模型数分别写入 `source_count`、`model_count`。

同粒度方案保留一条主记录：已有 SQL 初始化内容优先，否则采用最早入库的完整模型方案。其他模型的完整方案保留在来源中，不拼接步骤。已审核内容或来源变化时拒绝自动覆盖，并回滚本次入库。

具体解析与兼容策略见 `docs/solution-knowledge.md`。
