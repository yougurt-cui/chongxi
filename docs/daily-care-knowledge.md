# 日常养护知识库

入口：`pipeline-review.html#multi-model` → 日常养护知识库（`daily_care`）。内置“日常养护与操作步骤”“养护计划与观察记录”两个可编辑提示词模板。

采用现有多模型后台采集。每个模型返回后先将原始回答、模型、提示词、问题、搜索来源、用量和采集状态保存到 `chongxi_model_knowledge.knowledge_import_batch`，再自动从保存的回答解析 `care_points`，写入独立表 `daily_care_knowledge_point_detail`。解析错误写入批次的 `import_error`，不覆盖原始回答或成功采集状态；任务详情可点击“重新解析 / 入库”重试，不重新调用模型。也可调用现有 `POST /api/admin/multi-model/tasks/<task_id>/import`。

每条知识包含 `species_code`、`breed_code`、`life_stage_code`、`care_code`、`care_name`、`category_code`、`category_name`、适用条件、护理频率、操作步骤、观察指标、注意事项、停止条件、证据/审核状态、来源链接、维度映射说明及原始解析快照。维度映射使用健康知识相同字典；未指明阶段为 `unspecified`，不推断全阶段。所有新记录为候选、待审核。

一条模型来源一行，通过 `batch_id` 追溯完整回答；以批次和内容哈希去重。重复解析不新增相同记录，不覆盖审核状态；已经人工审核的数据发生内容变化时拒绝替换。不会向行为、健康或选粮知识表写入数据。

检索接口：`GET /api/admin/multi-model/daily-care-points`，支持 `q`、`species`、`category`、`page`，沿用现有管理员鉴权。表随既有数据库初始化创建；不迁移或改写已有其他知识库数据。
