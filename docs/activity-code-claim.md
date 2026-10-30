# 小程序活动码领取（发帖活动）

用户在「猫猫圈」发帖成功后领取活动码（兑换凭据）的能力。原实现是一份独立的
FastAPI 服务（`chongxi_activity_code.zip`），本文件记录它被改造并接入当前 Flask 后端后的
真实接口、表结构与运维方式。

## 1. 代码位置

| 层 | 文件 |
|---|---|
| 服务 | `services/miniprogram_activity_service.py` |
| 接口 | `api/miniprogram_api.py`（搜索「活动码领取」区块） |
| 导入脚本 | `scripts/import_activity_codes.py` |
| 表结构 | 服务内 `init_miniprogram_activity_tables()` 自动建表，幂等 |

与原 zip 包的差异（改造点）：

| 原包 | 本项目 |
|---|---|
| FastAPI + SQLAlchemy | Flask + pymysql（与 `services/` 其他小程序服务一致） |
| `PyJWT` 验 `sub` | 复用 `miniprogram_user_session` 令牌（`require_user_by_token`） |
| 表 `posts(id,user_id,status='published')` | `miniprogram_cat_moment_post`（`status='active'`，id 为 CHAR(32)） |
| `activity_id/post_id` 均为 BIGINT | `activity_id` 为整数，`post_id` 为 32 位字符串 |
| 仅 `OUT_OF_STOCK` | 增加 `BUSY`、活动有效期/分类校验、运营统计与补码接口 |

## 2. 数据表

- `miniprogram_activity`：活动配置（可选）。`status`(active/paused/closed)、`starts_at`/`ends_at`（UTC）、
  `required_category_code`（限定发帖话题，如 `STOOL`）。
- `miniprogram_activity_code`：活动码库存。`UNIQUE(activity_id, code)`，除码本身外还保留导出数据里的
  业务字段：`credits_per_code`（每码积分）、`redeem_ends_at`（兑换截止）、`sequence_no`（批次内序号）、
  `batch`（批次号）、`source_status`（导出时的状态）、`snapshot_at`（导出快照时间）。
  旧表会在 `init_miniprogram_activity_tables()` 里按需 `ALTER TABLE` 补列，无需手工迁移。
- `miniprogram_activity_claim`：领取记录。`UNIQUE(activity_id, user_id)` 保证一人一码，
  `UNIQUE(code_id)` 保证一码一人，`FOREIGN KEY(code_id)` 防止误删已发放的码。

活动未登记时仍可领取（只按 `activity_id` 隔离库存，会写 warning 日志）；一旦登记，则严格校验
状态、起止时间与限定分类。

## 3. 接口

### 3.1 发帖即领（推荐）

`POST /api/miniprogram/moments`，在原有发帖入参上多传一个 `activity_id`：

```json
{
  "content": "今天猫猫的搞笑瞬间",
  "category_code": "FUNNY",
  "images": [{"url": "https://..."}],
  "activity_id": 1001
}
```

发帖事务提交成功后才会分配活动码；领取失败不会回滚发帖，结果放在响应的 `activity` 字段：

```json
{
  "ok": true,
  "item": {"id": "3f2c...", "...": "..."},
  "activity": {"ok": true, "status": "SUCCESS", "message": "领取成功", "activity_id": 1001, "code": "CX-8F3K2A"}
}
```

不传 `activity_id` 则行为与之前完全一致（响应无 `activity` 字段）。

### 3.2 单独领取

`POST /api/miniprogram/activity/claim`，Header `Authorization: Bearer <token>`：

```json
{"activity_id": 1001, "post_id": "3f2c9a..."}
```

`status` 取值：`SUCCESS`（成功，返回 `code`）、`ALREADY_CLAIMED`（已领过，返回原码）、
`OUT_OF_STOCK`（库存为 0）、`BUSY`（有库存但都被并发请求锁住，前端可稍后重试）。

HTTP 状态码：`200` 业务结果；`400` 参数/活动不可用；`401` 未登录；`403` 帖子不属于当前用户或已失效；`500` 服务异常。

### 3.3 查询已领码

`GET /api/miniprogram/activity/<activity_id>/my-code`，用于弹窗关闭后恢复展示。
未领取返回 `status=NOT_CLAIMED`，`code` 为 `null`。

