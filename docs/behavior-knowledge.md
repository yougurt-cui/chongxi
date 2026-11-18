# 宠物行为知识库

入口：`pipeline-review.html#multi-model` → 宠物行为知识库。

支持“行为理解与诱因分析”“训练与环境调整”两类种子模板，复用已有模板管理、模型选择、联网搜索与后台任务流程。提示词可以在页面编辑；历史任务保留提交时的完整模板。

模型输出使用 `knowledge_base=behavior`、`question` 和 `behaviors` 数组。每条记录包含行为名、物种、品种、生命阶段、分类、行为表现、可能原因、判断所需信息、干预步骤、观察记录字段、停止条件、来源网址与证据状态。校验失败保留原始回答；“解析 / 重试入新库”可重新解析保存的回答，不重新调用模型。

数据保存到独立模型知识数据库的 `behavior_knowledge_point_detail`，通过 `batch_id` 关联任务、模型、问题、原始回答和搜索来源。知识全部默认待审核，模型无法设置审核状态。同批次内容指纹去重，重复入库保留人工审核结果；已审核内容发生变化时拒绝覆盖。新表由现有数据库初始化流程创建。

`GET /api/admin/multi-model/behavior-points` 使用现有管理员鉴权，支持 `q`（最多200字）、`species`（cat/dog/unknown）、`category` 和 `page` 参数，每页20条。页面中的“行为知识检索”可按关键词、物种、行为分类筛选；各任务结果也显示结构化行为知识与来源。

验证：

```bash
python -m pytest tests/test_behavior_knowledge.py tests/test_multi_model_prompts.py tests/test_multi_model_result_count.py tests/test_solution_knowledge.py tests/test_health_solution.py -q
```
