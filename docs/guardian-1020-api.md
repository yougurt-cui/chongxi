# 测试 case-1020：后端接口与小程序交接

本次实现需求 27、29 的后端部分，以及 case 2 的推荐精简与候选差异化。配套小程序同步完成接口所需的字段映射、卡片展示、次数录入/保存与历史配方选择，不需要负责人再次接线。以下字段均在接口中实际返回；旧字段继续提供，新增字段可逐项接入。

## 27：整体总结、逐款分析及后续确认

仍使用 `POST /api/miniprogram/chat/conversations/{conversation_id}/messages`，请求和鉴权方式不变。例如：

```json
{"message":"分析一下现在吃的和历史吃过的粮"}
```

当前粮与历史粮同时出现即可触发联合分析，不要求说“对比”。明确点名多款时支持用“和/与/、”分隔，最多 10 款。同一配方去重，保留它的不同角色；无画像或未关联配方的记录仍展示资料不足。档案历史仍按最近 10 条有界读取，有更多记录时返回 `coverage_note`。

历史口粮选择产品后，既有 `POST /api/miniprogram/cat-profiles/{pet_id}/diet-history` 同时传入正整数 `product_id`、`formula_id`。后端验证两者属于同一在用配方，品牌/产品名以目录为准，保存后用于本次分析；只传手工品牌名称仍兼容，但不猜配方 ID。已有同名未关联历史可补齐标识，已有不同配方不合并。这个改动供 28 的选择产品入口共用，无需另做页面。

返回 `message.result.health_analysis`（现有 version=1 的增量字段）：

| 字段 | 类型 | 小程序使用方式 |
| --- | --- | --- |
| `is_multiple` | boolean | 按实际去重后的产品数区分单品/多品 |
| `overview` | object / null | 多品时在逐款卡片之前展示；单品为 null |
| `overview.title/text` | string | 整体标题、完整可读总结 |
| `overview.advantages/attentions` | array | 整体“好在哪里/哪里需要留心”，各最多 2 条 |
| `overview.*[].id/title/text` | string | 本次总结事实 ID、标题、依据文案 |
| `overview.*[].subject_indices` | integer[] | 对应 `subjects` 数组的下标，0 起算 |
| `overview.unavailable_subjects` | string[] | 资料不足的产品名，不把它们算成“没有风险” |
| `overview.mode/model_name/note` | string / null | 归纳来源和说明，`evidence_fallback` 为模型不可用/输出不合规时的事实兜底 |
| `subjects` | array | 每款产品的分析，数组只包含真实产品，整体总结不塞进此数组 |
| `subjects[].label/formula_id/roles` | string / integer或null / string[] | 名称、配方标识、角色；角色为 current/history/named/selected，可有多个 |
| `subjects[].presentation.advantages/attentions` | array | 复用 26 的单品结构；每条含 title/summary/observation/source_title，缺资料时返回空数组 |
| `subjects[].status/message` | string | ready/partial/no_match/unavailable；无解读时显示 message |
| `subjects[].missing_dimensions/unmapped_dimensions/skipped_dimensions` | string[] | 区分缺资料、缺映射、中间档跳过，不能视为结论正常 |
| `comparisons` | array | 保留现有逐对比较及来源，额外有 subject_indices |
| `coverage_note` | string | 空字符串或本次历史记录范围说明 |
| `prompt` | string | 直接展示后端问题，前端不要固定写死单品问题 |

结构示例（名称及 ID 仅用于说明格式）：

```json
{
  "is_multiple": true,
  "overview": {
    "title": "猫咪的整体饮食解读",
    "advantages": [{"id":"f0","kind":"advantage","title":"肌肉维持","text":"粮A、粮B均：……","subject_indices":[0,1]}],
    "attentions": [],
    "unavailable_subjects": [],
    "mode": "model_evidence_selection",
    "model_name": "deepseek-flash",
    "note": "配方特点不代表实际健康效果",
    "text": "完整的整体解读文字"
  },
  "subjects": [{"label":"粮A","formula_id":101,"roles":["current"],"presentation":{"advantages":[],"attentions":[],"note":""}}],
  "comparisons": [],
  "coverage_note": "",
  "prompt": "是否需要根据上述内容推荐新粮？"
}
```

示例省略第二款和逐款完整证据。实际多品响应至少有两款去重后的 subjects。

按钮继续使用已有的 `message.interaction`，不需要新按钮组件。**原样回传 slot、value；有 analysis_id 时一并回传，不能由前端自行拼 yes/no 或替换问题。**

| 情况 | slot | options 的 value | 后续 |
| --- | --- | --- | --- |
| 单品 | `food_analysis_usage` | `current:{analysis_id}` / `considering:{analysis_id}` | 记录本次会话的在吃/考虑状态，不自动修改当前口粮档案 |
| 多品 | `food_analysis_recommendation` | `yes:{analysis_id}` / `no:{analysis_id}` | yes 进入原选粮追问/召回；no 保留分析供继续追问 |

