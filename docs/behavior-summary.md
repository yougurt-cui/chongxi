# 行为知识总结

`knowledge_import_batch` 保存行为采集原文；`behavior_parse_result` JSON 保存解析状态、历史和已迁移的旧调用归档。`behavior_model_call` 已移除，不会由初始化重新创建。

`behavior_knowledge_point_detail` 保留每个模型的原始结构化知识。
`behavior_knowledge_point` 由千问总结生成，按物种、品种、生命阶段、行为分类及行为名称分组。同名行为的不同编码可汇总；不同生命阶段不会混合，无法匹配对象字典的记录单独处理。

总结表维度顺序与明细一致，包含正常场景、异常场景、未映射健康问题、来源数量、模型数量、完整来源、来源网址和总结元数据。每个总结条目包含 `text` 和 `source_detail_ids`；`summary_meta` 包含模型、时间、输入指纹、Token 用量、分歧、说明与审计文件。结果默认待审核。原始行为编码及全部适用条件保留在 `sources` 中；展示编码采用首条来源的编码。

采集后自动解析并总结；相同来源指纹复用已有总结。解析和总结状态分别记录，失败保留原文并可重试。总结缓存与每次模型响应在 `var/multi-model/behavior-summary/`。任务详情展示总结及原始明细。

迁移先停止采集/API 工作进程：

```bash
python scripts/migrate_behavior_summary.py           # 只读预览
python scripts/migrate_behavior_summary.py --apply   # 备份、迁移、校验并删除旧调用表
python scripts/migrate_behavior_summary.py --apply --summarize # 同时总结历史数据
```

备份位于 `var/behavior-backups/`，包含旧表建表语句、全部调用、批次及原始明细。旧调用ID仅用于历史追溯，原记录保存在批次 JSON 的 `legacy_call`；新流程关联公共 `batch_id`。
