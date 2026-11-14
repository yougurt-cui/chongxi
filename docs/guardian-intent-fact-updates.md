# 对话行为与任务交接

任务判断器在已有会话中同时识别话题关系、表达行为、任务和业务执行意图。
首轮、图片、交互选项、紧急症状等现有专用入口保持各自处理方式。

`dialogue_act` 支持 request、provide_fact、answer_question、correct_fact、confirm、change_topic。
`active_context` 包含当前话题、用户原话、带消息来源的事实、当前问卷步骤和上一条助手回复。
事实字段只接收本轮连续原文，不允许识别器编造配方 ID；查询对象使用独立的 `product_query`，不能因询问产品而记录已吃。

健康话题中“换了卫仕LA20”作为补充事实继续分析。没有正在填写的问卷时，补充事实不能进入选粮推荐；若模型同时输出 business，会回到原话题解释，缺少可引用话题则澄清。
现有选粮问卷中的预算等回答继续原流程。明确要求推荐、产品分析或提醒时，由 `business_intent` 交给对应执行器，旧模型不再二次改判。

`facts_to_merge` 随用户消息的 `discussion` 保存，按话题保存在会话记忆中；纠正现用粮时会清除旧的现用/换后粮字段。该数据仅表示用户在会话中的陈述，不直接更新宠物吃粮档案。
后续解释会保留事实来源消息。粮名使用已有目录及版本解析规则，只读加载配方资料；无法唯一定位时传递候选和缺失状态，不默认选中，也不启动推荐问卷。

回归测试：

```bash
python -m pytest tests/test_guardian_task_recognizer.py tests/test_guardian_fact_routing.py tests/test_guardian_conversation_context.py tests/test_named_food_shorthand.py tests/test_guardian_ingredient_chat_route.py tests/test_soft_stool_consultation.py -q
```
