# 多模型知识采集：两张表

入口 `/pipeline-review.html#multi-model`，沿用工作台 data_admin/member 或 X-Admin-Token 权限。采集数据只使用独立库 `chongxi_model_knowledge` 的两张表；标准词典在独立库 `chongxi_knowledge_dictionary` 中。

## 数据结构

`knowledge_import_batch` 每个模型响应一条记录，通过 task_id 关联同次采集。保存问题、模板、模型、原始响应、解析 JSON、联网来源、用量和错误。

`knowledge_point_source` 将适用条件映射拆成独立列，不再包含 applicable_conditions、standardized_mappings、notes、topic 列：

| 字段 | 用途 |
|---|---|
| id | 主键 |
| batch_id | 关联采集批次 |
| source_provider | 原始答案提供商：qwen / deepseek / doubao / file，来自批次 provider，不是执行标准化的模型 |
| original_condition | 完整原始适用条件 |
| species_code | 物种编码；未知为 null |
| breed_code | 品种编码；未知为 null |
| life_stage_codes | 生命阶段编码 JSON 数组 |
| need_codes | 用户需求编码 JSON 数组 |
| category_code | 知识分类编码 |
| topic_code | 知识主题编码 |
| content | 原始知识正文，标准化不修改 |
| standardization_meta | 状态、模型、版本摘要、时间、错误和原始条件/主题输入 |

七个映射字段沿用 dict_mapping_example 的对应字段，移除备注；阶段和需求保持 JSON 数组以支持多个值。category_code 对应 dict_knowledge_category，(category_code,topic_code) 对应 dict_knowledge_topic。分类和主题直接由 category_code、topic_code 两列表示。

每个适用分支一行。若一条原始知识产生多组映射，则展开为多行，正文逐字复制，不做笛卡尔积。首次展开复用原始记录 id；重新标准化按映射数量更新、追加或删除分支，不会无限追加重复行。未处理/失败记录仍使用同样的列结构，未知编码为 null/空数组，并有明确状态。

## 入库及标准化

五维采集条目先逐条拆解。同一响应内按原始条件、原始主题、正文精确去重，不合并不同模型。原始响应保存在批次中，原始条件及主题同时保存在 standardization_meta.raw_input，供重新标准化和重复解析判断使用。

新采集或文件导入在拆解入库后自动调用已配置千问模型按词典归一，不联网、不查询本地商品库，不修改正文。阶段未指明不等于全阶段；原始条件由程序逐字复制。不存在的需求编码会被舍弃，不强行匹配相近症状。模型可输出临时备注供校验，但不保存 notes 列。其他结构或关联错误最多纠正两次。成功表示映射通过结构和词典校验，不代表内容已审核或事实已验证。

重复解析保持已有映射与编号。原始输入或词典发生变化时重新标准化；同输入、同词典、同程序版本且成功的记录默认跳过，避免重复模型费用。按批次加锁，保存前检查原始输入是否改变；失败显示错误并保留待处理结构及正文。

## 接口及命令

- GET `/api/admin/multi-model/config`
- POST `/api/admin/multi-model/tasks`：`{question,template,providers:["qwen","deepseek"],web_search:true}`
- GET `/api/admin/multi-model/tasks?page=1`
- GET `/api/admin/multi-model/tasks/{task_id}`
- POST `/api/admin/multi-model/tasks/{task_id}/import`：重新拆解已有原始回答，不重新调用模型。
- GET `/api/admin/model-knowledge?page=1&provider=qwen&batch_id=1&q=编码或正文`：直接返回独立映射列。

重新标准化或失败重试：`python scripts/standardize_model_knowledge.py`，支持 `--batch-id 1` 和 `--force`。只重新解析后产生了新行时，可用该命令标准化。文件导入：`python vendor/chongxi_knowledge_importer/import_knowledge.py --input result.json`，`--dry-run` 只预览模型原始三元组。

当前十三列结构升级：先暂停采集服务，执行 `python scripts/simplify_knowledge_columns.py /新的备份目录`，先备份两表，再删除 notes/topic 并调整列序；保留其余列数据不变，不调用模型。此前结构升级脚本已停用，避免恢复旧字段。新库直接使用 schema.sql。最终顺序：id、batch_id、original_condition、species_code、breed_code、life_stage_codes、need_codes、category_code、topic_code、content、standardization_meta。

采集阶段仍支持供应商内置联网搜索：千问原生 enable_search/forced_search/enable_source；DeepSeek 联网模型使用 deepseek-v4-pro Responses web_search。联网状态及来源保存在批次内，与不联网的标准化步骤分开。

来源列升级：`python scripts/add_knowledge_source_provider.py /新的备份目录`，先备份，再从批次回填；新行从批次写入，拆分映射时继承原始来源。字段位于 batch_id 后面。

## 知识库分区（2026-11-09 改造）

采集入口按三个知识库分区，每个库有独立的提示词模版集合，任务与知识条目按库隔离展示。

| code | 名称 | 定位 |
|---|---|---|
| selection | 选粮知识库 | 主粮与零食的配方选择依据 |
| health | 健康知识库 | 症状观察、健康风险与就医边界 |
| nutrition | 营养知识库 | 营养素机制与成分功能 |

### 配置位置

- `vendor/chongxi_multi_model_mysql/knowledge_bases.json`：定义库的 code、名称、描述、问题占位符与模版清单（模版 code / 标题 / 描述 / 文件路径）。
- `vendor/chongxi_multi_model_mysql/templates/*.txt`：模版正文。新增模版只需加文件 + 在 JSON 注册，页面下次加载即生效，不需重启。

所有模版必须保留 `{{question}}` 占位符，且输出严格等于五维 JSON（`applicable_object`、`selection_points`、`product_recommendations`、`product_content`、`precautions`），否则 `import_knowledge.to_rows` 会拒绝解析。维度名固定，语义按各库口径在模版内重新定义。

### 数据隔离

`knowledge_import_batch` 新增 `knowledge_base VARCHAR(32) NOT NULL DEFAULT 'selection'`（`_ensure_database()` 内幂等 ALTER，历史数据默认归入选粮库）。`knowledge_point_source` 不重复存该字段，列表查询 JOIN 批次表获取。

### 接口变更

- `GET /api/admin/multi-model/config`：新增 `knowledge_bases`（库 + 模版全文）与 `knowledge_base`（默认库）；`template` 字段保留为默认库首个模版，兼容旧调用。
- `GET /api/admin/multi-model/tasks?knowledge_base=health`：按库过滤任务。
- `POST /api/admin/multi-model/tasks`：接受 `knowledge_base` 与 `template_code`，服务端按库解析模版正文（不再信任前端传来的模板文本；仍兼容直接传 `template`）。
- `GET /api/admin/model-knowledge?knowledge_base=nutrition`：按库过滤知识条目。

### 标准化说明

三库共用 `chongxi_knowledge_dictionary` 的全局主题词典。健康与营养类知识会映射到 `health_symptoms`、`feeding_nutrition` 等已有分类；若出现无法映射的主题，条目仍会入库，只是 `standardization_meta.status` 为待处理，需补充词典后再标准化。
