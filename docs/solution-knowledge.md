# 解决方案知识库

## 当前数据粒度（2026-11-17）

`solution_knowledge_point` 是场景主表，唯一键为 `species_code + breed_code + life_stage_code + need_code + scenario_code`。五项均为独立列；`scenario` 是保留原文的具体场景描述。一组对象与需求下的不同观测前提分别成行，同一组合的不同模型或不同任务只增加来源变体。

原候选表完整迁移到 `solution_knowledge_point_detail`，原 ID、batch_id、point_key、审核状态、raw_json 和 scenario_data 均不改写。`health_solution` 暂保留为兼容投影，新页面和任务列表使用新主表；模型批次计数使用明细表。

主表独立保存 possible_causes、steps、record_fields、verification、stop_conditions、source_count、model_count、sources、source_urls、审核状态。verification 直接保存四项验证对象；不再依赖隐藏在 scenario_data 中的字段。代表方案选最早的未拒绝完整来源，所有其他方案连同验证前提在 sources 中保留，不交叉拼接，不自动认定为共识或通过审核。

场景来源或内容未变时保留主表 ID 和审核状态；已审核主记录发生变化时整个导入事务回滚。未知范围保留 unspecified，不扩大成适用全部；all 生命阶段规范为 all_stages。通用旧格式无法明确对象/场景时仅保留在明细层，不虚构主表维度。

显式需求及历史场景编码映射保存在 `vendor/chongxi_health_solution/scene_aliases.json`。迁移只按登记映射统一编码，不改写来源，不做模糊文本合并。现有 15 条场景因时间、年龄或伴随表现不同分别保留；相同五维组合由数据库唯一键确保不会重复。

### 迁移命令

先停止 API 和采集写入进程，再执行：

```bash
python scripts/migrate_solution_scene_grain.py  # 只读预览
python scripts/migrate_solution_scene_grain.py --apply --backup-dir var/backups/solution-scene-唯一时间戳
```

脚本会检查没有运行中的解决方案采集，取得导入锁，备份原表与批次快照（before.json）并生成 restore.sql。复制候选后逐字段校验，再构建临时主表，检查每条来源均被覆盖，最后用 RENAME TABLE 原子切换。旧候选主表另保留为 solution_knowledge_point_legacy_scene_v1。完成后启动更新后的 API。重复导入不会重复生成主记录。

回退时停止写入，使用迁移前代码并执行备份目录的 restore.sql；原模型批次未被迁移改写。归档表仍可用于人工核对。若迁移在切换前失败，旧主表不变，先核对明细/暂存表，不覆盖或删除已有数据。

### 验证

```bash
python -m pytest tests/test_solution_scene.py tests/test_solution_knowledge.py tests/test_health_solution.py -q
SOLUTION_SCENE_MYSQL_TEST=1 python -m pytest tests/test_solution_scene_mysql.py -q
```

MySQL 集成测试仅只读复制正式库的解决方案到随机 test_solution_scene_* 临时库，测试备份迁移、逐字段来源保留、重复导入、跨模型同场景归并、不同场景隔离、主表任务计数及审核冲突回滚，最后清除临时库。

以下为旧格式与兼容投影的说明：

多模型知识采集页的 `solution`（解决方案知识库）复用问题提交、模型选择、联网开关、任务筛选及提示词模板管理。自动采集和页面“重试解析入库”进入同一个专用导入器，不经过选粮标准化或产品推荐路径。

## 场景格式

默认模板“问题解决与执行步骤”和“记录与验证方法”输出以下结构：

```json
{
  "knowledge_base": "solution",
  "question": "换粮后便便臭怎么处理？",
  "solutions": [{
    "species": "cat",
    "breed": "all",
    "life_stage": "all",
    "need": "digestive_health",
    "scenario_code": "stool_odor_normal_stool",
    "scenario": "换粮后便便臭但成形",
    "possible_causes": ["发酵底物变化"],
    "solution": ["记录换粮时间及粪便表现"],
    "verification": {
      "review_condition": "回顾记录时",
      "expected_result": "能够对照时间与表现",
      "failure_condition": "记录信息不足",
      "next_action": "补充缺失记录"
    },
    "source_urls": [],
    "evidence_status": "pending_verification"
  }]
}
```

