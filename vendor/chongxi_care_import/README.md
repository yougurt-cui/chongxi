# 宠析日常养护解析工具（兼容 V3 / V4）

要求 Python 3.10+。默认只解析/预览，不调用模型，不写数据库。压缩包不含账号密码。

## 1. 本地 JSON 解析（可独立运行）

```bash
python import_care.py --input sample_input.json --output parsed_rows.json --batch example-001
python import_care.py --input sample_v4.json --output parsed_v4.json --batch example-v4
```

支持顶层作用范围的 V3，以及每条 `care_activities` 内设置物种、品种、生命阶段、need_code 和自定义 care_subtype 的 V4。每条原始活动对应一个候选场景，不推断、扩增阶段或体内/体外场景。六个知识 JSON 字段及原始活动完整保留。支持 JSON BOM、完整 Markdown JSON 代码块以及包含 raw_response/raw_text 的导出对象。

活动字段优先于顶层；unknown 表示未知，all 表示明确的全部范围。体内/体外不等同于口服/外用。不再把所有 preventive_care 都映射成寄生虫预防。同编码不同条件/内容不会互相覆盖，完全相同的活动会去重。

## 2. 解析网站已经采集的数据（推荐）

先把后端更新到包含 `scripts/parse_daily_care_activities.py` 的版本。网站继续使用独立数据库 `chongxi_model_knowledge` 和现有表 `daily_care_knowledge_point_detail`，共享原始回答在 `knowledge_import_batch`。

```bash
cd /home/admin/projects/chongxi
# 预览所有已成功采集的日常养护批次，不改库、不调用模型
.venv/bin/python scripts/parse_daily_care_activities.py
# 指定真实的任务 ID 预览
.venv/bin/python scripts/parse_daily_care_activities.py --task-id TASK_ID
# 核对后备份并入库
.venv/bin/python scripts/parse_daily_care_activities.py --task-id TASK_ID --apply
# 也可指定模型批次 ID
.venv/bin/python scripts/parse_daily_care_activities.py --batch-id BATCH_ID --apply
```

`TASK_ID` / `BATCH_ID` 替换为页面中的真实数字。无需提供数据库密码，读取后端现有 `.env` 配置。执行前将原始批次、现有知识及 DDL 备份到 `var/daily-care-backups/`。不覆盖原始回答，不重置审核，不重复新增相同记录。错误会列出，且进程返回非零状态。

从解压目录调用同一网站流程也可以：

```bash
python import_care.py --project-root /home/admin/projects/chongxi --task-id TASK_ID --output preview.json
python import_care.py --project-root /home/admin/projects/chongxi --task-id TASK_ID --output result.json --commit
```

网站新任务在模型回答保存后自动执行同一解析；旧 `care_points` 格式继续支持。V4 所需的输入占位符会替换为已知问题或明确的 unknown/空字典，不编造参考资料。生成知识一律为候选待审核，原始 evidence_status 保留在快照中。

## 3. 独立 MySQL 导入（可选，不是网站入库入口）

不提供 `--project-root`，仅使用本地 JSON 文件并加 `--commit` 时，工具会写独立的 `pet_care_knowledge` 表。这是离线宽表，不会自动出现在网站的养护检索里。要写网站请用上面的任务/批次模式。

```bash
pip install -r requirements.txt
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_USER=your_user
export MYSQL_PASSWORD='your_password'
python import_care.py --input input.json --output rows.json --batch source-001 --db-name your_import_database --commit
```

首次提交自动按 `schema.sql` 建表，并兼容旧表补充 `source_scope`。默认数据库为 `MODEL_KNOWLEDGE_DATABASE` 或 `chongxi_model_knowledge`，不误用业务库的 MYSQL_DATABASE。DDL 在 MySQL 会隐式提交，旧表应先备份。

知识键包含来源批次、作用范围、适用条件和原始内容。同样内容复跑不改审核状态、不增加版本；内容变更保留为新的候选来源版本，旧行不会自动删除。范围变化不会静默覆盖已审核资料。

## 4. 可选的模型分区

仅在明确需要对混合原文拆分时使用 `--llm-partition`。默认解析和网站流程均不使用此功能。此模式会产生额外模型用量，需要自备兼容 Chat Completions、支持 JSON 对象输出的接口：

```bash
export OPENAI_API_KEY='your_key'
export LLM_BASE_URL='https://your-endpoint/v1'
export LLM_MODEL='your_model'
python import_care.py --input input.json --output partitioned.json --batch source-001 --llm-partition
```

模型必须逐字提取已有字段，字符串和数值均按原字段校验，拒绝添加/改写事实；不能更改原文明确的阶段或子类型。覆盖校验拒绝遗漏已有知识。仍然不是医学核验，所有结果待人工审核。V4 已按场景输出时直接用默认模式即可。