```json
{"interaction":{"slot":"food_analysis_recommendation","value":"yes:本次分析ID","analysis_id":"本次分析ID"}}
```

旧客户端只传 slot/value 也可用。旧消息、新分析之后的按钮、跨账号/宠物/会话、重复确认均拒绝。单品状态按钮不能拿来确认多品推荐。完整 `message.content` 仍按“整体—逐款—比较—后续问题”返回，供文字客户端与历史记录使用。

**配套小程序已接入：**在 27 卡片顶部显示 `overview`；逐款复用 26 的 `presentation`，并按后端 `prompt/interaction` 展示结尾。旧接口没有 presentation 时仍使用 concerns；上线前后端新旧版本组合均有回退。

模型只选择已经由画像、映射和比较产生的事实 ID；类别、数量、ID 均在服务端校验，显示内容从原事实取回，模型不能自行添加配方、疾病、健康效果或时间趋势。没有可靠日期就不生成“历史到当前持续改善”的结论。

## 29：按日期记录次数与状态解读

次数与量值分开。字段名固定如下：

| 字段 | 类型 / 单位 | 约束 |
| --- | --- | --- |
| `day` | string，YYYY-MM-DD | 上海时区的自然日，不能晚于今天 |
| `food_count` | integer或null，次 | 0～999；进食次数，不是克数 |
| `water_count` | integer或null，次 | 0～999；饮水次数，不是毫升 |
| `stool_count` | integer或null，次 | 0～999；排便次数 |
| `food_g` | number或null，g | 可选，0～10000；只是录入边界，不是健康参考范围 |
| `water_ml` | number或null，ml | 可选，0～10000；不是正常饮水量区间 |
| `stool_shape` | string | 可选，100 字以内 |
| `complete_day` | boolean | 是否覆盖完整自然日；只有已结束的日期可为 true，默认 false |
| `source` | string | manual（默认）或 reported_device（用户填报设备统计，不代表设备已接入） |

省略某字段表示保留已存值；null 表示清除该值；0 表示实际记录为 0。三者不能混用。重复提交采用**当天绝对值覆盖**，不累计。每个指标保存 metric_sources；更改数据时 complete_day 需要重新明确提交，避免把未完整采集数据误当完整日。

### 保存与读取

需要现有 Bearer 登录态，宠物必须属于当前账号。忽略客户端 user_id；请求路径的日期为准。

```http
PUT /api/miniprogram/cat-profiles/{pet_id}/daily-records/2026-10-19
Content-Type: application/json
Authorization: Bearer <token>

{"food_count":3,"water_count":5,"stool_count":1,"complete_day":true,"source":"manual"}
```

返回 `{"ok":true,"record":{...}}`，record 包含 day、已保存字段、metric_sources、source、complete_day、updated_at（UTC ISO 时间）。

```http
GET /api/miniprogram/cat-profiles/{pet_id}/daily-records
```

返回 `{"ok":true,"items":[...]}`，按日期倒序，最近 14 个自然日，每天最多一条。401 未登录，404 宠物不存在或不属于此账号，400 输入不合法，500 保存/读取失败。不能假定请求失败时数据已保存。

手工和设备填报不会相加；同指标最新明确值覆盖，其他指标保留，并分别保留来源。没有原生设备绑定、认证、数据推送接入，也不接受客户端伪造“官方设备已校验”标记。

### 聊天与前端引导字段

原聊天入口发送“状态解读”或现有卡片原句即可。无需前端新增按钮也能在聊天中填写，例如：

```json
{"message":"昨天全天进食3次，饮水5次，排便1次"}
```

只有进入状态解读后，带明确同一日期的实际次数陈述才保存。举例、假设、将来计划、常规“每天”、多个日期混写等不入库。示例数字不能由前端自动提交，必须替换为真实数据。今天的记录按截至目前统计。

返回 `message.result.daily_status`：

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `version` | integer | 当前 1 |
| `status` | string | needs_records / partial / recorded；还可能为 pet_needed / data_unavailable，不表示健康正常或异常 |
| `pet_profile` | object | name、breed、sex、neutered、age_months、weight_kg 等已有档案 |
| `latest` | object | 最近的有日期记录；无记录为 {} |
| `missing_fields` | string[] | 最近记录尚缺的三类次数字段 |
| `input_fields` | array | 三项次数的 key/label/unit/period/type/minimum/maximum/nullable/required，前端可直接驱动输入表单 |
| `trends` | array | 同指标、同来源、完整日记录的变化；不足时空数组 |
| `trends[].metric/label/latest/baseline_mean/baseline_days/direction` | 多种 | 最新值、此前最多 7 个完整记录日均值及日期；direction=up/down/same，仅描述次数变化 |
| `saved` | boolean | 本次聊天是否成功保存了用户提供的实际次数 |
| `stale_record_days` | string[] | 超过最近14天的快照日期，不作为当前状态依据 |
| `text` | string | 完整状态说明，已有聊天文字组件即可显示 |