种类支持 cat/dog；五维字段使用小写英文编码。未明确品种或生命阶段时使用 `unspecified`，不能自动推断为适用于全部的 `all`。场景、原因、动作和验证条件保留原文；验证条件必须包含上述四个字段，动作数组不能为空。无效响应保留原始回答并报告解析失败，不进行部分入库。

## 存储和来源

默认独立数据库为 `chongxi_model_knowledge`：

- `knowledge_import_batch`：问题、模型、提示词快照、原始响应和解析状态。
- `solution_knowledge_point`：每个模型的方案候选，通过 batch_id 追溯来源；`scenario_data` 保留原始场景结构，`raw_json` 保留完整方案，包括链接和证据状态。
- `health_solution`：按种类、品种、生命阶段、需求、场景编码唯一聚合。原始业务字段与用户 SQL 一致，并增加 sources/source_count/model_count/input_hash。

同一场景的多个来源保留完整变体，不交叉拼接步骤。主记录优先保留原有 SQL 初始化方案，否则使用最早的完整模型方案。`sources` 保存原始 SQL 或模型来源以及各自完整场景内容；模型候选保留来源链接。来源更新、候选更新和场景更新在同一事务完成，同库解决方案导入通过数据库锁串行处理。

重复解析保留未变候选的 ID、审核状态和来源。已审核候选或场景发生内容/来源变化时拒绝覆盖，整体回滚。场景表新记录为 `pending_review`，候选表新记录为 `pending`，不会因模型返回 supported 自动通过审核。

任务列表计数是模型候选数，详情额外返回场景表及去重后的场景数量，展示场景、原因、动作、四项验证、模型来源、场景记录 ID 和审核状态。

## 初始化与兼容

运行时仅执行 `vendor/chongxi_health_solution/schema.sql` 建表，并为已有表补齐来源字段。`init.sql` 是用户原始脚本副本，其中 5 条示例不会被自动执行。目标库由项目独立模型知识库连接选择，不依赖脚本中的 USE。

旧通用格式 `title/problem/applicable_conditions/goal/steps/record_fields/verification/stop_conditions/source_urls/evidence_status` 继续支持，仍保存到候选表，不强行推断场景编码。已保存的自定义提示词不自动覆盖；如其仍输出通用格式，可在提示词管理页改成新场景格式后重新采集。

当前功能负责采集与入库；管家在线问答的解决方案检索需单独接入。

## 验证

```bash
python -m pytest tests/test_health_solution.py tests/test_solution_knowledge.py tests/test_multi_model_prompts.py tests/test_multi_model_result_count.py tests/test_health_import_routing.py tests/test_product_recommendation_pipeline.py -q
```

MySQL 集成验证使用隔离临时数据库执行原始 SQL，覆盖 5 条示例、已有表升级、重复导入、多模型同场景归并、新场景创建和人工审核冲突回滚；不向正式库写入示例数据。

## 自定义模板兼容约束

解决方案调用模型前，会在选定模板（包括历史自定义模板）末尾附加 `templates/solution_output_contract.txt`，明确顶层包装、场景编码、枚举、验证条件和 JSON 转义规则。用户模板原文及已保存的历史任务快照保持不变；`{{question}}` 替换为原始问题，旧模板中的可选参数占位符替换为未提供说明，`scenario_count` 默认 5。

解析器仅在任务明确属于 solution 时启用兼容：从任务补齐缺失的知识库和问题，归一 feline/canine 等明确种类别名；两种历史 pending_review 证据值降为 pending_verification，其他非法枚举仍拒绝。缺少场景编码的旧回答使用 `legacy_` 加规范化场景原文的 SHA256 前 32 位作为临时身份，仅支持同原文重试去重，不代表不同措辞的语义归并。明确给出的场景编码不改写。

JSON 有前言或代码围栏时可提取完整外层对象；外层损坏时不再尝试把内部 verification 等对象当作完整响应，会报告 JSON 错误行列。仅对字符串内成对中文短语（如 `"跟着猫走"、`）未转义的双引号补齐 JSON 转义，保留解码后的原文，并在批次 search_metadata.json_format_repairs 记录位置；缺逗号、截断或其他语法错误仍拒绝。原始回答保留。

