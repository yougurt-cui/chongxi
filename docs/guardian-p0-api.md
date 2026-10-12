# 管家 P0 小程序接口文档

版本：`main`，2026-10-11，功能发布提交 `3a5a7c7`。依据当前 Flask 路由、服务序列化和小程序调用整理；对应 [后端 PR #2（已合并并部署）](https://github.com/yougurt-cui/chongxi/pull/2) 和 [小程序草稿 PR #1（待处理、未发布）](https://github.com/yougurt-cui/xiaochengxu/pull/1)。业务变化见 [按功能审查说明](guardian-p0-review.md)。

本文覆盖本次 P0 新增与调整的接口，不是整个小程序的全部接口清单。JSON 示例中的名称、ID、评分和时间为结构示例，不代表真实推荐产品。

当前 `fix/guardian-optional-age` 分支将宠物年龄改为选填，未填写也可返回候选；此调整尚未发布。候选继续使用现有 `cautions` 展示适用阶段和未按年龄筛选的说明，HTTP 结构不变。选粮与换粮衔接核对见 [流程融合说明](guardian-food-flow-integration.md)。

## 1. 通用约定

- Base URL 使用环境配置 `apiBaseUrl`；所有路径前缀为 `/api/miniprogram`。
- 本文全部接口需要 `Authorization: Bearer <token>`。token 来自既有 `POST /api/miniprogram/auth/wechat-login`，不放到 URL 中。
- JSON 请求用 `Content-Type: application/json`；上传用 `multipart/form-data`。
- 账号以 Bearer token 为准；请求体的 `user_id` 不能指定其他账号。宠物、会话、投稿及历史记录都校验归属。
- 成功返回 `ok: true`，失败返回 `{"ok": false, "error": "说明"}`。须同时检查 HTTP 状态与 `ok`。
- 日期字段 `*_on` 是上海时区的 `YYYY-MM-DD`，未知返回空字符串。`observed_on` 是系统记录日期，不能当成实际开始吃粮日期。
- 投稿/聊天中的 `created_at`、`updated_at` 沿用 UTC 的 `YYYY-MM-DD HH:mm:ss` 字符串，无时区后缀；显示时须按 UTC 转换，不用它们推测吃粮区间。

| HTTP 状态 | 含义 | 前端处理 |
| --- | --- | --- |
| 200 | 查询、聊天、档案修改或删除成功 | 检查 `ok` 后处理结果 |
| 201 | 创建历史记录或会话成功 | 使用返回 ID；历史重新查询列表 |
| 202 | 投稿已接收 | 继续查询识别状态；不表示识别成功 |
| 400 | 请求结构、字段或业务规则不符合要求 | 展示 `error`，供用户修正 |
| 401 | 未登录或 token 无效 | 清理过期登录态，引导登录 |
| 404 | 对象不存在或不属于当前账号/宠物 | 不继续操作旧对象 |
| 409 | 投稿不能撤销 | 展示原因 |
| 500 | 服务暂不可用 | 展示错误、保留输入，允许稍后重试 |

本次未增加稳定的机器错误码；前端不要依赖中文 `error` 的固定文案进行分支判断。

## 2. 本次接口变更清单

| 方法与路径（省略公共前缀） | 变更 | 用途 |
| --- | --- | --- |
| `GET /cat-profiles/{pet_id}/diet-history` | 新增 | 读取当前粮、历史粮及同期咨询摘要 |
| `POST /cat-profiles/{pet_id}/diet-history` | 新增 | 补录已确认的历史吃粮 |
| `DELETE /cat-profiles/{pet_id}/diet-history/{record_id}` | 新增 | 删除错误的历史记录 |
| `POST /food-submissions` | 既有接口扩展 | 上传包装照片；返回新的识别反馈字段 |
| `GET /food-submissions`、`GET /food-submissions/{id}` | 既有接口扩展 | 新识别状态与操作权限 |
| `POST /chat/conversations/{id}/messages` | 既有接口扩展 | 拦截无配料分析；返回候选、推荐状态和操作入口 |
| `PATCH /cat-profiles/{pet_id}`（也支持 PUT） | 既有接口行为调整 | 用户确认当前粮后，原粮同步归档 |

推荐没有新增单独 HTTP 接口，仍通过聊天消息返回。确认当前粮没有新增独立接口，仍通过档案 PATCH 保存。

## 3. 查询当前粮与历史

`GET /api/miniprogram/cat-profiles/{pet_id}/diet-history?limit=50`

`pet_id` 是宠物档案 ID，非微信 open_id。`limit` 默认 50，按 1～100 限制；非整数返回 400。当前粮排在前，其他记录按结束/开始/记录时间倒序。列表包含当前粮，历史页自行筛选 `is_current: false`。

返回 200：

```json
{
  "ok": true,
  "items": [
    {
      "id": "11111111111111111111111111111111",
      "brand": "示例品牌",
      "product": "示例成猫粮",
      "product_id": null,
      "formula_id": null,
      "is_current": false,
      "source": "user_history",
      "note": "用户填写的吃粮表现",
      "started_on": "2026-09-01",
      "ended_on": "2026-09-30",
      "observed_on": "2026-10-11",
      "health_topics": [
        {
          "code": "digestive",
          "label": "排便与肠胃",
          "conversation_count": 2,
          "samples": [
            {
              "conversation_id": "22222222222222222222222222222222",
              "message_id": "33333333333333333333333333333333",
              "excerpt": "最近有点软便",
              "date": "2026-09-10",
              "label": "排便与肠胃"
            }
          ]
        }
      ],
      "association_available": true,
      "association_truncated": false
    }
  ],
  "has_more": false,
  "association_note": "仅表示吃粮记录期间咨询过的话题，不代表粮食导致健康问题；未记录的日期不推断。"
}
```

记录字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 历史记录 ID，不能用产品 ID 替代 |
| `brand`、`product` | string | 当时记录的品牌和产品名 |
| `product_id`、`formula_id` | integer/null | 已有关联时返回；手动补录本次不保存这两个 ID |
| `is_current` | boolean | 是否当前粮；当前粮不能用历史 DELETE 删除 |
| `source` | string | `profile_confirmed`：档案记录；`profile_snapshot`：旧档案归档；`user_history`：用户补录/确认导入 |
| `started_on`、`ended_on` | string | 用户已知的吃粮区间；未知为 `""`，结束日期包含当天 |
| `observed_on` | string | 服务端首次记录该条记录的日期 |
| `note` | string | 用户备注 |
| `health_topics` | array | 期间咨询的话题；最多每话题 3 个摘录、每摘录 160 字 |
| `association_available` | boolean | 日期范围可用且关联资料可查询；false 不代表没有健康问题 |
| `association_truncated` | boolean | 单条记录区间消息超过 1000 条，只统计最近 1000 条 |

话题代码为 `digestive`（排便与肠胃）、`skin`（皮肤与毛发）、`weight`（体重与食欲）、`urinary`（泌尿与饮水）、`allergy`（过敏与耐受）。按关键词匹配，同一话题在同一会话多次出现只算 1 次；不是疾病诊断。

手动历史需同时有开始、结束日期才关联。`profile_confirmed` 记录可从服务端首次观察起关联，当前记录允许开放结束时间；旧档案快照不推测开始日期。只读取同账号、同宠物的有效用户消息，已删除会话不纳入。

`has_more: true` 只表示还有记录；本次没有分页 cursor/offset 参数，客户端最多请求 100 条，不能声称已经展示全部。首次查询已有口粮的旧档案时，服务端会建立当前粮观察记录，不补写过去的吃粮日期。

## 4. 补录历史吃粮

`POST /api/miniprogram/cat-profiles/{pet_id}/diet-history`

```json
{
  "brand": "示例品牌",
  "product": "示例产品",
  "started_on": "2026-09-01",
  "ended_on": "2026-09-30",
  "note": "用户确认的记录",
  "legacy_record_id": "device-record-123"
}
```

| 请求字段 | 必填 | 校验与含义 |
| --- | --- | --- |
| `brand` | 是 | trim 后非空，最多 128 字 |
| `product` | 是 | trim 后非空，最多 512 字 |
| `started_on` | 否 | `YYYY-MM-DD`；省略/null/空字符串为未知，不能晚于今天 |
| `ended_on` | 否 | 同上；两日期齐全时不能早于开始日期 |
| `note` | 否 | 最多 1000 字 |
| `legacy_record_id` | 否 | 本机旧记录原始 ID，最多 128 字；仅用于确认导入的幂等性 |

返回 201：

```json
{"ok": true, "id": "11111111111111111111111111111111"}
```

这是已确认的历史，`is_current` 永远为 false，**不会修改当前口粮**。保存后重新 GET 获取完整记录和咨询关联。

提供 `legacy_record_id` 时，按账号、宠物和旧 ID 生成稳定记录 ID；重复提交仍返回相同 ID，保留第一次保存的内容，不是更新接口。已删除同 ID 的导入记录也不会因重试自动恢复。普通补录不提供该字段时，每次 POST 都会创建一条记录；超时后先查列表确认结果，避免盲目重试。

未知日期不要用设备记录日期或保存日期填充；一侧日期未知也可以保存，但不会进行手动历史的同期咨询关联。本次无历史记录编辑接口，填错需删除后重新补录。

## 5. 删除历史记录

`DELETE /api/miniprogram/cat-profiles/{pet_id}/diet-history/{record_id}`，无需请求体。

返回 200：`{"ok": true, "id": "11111111111111111111111111111111"}`。

- 只软删除当前账号、当前宠物下的有效历史。
- 当前粮返回 400：`当前口粮请在口粮清单中修改`。
- 不存在、不属于当前宠物或已经删除返回 404。
- 不支持删除后撤销；客户端删除前展示确认。

## 6. 食品上传与识别反馈

### 6.1 上传（既有接口）

`POST /api/miniprogram/food-submissions`，multipart 表单：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `brand_name` | 是 | 用户填品牌名；兼容 `brand`；最多保留 255 字 |
| `product_name` | 是 | 用户填产品名；最多保留 512 字 |
| `remark` | 否 | 备注，最多保留 1000 字 |
| `images` | 是 | 同名文件字段 1～3 张；兼容 `image`；每张 ≤10 MiB，总计 ≤30 MiB |

小程序上传组件接受 JPG、PNG、WEBP；服务端验证能否解码并转存 JPEG。不要上传 URL 或把照片放进 JSON。当前 `utils/food-submission.js` 负责组合多图表单。

返回 202：`{"ok": true, "item": <投稿对象>}`。创建时通常是 processing；接收成功不等于 OCR 成功。

### 6.2 查询

- `GET /api/miniprogram/food-submissions?limit=50` → 200：`{"ok": true, "count": 1, "items": [<投稿对象>]}`。limit 为 1～100，无分页；列表不含图片数组。
- `GET /api/miniprogram/food-submissions/{submission_id}` → 200：`{"ok": true, "item": <投稿对象>}`。详情额外含 `images` 数组。
- `GET /api/miniprogram/food-submissions/{submission_id}/images/{image_id}` → 私有图片二进制，也须 Bearer。微信图片使用已带授权头的下载方法，再展示临时路径。
- `DELETE /api/miniprogram/food-submissions/{submission_id}` → 200：`{"ok": true, "id": "投稿ID", "status": "cancelled"}`。撤销后不能分析或重新上传这条；已审核通过或收录的投稿返回 409。

投稿对象示例（时间字段省略）：

```json
{
  "id": "44444444444444444444444444444444",
  "claimed_brand": "示例品牌",
  "claimed_product_name": "示例产品",
  "remark": "",
  "recognized_brand": "",
  "recognized_product_name": "",
  "ingredient_composition": "",
  "guarantee": {},
  "recognition_status": "unrecognized",
  "recognition_message": "这次没有读到完整配料表。请拍包装背面的配料表近照，让文字完整、清晰、不反光；可另拍一张营养成分保证值。",
  "can_analyze": false,
  "can_reupload": true,
  "review_status": "pending",
  "publish_status": "unpublished",
  "catalog_key": "",
  "review_note": "",
  "reject_reason": "",
  "status": "active",
  "status_label": "未识别到配料表",
  "images": [
    {"id": "image-id", "image_type": "other", "url": "/api/miniprogram/food-submissions/44444444444444444444444444444444/images/image-id"}
  ]
}
```

### 6.3 新识别状态与字段

| `recognition_status` | 含义 | 分析 | 重新上传 |
| --- | --- | --- | --- |
| `pending` | 兼容旧排队记录 | 否 | 否 |
| `processing` | 识别中 | 否 | 否 |
| `unrecognized` | 未读到可用配料表 | 否 | 是 |
| `needs_review` | 识别内容需核对 | 否 | 是 |
| `failed` | 识别处理失败 | 否 | 是 |
| `success` | 有可用配料 | 是 | 否 |

上表权限针对有效投稿；`status: cancelled` 时两种操作都为 false。新字段 `recognition_message: string`、`can_analyze: boolean`、`can_reupload: boolean` 是前端提示与按钮的依据。兼容旧服务时，小程序仍检查配料非空。

`recognition_status` 与 `review_status`、`publish_status` 独立：识别成功不代表产品已经审核收录；OCR 成功后其他处理失败也不应显示“配料识别失败”。不能用来源 ID、审核标签或收录状态判断可否分析。

重新上传会重新 POST 新投稿，生成新 ID，保留用户填的品牌和名称，不覆盖旧投稿。投稿页处理 pending/processing 时约每 5 秒查询，隐藏/卸载页面停止查询。聊天中的 processing 回复只提示稍后刷新并重新提交分析，没有自动补发分析结果的后台推送。

## 7. 管家聊天与候选（既有接口扩展）

### 7.1 创建绑定宠物的会话

`POST /api/miniprogram/chat/conversations`

```json
{"pet_id": "55555555555555555555555555555555", "scene": "pet_manager"}
```

返回 201：

```json
{
  "ok": true,
  "conversation": {"id": "22222222222222222222222222222222", "pet_id": "55555555555555555555555555555555", "title": "猫咪的宠物管家", "created_at": "2026-10-11 01:00:00"},
  "welcome": {"response_type": "welcome", "reply": "嗨，我是猫咪的宠物管家，今天想聊点什么？"}
}
```

省略 `pet_id` 时服务端选择当前账号的一个有效档案；小程序应明确传当前选中的宠物。无档案返回 400。换宠物需创建/选择对应宠物会话。

### 7.2 发送消息

`POST /api/miniprogram/chat/conversations/{conversation_id}/messages`

普通消息：`{"message": "想换主粮，希望改善软便，预算每斤 40 元"}`。

已有交互选项：`{"interaction": {"slot": "target_requirement", "value": "改善软便"}}`。slot 和 value 使用上一条服务端返回的选项，不自行构造流程字段。

投稿分析：

```json
{"attachments": [{"type": "food_submission", "submission_id": "44444444444444444444444444444444"}]}
```

三类至少提供一项。投稿从服务端读取，不让客户端自行提交“识别成功”或配料结果。可沿用既有 `client_context.recent_daily_records`，本次历史口粮从服务端读取，无需客户端上送。

返回 200 的消息外层结构：

```json
{
  "ok": true,
  "message": {
    "id": "33333333333333333333333333333333",
    "conversation_id": "22222222222222222222222222222222",
    "role": "assistant",
    "content": "服务端生成的回复",
    "response_type": "result_card",
    "interaction": null,
    "attachments": null,
    "result": {
      "intent": "food_switch",
      "limited_info": false,
      "food_candidates": [],
      "recommendation": {"status": "data_unavailable", "items": [], "note": "推荐资料暂不可用，可以先对照配料表选粮。"},
      "actions": [{"type": "diet_history", "label": "查看历史口粮", "pet_id": "55555555555555555555555555555555"}],
      "turn_no": 3,
      "sequence_no": 1,
      "model_name": null
    },
    "created_at": "2026-10-11 01:05:00",
    "disclaimer": "AI内容仅供参考，不能替代兽医诊断"
  },
  "messages": [
    {
      "id": "33333333333333333333333333333333",
      "conversation_id": "22222222222222222222222222222222",
      "role": "assistant",
      "content": "服务端生成的回复",
      "response_type": "result_card",
      "interaction": null,
      "attachments": null,
      "result": {
        "intent": "food_switch",
        "limited_info": false,
        "food_candidates": [],
        "recommendation": {"status": "data_unavailable", "items": [], "note": "推荐资料暂不可用，可以先对照配料表选粮。"},
        "actions": [{"type": "diet_history", "label": "查看历史口粮", "pet_id": "55555555555555555555555555555555"}],
        "turn_no": 3,
        "sequence_no": 1,
        "model_name": null
      },
      "created_at": "2026-10-11 01:05:00",
      "disclaimer": "AI内容仅供参考，不能替代兽医诊断"
    }
  ]
}
```

`messages` 为消息对象数组。客户端优先顺序渲染 `messages`，不要只渲染 `message`；`message` 是最后一条，兼容旧客户端，软便回复可能含多条。字段 `content` 是最终接口的文本，内部策略的 `reply` 不是这个接口字段。交互响应时 `interaction` 含 `slot`、`options`，不同流程的 `result` 字段可能不同/缺省。

### 7.3 推荐状态和候选结构

选粮流程信息足够时返回 `result.recommendation` 和 `result.food_candidates`。`recommendation.items` 与该次候选一致；展示候选卡需 `recommendation.status === "ready"`，不能只看 `food_candidates` 非空，因为其他流程也可能返回查询候选。

| `recommendation.status` | 含义 |
| --- | --- |
| `ready` | 有完整资料支持的候选 |
| `goal_needed` | 改善方向未匹配现有推荐目标 |
| `profile_missing` | 该目标无可用评分画像 |
| `no_eligible_food` | 无符合年龄、配料、偏好等条件的候选 |
| `data_unavailable` | 推荐资料、指标或服务不可用 |
| `unsupported_species` | 当前猫粮库不支持这类宠物 |
| `medical_diet_required` | 档案有疾病背景，需要结合医生饮食要求 |

非 ready 的 `items` 为空，客户端展示说明或继续补充；不把空列表解释为市面上没有合适粮款。没有触发选粮时，`recommendation` 可为 null/缺省。

候选对象示例：

```json
{
  "catalog_key": "formula:123",
  "product_id": 456,
  "formula_id": 123,
  "brand": "示例品牌",
  "product_name": "示例成猫粮",
  "label": "示例品牌 示例成猫粮",
  "life_stage": "adult",
  "price": 30.0,
  "price_source": "brand_reference_range",
  "price_min_per_jin": 25.0,
  "price_max_per_jin": 35.0,
  "fit_score": 80.0,
  "profile_code": "示例画像代码",
  "profile_name": "示例画像名称",
  "strengths": ["引擎生成的参考理由"],
  "cautions": ["引擎生成的需留意项"]
}
```

`catalog_key`、`brand`、`product_name`、`label`、`life_stage`、`price_source`、画像字段为字符串；ID 为整数；价格、分数为数字，价格可为 null；理由为字符串数组。`fit_score` 是引擎参考分，不是疗效概率。价格是品牌每斤参考区间，`price` 为参考均值，预算按区间上界筛选；不得宣传为实时成交价。

候选最多 3 款。目标目前支持软便、黑下巴、泪痕、减重、增重；年龄选填，有年龄时匹配适用阶段，无年龄时继续推荐并注明未按年龄筛选。产品必须有可用适用阶段、主粮/配料和画像所需指标；排除当前粮。明确每斤预算、品牌、产地及已知过敏会影响筛选。咨询关联不直接作为病因或负面喂养反馈。

### 7.4 操作入口

| `result.actions[].type` | 前端行为 |
| --- | --- |
| `flow_handoff` 且 `target_flow: food_switch` | 在同一会话发送“帮我推荐一款主粮”，复用已有背景 |
| `diet_history` | 打开当前宠物历史口粮页，调用第 3 节 GET |
| `medical_record` | 目前只打开既有档案编辑页，按钮展示“补充健康档案”；不是完整病历接口 |

未知 action 忽略。操作前校验登录账号、当前宠物和会话绑定。

上传 processing 时回复的 `response_type: processing`，`result` 含 `resource: food_submission`、`id`、`interval_ms: 2000`（建议查询间隔，非自动完成通知）。识别不成功时返回说明，不调用配料分析。投稿权限判断仍以第 6 节对象字段为准。

## 8. 确认已经吃某款后更新当前粮

`PATCH /api/miniprogram/cat-profiles/{pet_id}`，也兼容 PUT；部分更新优先使用 PATCH。

候选卡点击并确认“已经开始吃”后：

```json
{"diet": {"product_id": 456, "formula_id": 123}}
```

两个 ID 必须同时提供。服务端校验有效产品和配方关联，使用标准目录名称，不信任客户端自填候选名称。失败返回 400。

手动修改当前粮：`{"diet": {"brand": "示例品牌", "product": "示例产品"}}`。实际修改名称且没有传新配方 ID 时，旧 `product_id`、`formula_id` 清空，避免旧配方附着在新名称上。兼容原有顶层 `food_brand`、`food_product`、`food_product_id`、`food_formula_id`，小程序优先用 `diet`。

返回 200：`{"ok": true, "item": <完整宠物档案>}`，档案包含：

```json
{
  "id": "55555555555555555555555555555555",
  "food_brand": "标准品牌名",
  "food_product": "标准产品名",
  "food_product_id": 456,
  "food_formula_id": 123,
  "diet": {"brand": "标准品牌名", "product": "标准产品名", "product_id": 456, "formula_id": 123}
}
```

上例仅展示口粮相关字段，`item` 还包含既有姓名、年龄、健康等档案字段。前端保存后刷新档案及历史；以后软便对话刷新饮食背景。

服务端同一事务中结束旧当前记录、归档旧粮并建立新当前粮观察记录。没有实际变化则不重复建历史；新记录从服务端观察时间起记录，不推测更早的开始时间。旧档案从未建立历史时，归档旧粮不填未知开始日期。

推荐、上传食品、聊天表达换粮意向都不会调用这个更新。前端需要确认用户已开始吃，取消或账号/宠物变更时不提交；接口自身无法判断用户是否真的吃过。

## 9. 前端联调顺序

1. 已登录并有宠物档案：查询历史，确认当前粮；补录一条带日期的历史，查询咨询话题，再删除。
2. 上传包装图片：202 后查详情；有配料才提交聊天附件；识别失败时补拍，新投稿保留品牌和产品名。
3. 在宠物会话请求选粮：分别检查缺资料降级和完整资料下的候选结构，确认只展示 ready 的候选卡。
4. 点击候选后取消：不发 PATCH；再次确认已开始吃：PATCH 后刷新，旧粮进入历史。
5. 切换账号/宠物：丢弃旧请求结果，确认旧对象不能读取或修改。

后端已先部署，生产历史表结构、服务及公开 HTTPS 鉴权检查通过；配套小程序仍待负责人发布。本次已通过本地 API/auth/MySQL smoke 与离线推荐样例检查；真实产品资料、OCR/LLM、微信开发者工具编译及真机验收仍待完成。

新后端已通过改动前小程序 `2c4813f` 的兼容检查，可以独立先发布；旧版继续使用原接口与返回字段，暂不展示新候选卡、云端历史和补拍入口。固定版本、验证方式和生产预检限制见 [后端独立发布与旧版兼容](guardian-p0.md#后端独立发布与旧版兼容)。

## 10. 实现索引

- 路由及 HTTP 状态：`api/miniprogram_api.py`。
- 历史字段、日期和咨询关联：`services/miniprogram_diet_history_service.py`。
- 识别对象和操作权限：`services/miniprogram_food_submission_service.py`。
- 聊天消息、动作和推荐接入：`services/miniprogram_chat_service.py`。
- 评分筛选与候选字段：`services/guardian_recommendation_service.py`。
- 档案更新和历史事务：`services/miniprogram_cat_profile_service.py`。
