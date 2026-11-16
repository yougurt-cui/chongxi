# 解决方案知识库

多模型知识采集页新增 `solution`（解决方案知识库），与选粮、健康和营养知识库并列，复用问题提交、模型选择、联网开关、任务筛选及提示词模板管理。

默认模板：

- 问题解决与执行步骤：适用条件、目标、顺序步骤、理由、验证与停止条件。
- 记录与验证方法：具体记录字段、时间节点、估计日期和回顾方式。

例如输入“我该怎样记录换粮开始的时间？”，结果应区分首次喂新粮、混喂与完全更换等节点，并给出记录字段及回顾方式，不预设已发生日期，也不改成选粮推荐。

模型返回 `{knowledge_base:"solution", question:"...", solutions:[...]}`。每条方案包含 `title/problem/applicable_conditions/goal/steps/record_fields/verification/stop_conditions/source_urls/evidence_status`。`steps` 是按执行顺序排列的 `{action,reason}` 数组。格式由采集器统一校验，错误响应保留原文。

采集批次和原始回答仍写入 `chongxi_model_knowledge.knowledge_import_batch`。解决方案写入同库的独立表 `solution_knowledge_point`，通过 `batch_id` 追溯问题、模型、提示词快照和原始回答；数据库初始化按需建表。自动解析和页面“重试解析入库”使用同一专用导入器，不进入选粮条件标准化、产品推荐或健康原因入库路径。

同一批次的相同方案精确去重，重复入库保留行及审核状态；内容变化且已有人工审核记录时拒绝自动覆盖。所有新方案默认 `pending`，不因模型声称 supported 就提升审核状态。不同模型的方案独立保留。

任务列表和解析状态按解决方案表计数；详情展示目标、步骤、字段、验证方法、停止条件、来源与审核状态。知识库只负责采集管理，管家在线回答检索的接入是独立功能。

验证命令：

```bash
python -m pytest tests/test_solution_knowledge.py tests/test_multi_model_prompts.py tests/test_multi_model_result_count.py tests/test_health_import_routing.py tests/test_product_recommendation_pipeline.py -q
```