至少有两个此前同来源完整日、最新日期也完整且对应值不缺失，才给对照。半天与整天不比较；缺失不当 0。不同宠物/账号不混合。已有 `client_context.recent_daily_records` 继续接受，新增 food_count/water_count 可使用，但仅作临时快照，不自动写入云端、不认定完整日。相同日期优先云端确认记录，避免两份数据相加。

**配套小程序已接入：**29 在原聊天里展示日常次数录入入口，表单由 input_fields 驱动；支持最近14天的日期、三类次数、完整日标记和手填设备统计来源。读取失败禁止覆盖保存；保存 PUT 成功后立即重新获取状态解读。旧聊天自然语言录入仍可用。快照传参和首页增量记录已修正空值转0的问题，不把未填写值自动补0。

**产品口径调整：**当前没有品种/性别对应的可信次数参考表，不能生成正常区间或仅凭次数下疾病结论。品种、性别作为真实个体背景；先做个体记录与变化说明。后续若提供参考表，另行带来源、适用范围和版本接入。

## Case 2：推荐数据、精简展示与“换一组”

沿用已有候选卡和目标选择流程，接入 recommendation_card，补“查看详情”和“换一组”操作，不新增页面。

| 新增/调整字段 | 用途 |
| --- | --- |
| `result.recommendation.selection_summary` | 包含实际身体场景和目标，例如“无明显问题＋减重”；不是只展示身体状态 |
| `result.recommendation.display_intro` | 推荐顶部的简短说明和操作提示；已有 display_blocks 会使用它，避免正文和候选卡重复展示每款详情 |
| `result.recommendation.selection_audit` | 筛选过程计数、目标、拒绝原因和返回数量，用于定位候选重复，不作为用户健康结论 |
| `result.recommendation.ranking_policy` | quality-diversity-v1；现有 100 分制中最多 5 分差的同档内优先不同品牌，阈值是业务排序策略，非医学阈值 |
| `food_candidates[].strengths/cautions` | 兼容旧卡片，每项最多 2 条；年龄缺失说明优先保留，减重明确能量未核验 |
| `food_candidates[].recommendation_card` | version=1、reasons、cautions、goal_label、life_stage、life_stage_label、price_range_per_jin；方便后续卡片接入 |
| `food_candidates[].recommendation_detail` | 保留完整 strengths/cautions；analysis 继续保留营养、比较和风险证据 |

推荐不再先请求模型写长段，再追加完整营养/风险清单。旧文字 content 保留简短候选信息；支持 display_blocks 的现有小程序显示简介加原候选卡。选中某款仍使用原详情流程，能读取完整证据，选择本身不修改当前口粮。

“换一组/换一批”可直接发送普通聊天消息，不要求新按钮。每个会话保存当前条件下已展示的配方 ID；下一组从同样通过筛选的剩余候选中选择。已展示完返回 `alternatives_exhausted`，不降低条件、不编造候选。目标、状态、偏好、宠物档案或历史记录改变会重置该轮排除。相同条件普通重复询问不随机换序。

没有原粮时只比较候选自身条件，不认定改善幅度。五维评分不是热量或减重效果；缺乏能量资料时保留“未核验能量密度”。不为了不同品牌牺牲明显更高的匹配质量，同品牌也可以在没有同档替代时继续展示。

## 验证与发布

新增回归测试见 `tests/test_guardian_1020_backend.py`，本地真实鉴权/API/MySQL 验证使用 `scripts/smoke_guardian_1020_local.py`，强制只允许 127.0.0.1:13306 的隔离数据库并清理隔离测试账号。模型事实选择另做真实调用验证；普通回归和 API 冒烟不需要付费模型。

已有小程序协议检查可在上述冒烟生成响应后运行：

```bash
node scripts/check_guardian_1020_client.cjs ../xiaochengxu origin/dev < ../local-dev/1020-api-results.json
node scripts/check_guardian_1020_client.cjs ../xiaochengxu 4d74d3f < ../local-dev/1020-api-results.json
```

检查使用指定版本未修改的消息映射及按钮回传代码，覆盖文字、逐款数据、推荐卡、次数回复和分析标识；不代表微信真机样式或新卡片已验收。

后端与小程序配套改动均已实现，发布前仍需各自合并/部署和微信发布。上线后首次访问日常记录接口会创建 `miniprogram_pet_daily_record`，不改动旧表；发布账号需要既有建表权限。27 的新卡片和 29 的录入弹层由配套小程序 PR 一起提供；不把本地验证或 PR 创建当成微信正式发布。
