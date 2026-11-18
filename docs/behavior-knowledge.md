# 宠物行为知识库

入口：`pipeline-review.html#multi-model` → 宠物行为知识库。

支持“行为理解与诱因分析”“训练与环境调整”两类种子模板，复用已有模板管理、模型选择、联网搜索与后台任务流程。提示词可以在页面编辑；历史任务保留提交时的完整模板。

模型输出使用 `knowledge_base=behavior`、`question` 和 `behaviors` 数组。每条记录包含行为名、物种、品种、生命阶段、分类、行为表现、可能原因、判断所需信息、干预步骤、观察记录字段、停止条件、来源网址与证据状态。校验失败保留原始回答；“解析 / 重试入新库”可重新解析保存的回答，不重新调用模型。

也支持观察现象模板的 `observations` 结构：保留现象编码、领域、类别、常见表达、观察特征、鉴别现象、正常场景、异常边界、风险等级、候选健康关联和未映射健康问题。不会为该结构补造训练步骤。现象与健康编码、来源和模型给出的阈值均待核验，只保存候选内容，不写正式疾病关联表。模型给出的适用范围也不等于用户个体事实。

当前输入只提供问题文本。行为模板中的 `species`、`breed`、`life_stage`、`existing_observations`、`disease_candidates`、`reference_materials` 占位符会明确替换为缺省上下文（未提供或空数组），不伪装为已有字典或本地疾病知识；`question` 使用本次问题。历史提示词快照保持原样。

## 展开观察现象字段

```bash
# 只检查数据、预览SQL和样例，不创建或修改表
python scripts/parse_behavior_observations.py
# 备份后执行，重复执行不会重复新增列
python scripts/parse_behavior_observations.py --apply
```

脚本在原表 `behavior_knowledge_point_detail` 新增7个存储生成列：`breed`、`life_stage`、`observation_code`、`observation_name`、`normal_scenarios`、`abnormal_scenarios`、`unmapped_health_issues`。`species`沿用已有列。仍是一条模型来源知识一行，以`id`、`batch_id`追溯，不强行合并模型之间不同的现象编码。后三列保留完整JSON数组，不拆分或丢失场景条件、风险、关联等子字段。

生成列自动从`point_data`提取，历史数据立即展开，新数据和后续JSON修改也会自动同步；生成列不能直接赋值，应通过原有入口维护`point_data`。非观察现象记录缺失的现象字段为SQL NULL，空数组仍为`[]`，不猜测未知品种或生命阶段。脚本不会进行字典标准化或模型推断。

执行前备份原表DDL及记录到`var/behavior-observation-backups/`，完成后逐条验证源数据、审核状态和展开字段。MySQL DDL会隐式提交，失败时应检查报告和备份，不将事务回滚当作撤销DDL。

```sql
SELECT id, batch_id, species, breed, life_stage,
       observation_code, observation_name,
       normal_scenarios, abnormal_scenarios, unmapped_health_issues
FROM chongxi_model_knowledge.behavior_knowledge_point_detail
ORDER BY id;
```

数据保存到独立模型知识数据库的 `behavior_knowledge_point_detail`，通过 `batch_id` 关联任务、模型、问题、原始回答和搜索来源。知识全部默认待审核，模型无法设置审核状态。同批次内容指纹去重，重复入库保留人工审核结果；已审核内容发生变化时拒绝覆盖。新表由现有数据库初始化流程创建。

`GET /api/admin/multi-model/behavior-points` 使用现有管理员鉴权，支持 `q`（最多200字）、`species`（cat/dog/unknown）、`category` 和 `page` 参数，每页20条。页面中的“行为知识检索”可按关键词、物种、行为分类筛选；各任务结果也显示结构化行为知识与来源。

验证：

```bash
python -m pytest tests/test_behavior_knowledge.py tests/test_multi_model_prompts.py tests/test_multi_model_result_count.py tests/test_solution_knowledge.py tests/test_health_solution.py -q
```
