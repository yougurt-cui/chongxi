行为知识库总结流程（2026-11-19）

采集原文与调用状态保存在 knowledge_import_batch；解析状态、历史及旧调用归档保存在 behavior_parse_result JSON。behavior_model_call 已备份迁移后删除，不再创建。

behavior_knowledge_point_detail 保留每个模型的结构化来源。解析完成后自动调用千问，将同一物种、品种、生命阶段、分类及行为名称的来源总结到 behavior_knowledge_point。名称一致的不同编码可归为一组；物种、品种、阶段未解析成功时不跨来源合并。生命阶段 unspecified 与 adult 等具体阶段分开，不推断为全阶段。名称不同的同义行为暂不自动合并。

总结字段包括 normal_scenarios、abnormal_scenarios、unmapped_health_issues，每条有 text 和 source_detail_ids。sources 保留完整原始知识、模型和批次；source_count 为明细数，model_count 为不同 provider/model 数。summary_meta 保存总结模型、版本、时间、输入指纹、Token 用量、分歧及审计文件。结果为待审核候选。

相同来源复用结果，修改来源会重新总结。模型失败保留明细与既有总结，在 behavior_parse_result.summary_error 记录原因，可通过任务重试。已审核总结不会自动覆盖。

页面行为检索支持“千问总结 / 模型来源明细”切换，任务详情同时展示两者。GET /api/admin/multi-model/behavior-summary-points 支持 page、q、species、category、task_id。

迁移预览：python scripts/migrate_behavior_summary.py
停止旧采集进程后迁移：python scripts/migrate_behavior_summary.py --apply
对现有数据执行总结：python scripts/migrate_behavior_summary.py --apply --summarize
备份：var/behavior-backups；模型每次回答及缓存：var/multi-model/behavior-summary。
