# 健康原因分类汇总

数据库：`chongxi_model_knowledge`。

`health_knowledge_point` 按 `(species_code, breed_code, life_stage_code, issue_code, cause_type)`
唯一归档。`cause_type` 为 `diet`、`feeding_behavior`、`physiology`、`environment`、`disease`。
字段优先排列对象、健康问题、原因分类，再放总结内容、从属字段、来源和总结元数据。

`cause` 和 `mechanism` 是同类原因的可读总结。`causal_chains` 保留每个独立原因
及其自己的机制、伴随表现、支持/反向依据、需补充信息和营养关联。
不同独立原因不会仅因分类相同而合成一个原因。
汇总行的 `manifestations` 不按原因分组，直接合并为去重的表现字符串数组，不添加原因前缀。

模型只能通过原始明细 ID 和数组下标引用从属字段，最终内容从原文提取，
不能新增营养指标或检查结果。每条来源和每个从属数组元素都要求有引用。
`source_count`、`model_count`、`sources`、`source_urls` 用于追溯。
`summary_meta` 保存输入指纹、模型、Token 用量、分歧、引用修正记录；均为待审核候选知识。

原 `health_knowledge_point` 无损改名为 `health_knowledge_point_detail`，原 ID 和数据保留。
采集解析、批次计数、重试入库及管家的原始证据读取均使用明细表。
健康入库结束后自动刷新分类汇总；输入指纹不变时复用结果，不重新调用模型。
任务详情 `health_structured.points` 返回分类汇总，`source_points` 返回原始原因，
`point_count` 和 `source_point_count` 分别表示汇总行数和来源行数。

模型每组最多两次调用，SDK 隐式重试关闭，每次最多 4500 输出 Token。
付费响应单独记录于 `var/multi-model/health-summary-usage.jsonl`。
暂停开关 `var/knowledge-standardization.paused` 同样阻止总结调用。
单项原文数组的唯一可确定下标允许归一到 0 并记录；多项数组不猜测下标。

2026-11-11：原始 11 条记录汇总为 4 类、内部保留 7 个独立原因；
喂养行为组的引用修正已记录在对应 `summary_meta.notes`。
迁移及原始数据备份：`var/backups/health-cause-summary-20261111/`。
旧版针对原表结构的迁移/清空脚本不应直接重跑。
