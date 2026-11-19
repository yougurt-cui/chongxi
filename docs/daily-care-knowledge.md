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
