# 解决方案知识库

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