页面“重试解析入库”会对 solution 的 parse_failed 批次重新解析保存的原文，成功后继续共享入库流程；失败则保留原文与准确错误，不调用模型，不改写历史提示词。

## 当前来源存储（2026-11-19）

`solution_knowledge_point_detail` 已退役并物理移除。解决方案链路使用三张表：

- `knowledge_import_batch`：原始模型调用和解析快照 `solution_parse_result`。
- `solution_knowledge_point`：页面使用的五维场景主表。
- `health_solution`：管家读取的兼容场景方案表。

解析快照包含 `version`、`points`、`point_count`、处理时间或迁移标记。`points` 保留原来源内容、point_key、审核状态、时间和原始场景结构；旧 ID 保留，新来源使用 batch ID 与内容哈希组成的稳定字符串 ID。原始回答、提示词和模型信息仍在批次字段中。来源读取不再依赖独立明细表；任务计数从快照 points 数组计算。

每次成功解析后同一事务内更新批次快照和两张场景表，沿用导入锁、五维归组、显式编码映射和代表方案规则；不新增大模型融合调用。重复解析保留 ID 和审核状态，已审核内容变化仍拒绝并回滚。通用旧格式方案保留在批次快照，不能明确场景的内容不虚构场景主记录。

停止 API/采集后执行 `python scripts/remove_solution_source_detail.py` 预览，加 `--apply` 迁移并移除旧表。脚本在 `var/solution-source-backups` 保存旧表、两张场景表和原始批次备份，逐字段验证来源快照并确认两张场景表完全不变后删除旧表。再次执行返回 already_removed，初始化不会重新创建旧表。上文旧明细表说明仅适用于迁移前结构。

## 千问二次总结（2026-11-19）

`solution_knowledge_point` 现在由千问基于保存的模型来源生成候选总结，不再仅展示最早来源。
先按物种、品种、生命阶段、需求隔离对象范围，再调用千问对齐语义相近的场景。对齐后的场景编码仍组成五维唯一键；每个原编码必须完整、唯一归组，原文及不同前提均保留。未知范围不扩大为全部，年龄阶段不跨组。
归组后总结可能原因、操作步骤、记录字段、停止条件及验证内容，保留原始不确定性与分歧。
所有来源、原因条目和步骤均须有合法引用并完整覆盖；不允许引用其他场景或已拒绝来源。
不同原始方案的步骤或 verification 不完全相同时，强制分成不同 plan，保持步骤和四项验证条件绑定。
主表 steps 带 plan_id/source_refs，verification 文本带方案编号；完整结构化总结及引用保存在 summary_meta。

summary_meta 包含 status、version、provider、model、processed_at、input_hash、notes、disagreements、
usage、source_index、structured_summary 和审计文件位置。原始模型方案继续保留在 sources 和批次的
solution_parse_result，health_solution 兼容表维持原有处理，不被本次总结改写。
总结结果始终候选待审核；已审核记录发生内容/来源变化时拒绝自动覆盖。
版本及来源哈希未变时复用已有总结，不额外调用模型。经校验的模型回答在
var/multi-model/solution-summary-cache 缓存，保存每次调用的原始回答、完成原因和用量，
事务失败后重试可复用已成功调用，避免重复付费。

每次解决方案入库后自动更新受影响的总结；手工“重新解析/入库”使用同一流程。
现有记录可用 `python scripts/summarize_solution_knowledge.py` 预览，加 `--apply` 后备份并执行总结。
脚本逐场景提交，失败保留该组旧结果，已完成组无需重跑。不会重新向原采集模型提问。

场景对齐调用及缓存位于 `var/multi-model/solution-scene-alignment`，其模型、版本、理由、原编码和用量保存在结果 `summary_meta.scene_alignment`。主表场景字段展开全部来源前提；每个总结方案通过 source_ids 关联自己的 source_index.scenario。页面逐方案展示适用前提、步骤和验证，不能把一套方案的措施套用到另一套前提。已有不同编码的待审核旧行仅在新组总结成功后退役；已审核旧行不会自动覆盖或删除。
