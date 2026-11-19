# 社区经验主题和搜索词

在后端项目根目录执行以下命令。使用与知识汇总任务相同的 MySQL 配置，模型使用 get_chat_model_config 的配置。首次运行从五张汇总表提取主题并调用模型生成搜索词。后续运行复用 var/experience-topic-cache 下输入内容相同的结果，源内容变更会重新抽取。

    .venv/bin/python scripts/build_experience_topics.py
    .venv/bin/python scripts/build_experience_topics.py --apply

第一条只准备数据和输出统计。第二条在 MODEL_KNOWLEDGE_DATABASE 中发布 experience_topic、experience_topic_source 和 experience_search_term，用同一事务同步知识关联与检索词。任务只在执行命令时更新。失败后可用同一缓存重跑。

主题按物种、品种、阶段和规范化后的场景、行为、结果去重。只有说明性陈述时保留原文区分对象。关联键为来源表名和该表的知识 ID，每条关联保留支持原文。检索词按 position_no 1 到 5 排序，供社区采集任务选择。这些是基于模型知识生成的检索候选，社区实帖的结果应由采集流程另行记录。

后台读取沿用知识管理接口的鉴权。可通过 source_table 和 knowledge_id 回查原知识对应的主题，也可用 limit 和 offset 分页（每页最多 100 个）。

    GET /api/knowledge-second-stage/experience-topics?source_table=knowledge_point&knowledge_id=1

每项含 object_scope、object_name、scenario、behavior、result_text、sources 和 terms。没有明示行为或结果时，相应字段为空。
