# 对象与分类知识汇总

数据库：`chongxi_model_knowledge`。

`knowledge_point_source` 每行一个对象下的一个知识分类，唯一键为
`(species_code, breed_code, life_stage_code, need_scene_code, category)`。

字段顺序：`id`、`species_code`、`breed_code`、`life_stage_code`、`need_scene_code`、`need_scene_name`、`need_scene_codes`、`need_relation`、`category`、
`content`、`sources`、`source_count`、`model_count`、`summary_meta`、`updated_at`。
原七个分类列改为 `category` 的值：`pet_profile`、`life_stage_nutrition`、
`digestive_tolerance`、`food_selection`、`product_recommendation`、
`product_suitability`、`product_overview`。

相同对象、阶段、需求场景和分类的多模型内容交由千问总结，合并同义观点，保留互补信息、
不同产品及其限制、模型间分歧和待核验状态。总结仍为候选知识，不能视为事实核验。
`content` 为可阅读的合并总结；`sources` 保存全部原文及明细 ID、模型和批次；
`summary_meta.points` 保留每条总结对应的原始 ID，覆盖该组全部输入来源。
`summary_meta` 同时记录输入指纹、总结版本、模型、Token 用量、分歧与审核状态。
每次付费响应还写入 `var/multi-model/category-summary-usage.jsonl`。

输入指纹未变化时复用总结，不重复调用模型。每组最多两次调用，SDK 隐式重试关闭，
每次输出最多 3000 Token。失败组保留既有总结，其余组结果可独立保存并复用。
暂停开关 `var/knowledge-standardization.paused` 同样阻止总结调用。

`knowledge_point_source_detail` 保留原始标准化明细。阶段数组拆成单阶段对象，
`all_stages` 不复制到具体阶段；不同品种、物种、阶段和分类不混合。
同一条跨阶段原文可以被多个汇总行引用，检索时按来源 ID 去重。
任务详情接口中的 `assembled_points` 返回分类行及来源，页面展示总结和原文追溯。
管家既有的明细检索继续读取 `knowledge_point_source_detail`。

2026-11-11：58 条明细归入 7 个对象、20 个分类行，全部完成模型总结。
迁移备份：`var/backups/category-summary-rebuild-20261111/`；
原宽表另存为 `knowledge_point_source_wide_backup_20261111`。
旧版宽表迁移和重置脚本不适用于当前结构，不应直接重跑。

## 需求场景与枚举清洗（2026-11-11）

需求场景来自提交到 `knowledge_import_batch.question` 的实际问题，三家模型共用
`dict_user_need`，不以模型自行回写的场景名称分组。
`food_selection` 为选粮、`food_transition` 为换粮过渡、`digestive_sensitivity` 为肠胃敏感、
`soft_stool` 为软便。肠胃敏感不自动等同于软便或食物不良反应。

`need_scene_codes` 是标准需求集合；多需求以 `need_relation=and/or` 保留共同限制或替代关系，
不做笛卡尔积。单需求场景使用其标准 code，多需求使用关系类型和规范编码集合的稳定哈希。
无法识别时用场景哨兵 `unspecified`，不表示适用于全部需求。

明细中的 `need_codes` 继续表示原文知识与条件标签，允许与提交问题的主场景不同。
代码大小写、明确同义词和别名统一归一；未知值不编造替代码，原值保留在审核元数据。
`scene_meta` 和 `standardization_meta.enum_cleaning` 记录场景来源与清洗前值。
仅追加字典项并完整复核合法编码后更新字典指纹，避免无必要的整批付费标准化。

本次 219 条明细完成清洗，64 条需求标签语义集合调整；生成 55 行场景分类汇总，
其中 19 行通过来源覆盖校验复用已有总结，36 组重新总结。
备份与清洗报告：`var/backups/need-scene-cleaning-20261111/`。
原无场景汇总表保留为 `knowledge_point_source_no_scene_backup_20261111`。
