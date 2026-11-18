# 宠物行为知识库

入口：`pipeline-review.html#multi-model` → 宠物行为知识库。

## 采集与解析分离

数据库：`chongxi_model_knowledge`。

| 表 | 用途 |
|---|---|
| `knowledge_import_batch` | 现有共享任务与批次记录；保留历史内容 |
| `behavior_model_call` | 行为库专属的原始模型调用快照：问题、模型、模板、实际请求提示词、原始回答、搜索来源、Token、耗时、调用结果，以及解析状态、版本、次数、错误与历史 |
| `behavior_knowledge_point_detail` | 独立解析结果，通过`source_call_id`关联调用记录，保留`batch_id` |

点击“开始运行”只调用所选模型并保存原始回答。即使返回文字不是合法JSON，API调用成功仍显示采集成功，解析状态为待解析；不会自动创建知识记录。

点击任务中的“解析已保存的回答 / 重试”，或运行以下脚本，才会从`behavior_model_call.raw_response`解析知识。解析过程不调用大模型，不修改原始调用快照。解析状态写入同一调用记录的`parse_status`等字段，不改变`call_status`；历史保存在`parse_history` JSON数组中。重复解析相同内容不新增重复知识，既有人工审核状态保留；已审核内容发生变化时拒绝覆盖。

`behavior_model_call.request_prompt`记录新采集的实际请求全文；历史记录无法确定当时运行时的占位符替换细节，因此该列留NULL，同时保留原始`prompt_template`和问题，不伪造历史请求。

合并迁移脚本：`python scripts/merge_behavior_call_parse_records.py --apply`。执行前备份调用记录、旧解析记录及DDL；将旧解析历史完整保存到`behavior_model_call.parse_history`，核对后移除旧`behavior_knowledge_parse_run`表。可不加`--apply`先预览，重复执行不会重复计数。

常用解析字段：`parse_status`、`parse_source_hash`、`parser_version`、`parse_attempts`、`point_count`、`parse_error`、`parse_started_at`、`parsed_at`、`parse_history`。一条调用记录一行，最新解析结果直接查列，历史通过JSON数组查看。

## 独立解析脚本

```bash
# 只预览，不改表或数据
python scripts/parse_behavior_observations.py --task-id 539
# 备份后归档并解析已保存的回答
python scripts/parse_behavior_observations.py --apply --task-id 539
# 只处理一个模型批次
python scripts/parse_behavior_observations.py --apply --batch-id 539
# 历史数据迁移时仅补齐调用记录，不解析
python scripts/parse_behavior_observations.py --apply --archive-only
```

不传任务/批次参数则遍历行为库已结束的批次。API失败批次只归档，不解析；执行前将任务原始数据、现有知识和表DDL备份到`var/behavior-pipeline-backups/`。脚本不会产生模型用量。MySQL建表/扩展字段会隐式提交，不应将事务回滚视为撤销DDL。

## 解析结果

支持`behaviors`结构（行为理解、训练与环境调整）和`observations`结构（观察现象、正常场景、异常边界、候选健康关联）。不为观察现象补造训练步骤；来源、阈值、健康编码和关联均待审核，不写正式疾病关联表。

`behavior_knowledge_point_detail`一条模型来源知识一行，不强行合并不同模型的编码。可直接查询`species_code`、`breed_code`、`life_stage_code`、`observation_code`、`observation_name`、`normal_scenarios`、`abnormal_scenarios`、`unmapped_health_issues`。后三列保存完整JSON数组。展开列从本表的**解析结果快照**`point_data`生成，不从调用表动态取值；采集新回答不会触发本表生成。未定义字段保留SQL NULL，未知品种和阶段不推断。

```sql
SELECT p.id, p.source_call_id, c.provider, c.model, c.task_id,
       p.species_code, p.breed_code, p.life_stage_code, p.observation_code, p.observation_name,
       p.normal_scenarios, p.abnormal_scenarios, p.unmapped_health_issues,
       p.review_status, p.parser_version
FROM chongxi_model_knowledge.behavior_knowledge_point_detail p
JOIN chongxi_model_knowledge.behavior_model_call c ON c.id=p.source_call_id
ORDER BY p.id;
```

模板可在页面编辑，历史任务保留完整模板快照。目前采集输入只提供问题文本，物种/品种/生命阶段、观察字典、疾病候选和参考资料占位符会明确替换成缺省上下文（未提供或空数组），不伪装已有资料。

行为知识检索接口`GET /api/admin/multi-model/behavior-points`沿用现有鉴权，支持`q`、`species`、`category`、`page`。任务详情分别展示调用状态和解析状态。

验证：

```bash
python -m pytest tests/test_behavior_model_pipeline.py tests/test_behavior_knowledge.py tests/test_parse_behavior_observations.py tests/test_multi_model_prompts.py tests/test_solution_knowledge.py -q
```


### 与健康知识表的维度对齐

运行 `python scripts/align_behavior_knowledge_dimensions.py` 预览，停止解析写入后加 `--apply` 执行。脚本备份原表数据和建表语句，验证原文、ID、审核状态及场景数组不变后切换表；重复运行无变化时不重建。

物理字段顺序为：`id` → `species_code` → `breed_code` → `life_stage_code` → `observation_code` → `observation_name` → `category_code` → `category_name` → 三个场景/未映射问题字段 → `evidence_status` → `review_status` → `source_count` → `model_count` → `sources` → `source_urls` → `summary_meta` → `updated_at` → 来源追溯字段、原始快照和创建时间。

物种、品种、生命阶段使用健康知识相同的已启用字典。未知猫/狗品种分别为 `unknown_cat` / `unknown_dog`；未指明阶段为 `unspecified`，明确全阶段才用 `all_stages`；无法准确映射的值为 SQL NULL，原值及原因保存在 `summary_meta`。原始模型字段仍保存在 `point_data`，不会改写模型原文。

共享状态使用 `evidence_status=candidate`、`review_status=pending/verified/rejected`；既有审核状态保留。行为明细每行只代表一个来源，`source_count=1`，模型来源的 `model_count=1`，文件来源为 0；不冒充健康知识表的跨模型聚合。`category_code/category_name` 是行为分类；`observation_code` 是观察现象编码，不改为健康问题的 `issue_code`。
