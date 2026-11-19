# 日常养护知识库

入口：`pipeline-review.html#multi-model` → 日常养护知识库（`daily_care`）。内置“日常养护与操作步骤”“养护计划与观察记录”两个可编辑提示词模板。自定义模板未写 `{{question}}` 时，保存会在正文末尾自动补充“本次采集问题：{{question}}”，原有正文不改写；补充后仍须满足 30,000 字上限。

采用现有多模型后台采集。每个模型返回后先将原始回答、模型、提示词、问题、搜索来源、用量和采集状态保存到 `chongxi_model_knowledge.knowledge_import_batch`，再自动从保存的回答解析 `care_points`，写入独立表 `daily_care_knowledge_point_detail`。解析错误写入批次的 `import_error`，不覆盖原始回答或成功采集状态；任务详情可点击“重新解析 / 入库”重试，不重新调用模型。也可调用现有 `POST /api/admin/multi-model/tasks/<task_id>/import`。

每条知识包含 `species_code`、`breed_code`、`life_stage_code`、`care_code`、`care_name`、`category_code`、`category_name`、适用条件、护理频率、操作步骤、观察指标、注意事项、停止条件、证据/审核状态、来源链接、维度映射说明及原始解析快照。维度映射使用健康知识相同字典；未指明阶段为 `unspecified`，不推断全阶段。所有新记录为候选、待审核。

一条模型来源一行，通过 `batch_id` 追溯完整回答；以批次和内容哈希去重。重复解析不新增相同记录，不覆盖审核状态；已经人工审核的数据发生内容变化时拒绝替换。不会向行为、健康或选粮知识表写入数据。

检索接口：`GET /api/admin/multi-model/daily-care-points`，支持 `q`、`species`、`category`、`page`，沿用现有管理员鉴权。表随既有数据库初始化创建；不迁移或改写已有其他知识库数据。


## V3 / V4 活动格式

同时支持旧 `care_points` 与 `care_activities`；不能在一个响应中混用。V3 从顶层继承范围，V4 优先使用每条活动内的物种、品种、生命阶段、需求和任意明确子类型，不补造阶段或场景。原始活动保存在 `point_data.care_activity`，原始调用仍在共享批次表。所有新数据候选待审核。

养护表新增 `care_activity_code`、`care_activity_name`（兼容 `care_code/care_name`）、`need_code`、`care_subtype`、`care_domain`、`care_category`、`care_type`，以及独立 JSON 列 `schedule`、`tools_materials`、`methods`、`avoidances`、`observations`、`adjustment_rules`。这些列从本表解析快照生成，保留原文嵌套结构，旧格式缺少的列为 NULL；已有行、原文和审核状态不改变。初始化会按需补列。

字典维度中，明确的 `breed=all` 映射为 `all_cat/all_dog`，`life_stage=all` 映射为 `all_stages`，犬 `adult_dog/senior_dog` 对应公共阶段 `adult/senior`；原始标签仍保留。未知不视为全部。未匹配字典的候选代码保留原文，标准维度留 NULL，不伪造映射。

网站模板中的 question、species、breed、life_stage、existing_care_activities、existing_observations、reference_materials 会一次性替换；问题内的类似占位符文本不再二次替换。没有给定个体或资料时使用 unknown、空字典和未提供资料说明。

已有批次可以运行 `python scripts/parse_daily_care_activities.py --task-id TASK_ID` 预览；加 `--apply` 后备份并调用现有入库流程。默认不调用模型。重复导入不新增相同记录；有人工审核而原始内容变化时拒绝替换。指定不存在/未成功采集的任务或批次时明确报错。

`vendor/chongxi_care_import` 包含独立可运行的 JSON/离线 MySQL 导入工具、V3/V4 样例和说明。其本地文件 `--commit` 模式创建的是离线 `pet_care_knowledge`，不作为网站表；网站数据应使用 `--project-root` 配合任务/批次模式。

豆包联网长结构化输出默认使用 `max_output_tokens=32768`，可通过 `DOUBAO_SEARCH_MAX_OUTPUT_TOKENS` 调整。保存响应的完成状态、截断原因和当时输出上限。若响应为 incomplete，原文保留，入库明确报告回答被截断，不自动补造尾部内容；重复解析不重新调用模型，也无法补全缺失内容，需单独重新采集该模型。

