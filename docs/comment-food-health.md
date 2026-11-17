# 评论分类与宠物食品病症候选链路

原始脚本备份：`vendor/comment_router/original.py`（用户 Downloads/comment_router.py）。新实现是 `comment_pipeline/llm_router.py`，保留五领域、五处理器及连续原文证据校验；使用项目千问配置，不使用脚本独立密钥。

## 本轮范围

从已有 `csv_labeling.comment_clean_base` 取样，再使用公共 `normalize_text` 去噪。第一阶段不重新启动旧原始评论采集、同步或增长报表链路。现有 legacy 模式保持兼容，新模式显式选择：

```bash
python -m comment_pipeline.run_pipeline --flow food-health --limit 100 --scan-limit 1000 --workers 4
```

也可使用 `python -m comment_pipeline.food_health`。`--limit` 是成功分类为 pet_food 的评论目标数，非扫描总数，也非病症事件数；`--scan-limit` 是关键词召回池上限。按最新评论顺序做食品词召回，仅用于限定测试样本，最终是否属于食品由模型判定；不代表随机样本或分类准确率测试。评论不超过1000字，空白、低信息量、超长块文本和同轮重复正文跳过，记录计数。

`--flow food-health --dry-run` 只展示计划，不调用模型、不连接或写数据库。旧入口继续使用 legacy，正式增长引擎的五个生成接口仍由 Nginx 暂停配置拦截；本轮通过独立命令试跑，不解除暂停。

## 分类与打标

所有分类成功的结果保留 domains、processors、species 以及来源证据。评论正文优先，上下文标题/正文/关键词只用于补足领域和物种；处理器证据仅取清洗后评论的连续片段。物种需有明确猫/犬证据，无法确认或混合物种为 unknown。

只有 `pet_food + health_observation + species=cat` 才调用病症标注。其他领域与狗的结果保存分类，暂不执行猫病症处理。不要求必须有商品反馈或品牌，不能因未提品牌丢掉健康观察。

病症沿用现有一级/二级枚举，保留品牌、产品原文、症状、方向、证据、置信度、时间原文、采取措施原文，新增 observation：

- present：明确发生；方向根据食品与表现的叙述关系选加重/改善/不确定，不能等同因果确证。
- improved：明确曾发生、随后改善。
- absent：从未发生或纯否定，只存标注审计，不写患病候选。
- uncertain：不能确认实际发生，只存标注审计。

纯科普或一般假设不提取事件；“正在软便，怎么办”包含实际观察。模型证据必须连续且忠于原文。二级症状还需在完整评论中有现有词表支持的明确表现，不能仅由“排出毛球”等现象推断便秘；不支持时反馈重试，仍无法通过则保存失败。复用旧标注缓存时也会重新经过这一校验。品牌和产品不得从搜索词猜测，未提品牌留空。未通过校验的响应带具体错误反馈重试一次，仍失败则明确保存 route_failed/tag_failed，后续运行重试；无结果与失败不是同一种状态。

## 存储、审核与来源

- `comment_classification_runs`：每次运行、版本、模型、状态和统计。
- `comment_classification_results`：原始/清洗输入快照、分类及病症原始模型输出、校验后的结果、调用次数和 token usage、候选 ID。唯一键 comment_id + input_hash；指纹覆盖清洗正文、上下文、模型、版本和两套提示词。成功结果可复用，失败结果不作为完成缓存。
- `cat_disease_clue_candidates`：仅实际发生或改善的事件，新增时全部 PENDING。保留原有唯一键，重复候选只读取已有 ID，不更新正文、证据、审核状态或人工结果。

候选增加 source_table、source_comment_id、source_router_result_id 可空字段。新记录的 source_table 为 comment_clean_base，source_comment_id 指向该表，source_router_result_id 指向新分类结果；旧字段 source_candidate_id=0 表示未经过旧 Choice 表，不能拿它去关联 Choice 行。重复旧候选的关联记录在分类结果的 candidate_ids，不改旧候选来源。

候选的 review_text 为清洗后正文，原文存于输入快照。时间和措施保留在标注 JSON 中，不把采集时间冒充症状发生日期。新管线不执行审批，也不写 protein_feature_platform.cat_disease_clues，不刷新增长报表。

每次运行持有独立数据库互斥锁；网络调用在有限线程池进行，主线程逐条事务保存候选及分类关联。产物默认 `var/comment-food-health/<run_id>/results.jsonl` 和 `summary.json`。中断保留已完成记录，下次按输入指纹复用；错误不得被当成未命中。

## 验证

```bash
python -m pytest tests/test_comment_food_health.py tests/test_comment_pipeline.py tests/test_comment_clean_sync_service.py tests/test_comment_mining_api.py -q
```

100条实跑仅验证链路及抽样质量。需另建人工标注的食品/用品/医疗/智能设备/日常照护混合样本，才能评估路由精确率和召回率。
