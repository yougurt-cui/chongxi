# 小程序瞬间墙举报流程

## 1. 举报人流程

1. 用户在瞬间墙列表或瞬间详情页点击帖子右上角 `···`。
2. 选择“举报”。
3. 系统要求用户已登录；未登录时跳转到“我的”页登录。
4. 用户选择举报原因：
   - 广告/垃圾信息
   - 辱骂/攻击
   - 虚假或误导信息
   - 侵犯隐私
   - 伤害动物
   - 其他问题
5. 用户可补充说明。
6. 前端提交到 `POST /api/miniprogram/moments/<post_id>/reports`。
7. 后端写入 `miniprogram_cat_moment_report`，记录举报人、被举报人、帖子快照、原因、说明和证据来源。
8. 同一用户重复举报同一帖子时，不新增多条记录，而是更新原举报并重新置为 `pending`。

## 2. 被举报人流程

当前版本先完成数据闭环，不主动通知被举报人。

平台审核后可执行：

- `reject`：驳回举报，帖子继续展示。
- `resolve`：确认举报已处理，仅更新举报状态。
- `hide_post`：确认违规并隐藏帖子，帖子状态改为 `hidden`，不再出现在瞬间墙和详情接口中。

后续如果需要通知，可基于 `reported_user_id` 增加站内消息或模板消息。

## 3. 平台处理流程

1. 平台侧使用管理员接口拉取举报列表：
   - `GET /api/miniprogram/admin/moment-reports?status=pending`
   - 请求头需要 `X-Admin-Token`
2. 平台查看举报记录：
   - 举报人：`reporter_user_id`
   - 被举报人：`reported_user_id`
   - 被举报帖子：`post_id`
   - 举报原因：`reason_code` / `reason_text`
   - 补充说明：`detail`
   - 帖子快照：`post_title` / `post_content_preview`
3. 平台审核处理：
   - 标记处理中：`action=processing`
   - 驳回：`action=reject`
   - 处理完成但不隐藏帖子：`action=resolve`
   - 隐藏违规帖子：`action=hide_post`
4. 平台处理接口：
   - `PATCH /api/miniprogram/admin/moment-reports/<report_id>`
   - 请求头需要 `X-Admin-Token`
   - 请求体示例：

```json
{
  "action": "hide_post",
  "operator_id": "admin",
  "review_note": "内容涉及违规，已隐藏"
}
```

## 4. 数据表

举报表：`miniprogram_cat_moment_report`

核心字段：

- `reporter_user_id`：举报人
- `reported_user_id`：被举报人，即帖子作者
- `post_id`：被举报帖子
- `reason_code` / `reason_text`：举报原因
- `detail`：举报补充说明
- `evidence_json`：证据上下文
- `post_title` / `post_content_preview`：举报时的帖子快照
- `status`：`pending` / `processing` / `resolved` / `rejected`
- `review_note`：平台审核备注
- `operator_id`：平台处理人
- `handled_at`：处理时间

## 5. 配置要求

管理员接口必须配置环境变量：

```bash
MINIPROGRAM_ADMIN_TOKEN=换成强随机字符串
```

未配置时，平台审核接口会返回 `401`，避免审核能力暴露。
