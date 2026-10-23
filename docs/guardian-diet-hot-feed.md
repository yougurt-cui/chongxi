# 需求30、31：饮食建议与社区点击排序

## 30：饮食建议入口

沿用 `POST /api/miniprogram/chat/conversations/<id>/messages`，小程序仅在点击“饮食建议”时增加：

```json
{"message":"想了解适合我家宠物的日常饮食建议。","client_context":{"entry_point":"diet_advice"}}
```

宠物身份仍取已认证用户拥有的会话；客户端不传粮食清单。后端读取当前粮和最近10条真实吃粮记录，使用 `saved_diet` 分析范围：

- 当前粮或历史粮存在：按配方去重，保留 current/history 来源，复用营养健康解读；多粮先整体总结，再展示各粮内容，不强制比较。
- 仅历史记录存在：不虚构当前粮。只有投稿不算吃过。
- 已有记录缺配方/画像：保留产品名称并说明资料不足。
- 确认没有记录：继续原有饮食建议流程。
- 历史读取失败：没有当前粮则提示重试；有当前粮则只分析当前粮并披露历史未读取。还有更早记录时展示覆盖范围提示。

吃粮事实已经确定，因此包括单粮在内均直接询问“是否需要根据上述内容推荐新粮？”，不重复问“是否已经在吃”。服务端快照保存 `confirm_slot`，沿用原有带 analysis_id 的 yes/no 交互，确认后才进入现有推荐；旧快照与其他分析入口保持兼容。

## 31：全部社区帖子按点击量排序

`GET /api/miniprogram/moments?sort=popular&limit=100`：后台先对全部符合可见条件的帖子按 `view_count DESC, COALESCE(source_published_at, created_at) DESC, id DESC` 排序，再 LIMIT。未传 sort 仍是 latest；指定分类或 include_private 时仍按创建时间排序。既有可见性规则不变。

帖子对象新增 `view_count`（既有帖子初始化为0），不要求页面显示阅读数字。

`POST /api/miniprogram/moments/<id>/view`：详情成功加载后，小程序每次页面打开最多上报一次。登录用户身份取 Bearer token；游客传安装级随机32位十六进制 `visitor_id`，它只用于统计，不授予访问权限。二者分开计数，游客登录后可能各计一次。

后端按帖子＋身份摘要，距上一次有效点击满30分钟才再次计数；重复访问不延长窗口。详情可见性检查、去重时间写入和原子增量在同一事务中完成，私密帖子不累计公开热度。GET详情、点赞、评论和重试加载不会增加点击，也不修改帖子更新时间。

小程序“全部”请求 popular，分类请求 latest 和 category_code，保持接口顺序；忽略过期请求，防止快速切分类覆盖列表。详情统计失败不影响阅读、不触发登录。返回首页时重新加载排序。

## 发布与验证

后端先发布，小程序后发布。新增接口和字段向后兼容；旧小程序仍走原入口与时间排序，新小程序上线后启用新行为。表字段、索引和去重表自动增量初始化，兼容多 worker 同时升级。历史点击无法回填。

```sh
.venv/bin/python -m unittest discover -s tests
```

新增 `test_guardian_diet_advice.py` 与 `test_moment_popularity.py`。另做本地 MySQL/Flask 联调，覆盖五种吃粮情况、旧按钮失效、最近100条以外的热门帖、同热度稳定排序、滚动去重、并发相同/不同访客及私密帖子权限。小程序行为验证见对应仓库 `scripts/check-diet-hot-feed.cjs`。