## 来源明细与融合结果（2026-11-19）

以本节为当前表结构准则，替代上文旧字段/生成列说明。

- `daily_care_knowledge_point_detail`：每个模型、每个养护活动一条来源明细。保留标准维度 `species_code / breed_code / life_stage_code`、原始维度 `raw_scope`；统一使用 `need_code / care_activity_code / care_activity_name / care_subtype / care_domain / care_category / care_type / care_objective`，独立展开 `applicable_conditions / schedule / tools_materials / methods / avoidances / observations / adjustment_rules`。参考资料为 `source_urls / source_ids`，解析扩展和字典说明为 `extraction_notes`，保留证据、人工审核状态、批次、内容哈希和时间。移除 `care_code / care_name / category_code / category_name / frequency / steps / observation_fields / precautions / stop_conditions / summary_meta / point_data`，取消重复生成列。完整模型原文仍在 `knowledge_import_batch`，不复制到明细。
- `daily_care_knowledge_source_view`：明细关联批次的查询视图，直接包含 `source_provider / source_model / task_id / source_question`，避免在明细表重复存储调用信息。
- `daily_care_knowledge_point`：融合候选结果。按完全相同的问题、标准和原始范围、需求、活动编码、子类型、领域和类型归组，跨采集任务归集同一问题的来源。分类名称不参与归组，避免模型中英文标签导致人为拆分。未知范围保留原始值，不当作全范围；不同阶段和体内/体外子类型不合并。不同活动编码不做模糊语义匹配。
- `daily_care_knowledge_point_source`：融合结果与来源明细关联，每条当前有效来源最多归属一个融合组。被拒绝的明细不参与融合。

融合表独立展开名称、分类、目标、适用条件、六组养护字段、证据和参考来源等 JSON 列。每列为候选版本数组：`[{value, source_detail_ids, source_providers, source_models, support_model_count}]`，只合并完全相同的 JSON 值（忽略对象键顺序，数组顺序保留）。不同内容保留并列版本及来源，不混写成一份操作指南，也不裁定医学结论。各字段必须结合相同 `source_detail_ids` 对应的适用条件阅读；空值表示来源未提供，不视为事实冲突。`model_count` 和支持模型数按 provider/model 去重，同模型重复采集不算多个模型支持。

融合还展开 `source_count / model_count / source_providers / source_models / source_task_ids / has_differences / field_differences / fusion_method / review_status / content_hash / active / created_at / updated_at`。差异标记表示存在多个非空版本，不等于已判定事实矛盾。全部结果初始待审核；内容和来源完全不变时保留审核状态，来源/内容变化时转回待审核，失去全部有效来源的组保留历史但 `active=0`。

每次来源解析成功后，在同一事务内自动更新融合与关联，命名锁串行化并发模型写入；不额外调用大模型。解析或融合失败回滚整次明细写入，批次原文不受影响。页面支持“多模型融合结果 / 模型来源明细”切换，任务详情同时展示来源明细及相关融合结果（可包含其他任务的同组来源）。数据库视图 `daily_care_knowledge_source_view` 可直接查询明细及 `source_provider/source_model/task_id/source_question`，不在明细表重复存储批次已有信息。接口 `GET /api/admin/multi-model/daily-care-points?view=source|fused`，默认 source，检索分类现在使用 care_domain 编码。

迁移：停止采集后运行 `python scripts/migrate_daily_care_source_fusion.py` 预览，使用 `--apply` 执行。写入前备份原结构、全部旧明细和养护原始调用到 `var/daily-care-backups/source_fusion_*.json`；影子表逐字段校验后原子换表，旧表保留为 `daily_care_detail_backup_<时间>`。保留明细 ID、批次、去重哈希、人工审核和创建/更新时间。旧格式频率迁移到 `schedule.frequency_text`，未分阶段观察迁移到 `observations.unspecified`，不猜测观察时点。原模板与模型调用记录不变。脚本可重复执行，仅重建融合，不调用模型。

```sql
-- 直接查看来源模型
SELECT id, source_provider, source_model, task_id, batch_id, care_activity_name
FROM chongxi_model_knowledge.daily_care_knowledge_source_view;
-- 查看融合候选与差异
SELECT id, care_activity_code, source_count, model_count, schedule, field_differences
FROM chongxi_model_knowledge.daily_care_knowledge_point WHERE active=1;
```