### 3.4 运营接口（需 `X-Admin-Token`）

- `GET /api/miniprogram/admin/activity/<activity_id>/stats`：总量 / 已领 / 剩余。
- `POST /api/miniprogram/admin/activity/<activity_id>/codes`：补码，
  body `{"text": "每行一个码"}` 或 `{"codes": [...]}`，支持 `"dry_run": true`。

## 4. 小程序端调用

```js
// 发帖成功后（也可直接在发帖请求里带 activity_id）
wx.request({
  url: `${apiBaseUrl}/api/miniprogram/activity/claim`,
  method: 'POST',
  header: { Authorization: `Bearer ${token}` },
  data: { activity_id: 1001, post_id: postId },
  success: ({ data }) => {
    if (data.status === 'SUCCESS' || data.status === 'ALREADY_CLAIMED') {
      wx.showModal({ title: '活动码', content: data.code, showCancel: false })
    } else if (data.status === 'BUSY') {
      // 稍后重试，或提示"领取人数较多"
    } else if (data.status === 'OUT_OF_STOCK') {
      wx.showToast({ title: '活动码已领完', icon: 'none' })
    }
  },
})
```

## 5. 运营操作

```bash
# 1) 登记活动（可限定话题，时间为 UTC）
python scripts/import_activity_codes.py --activity-id 1001 --name "双十一发帖有礼" \
  --category STOOL --starts-at "2026-11-01 00:00:00" --ends-at "2026-11-12 00:00:00"

# 2) 导入活动码
#    a. 纯文本，一行一个码
python scripts/import_activity_codes.py --activity-id 1001 --file codes.txt --batch 20261030 --dry-run
python scripts/import_activity_codes.py --activity-id 1001 --file codes.txt --batch 20261030
#    b. 平台导出的 CSV（自动识别表头，保留积分/兑换截止/批次/序号；campaign_name 会自动登记活动）
python scripts/import_activity_codes.py --activity-id 1001 --file pexport_xxx.csv --dry-run
python scripts/import_activity_codes.py --activity-id 1001 --file pexport_xxx.csv

# 3) 查库存
python scripts/import_activity_codes.py --activity-id 1001 --stats

# 4) 停办
python scripts/import_activity_codes.py --activity-id 1001 --status closed
```

`--dry-run` 全程只读：不写活动、不写码。重复导入同一文件是安全的
（已存在的码跳过，返回 `skipped_existing`，不会覆盖已有字段）。

### CSV 列名映射

| 导出列 | 入库字段 |
|---|---|
| `code` | `code`（必填） |
| `campaign_name` | 活动名称（未指定 `--name` 时自动登记活动） |
| `credits_per_code` | `credits_per_code` |
| `redeem_ends_at` | `redeem_ends_at`（`2026-11-06T02:48:00Z` 转 UTC） |
| `batch_id` | `batch` |
| `sequence_no` | `sequence_no` |
| `status` | `source_status` |
| `snapshot_at` | `snapshot_at` |

## 5.1 当前已导入

| 活动 ID | 名称 | 码总量 | 每码积分 | 兑换截止 | 批次 |
|---|---|---|---|---|---|
| 1001 | 测试活动 | 9 | 10.00 | 2026-11-06 02:48 (UTC) | `pbatch_e2c84005b2b92a1375e4b1b4400b61bc` |

序号 1–7、9、10（源数据本身缺 8）。

## 6. 并发与边界

- 抢码使用 `SELECT ... FOR UPDATE SKIP LOCKED` + `READ COMMITTED`；唯一键兜底，不依赖应用层锁。
  已用 12 并发抢 5 码连续三轮验证：发放 5 个、无重复、无超发。
- 一人一码按 `activity_id` 维度；如需"每天可领一次"，需把唯一键改为 `(activity_id, user_id, claim_date)`，
  并在 `activity_claim` 增加日期列，属于后续扩展。
- 活动码是兑换凭据，接口与脚本均不写日志到业务表；生产环境建议对领取接口做频率限制。
- `post_id` 只校验"属于当前用户 + status=active + 分类命中"，不校验帖子发布时间；
  如需"发帖后 7 天内才能领"，在 `_assert_post_belongs_to_user` 里加 `created_at` 比较即可。
