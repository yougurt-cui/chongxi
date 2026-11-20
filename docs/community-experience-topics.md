# 社区经验主题和搜索词

在后端项目根目录执行以下命令。使用与知识汇总任务相同的 MySQL 配置，模型使用 get_chat_model_config 的配置。首次运行从五张汇总表提取主题并调用模型生成搜索词。后续运行复用 var/experience-topic-cache 下输入内容相同的结果，源内容变更会重新抽取。

    .venv/bin/python scripts/build_experience_topics.py
    .venv/bin/python scripts/build_experience_topics.py --apply

第一条只准备数据和输出统计。第二条在 MODEL_KNOWLEDGE_DATABASE 中发布 experience_topic、experience_topic_source 和 experience_search_term，用同一事务同步知识关联与检索词。任务只在执行命令时更新。失败后可用同一缓存重跑。

主题按物种、品种、阶段和规范化后的场景、行为、结果去重。只有说明性陈述时保留原文区分对象。关联键为来源表名和该表的知识 ID，每条关联保留支持原文。检索词按 position_no 1 到 5 排序，供社区采集任务选择。这些是基于模型知识生成的检索候选，社区实帖的结果应由采集流程另行记录。

后台读取沿用知识管理接口的鉴权。可通过 source_table 和 knowledge_id 回查原知识对应的主题，也可用 limit 和 offset 分页（每页最多 100 个）。

    GET /api/knowledge-second-stage/experience-topics?source_table=knowledge_point&knowledge_id=1

每项含 object_scope、object_name、scenario、behavior、result_text、sources 和 terms。没有明示行为或结果时，相应字段为空。

搜索词新增 `category`（种类：猫、狗、未分类）和 `term_type`（类型：症状与表现、原因与疾病、行为与习惯、做法与护理、产品与配方、结果与变化、待确认）。种类从主题的结构化物种取值；已有词和新词的类型默认“待确认”，不将猜测当作已核实分类。原 `terms` 字符串数组保持兼容，接口另返回 `term_details`，包含序号、词、种类和类型。

升级已有数据库：先运行 `.venv/bin/python scripts/migrate_experience_term_classification.py` 查看物种数量；确认后加 `--apply` 创建缺失字段并根据主题回填种类。该脚本不会分类搜索意图；MySQL 的结构变更会隐式提交，执行前应备份。重新构建主题时，相同主题及搜索词的类型会保留，新词默认待确认。

管理员可调用 `PUT /api/knowledge-second-stage/experience-topics/<topic_key>/terms/<position_no>/type`，JSON 包含 `search_term`（原词，用于防止覆盖已变化的词）与 `term_type`（上述类型之一）。更改与重建共用锁。
