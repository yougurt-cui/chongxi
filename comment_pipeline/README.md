# Comment Pipeline

轻量评论处理链路：原始评论 → 公共清洗层 → 纯规则多标签 Router → Choice / Product Preference。

## Router 定义

- `choice`：猫粮、罐头、冻干、猫条和宠物零食等食品相关的 Need、Decision、Experience、Switch，输出到历史表 `catfood_choice_comments_filtered_v2`。
- `product_preference`：宠物玩具、逗猫棒、猫抓板、轨道球、隧道等非食品用品的结构、互动、耐用性和偏好，输出到 `product_preference_events`。
- 药品、医疗器械、定位器、摄像头和健康监测设备属于保留领域，当前不进入上述两条 Pipeline，后续增加独立标签和处理器。

Router 只使用明确领域词和行为/反馈词分流；规则无法明确判断的评论保留为未命中，不调用大模型。
Product Preference 的所有字段也都由确定性规则抽取，不调用大模型。
产品领域的判断优先级为：评论正文中的产品词 > 采集时的 `search_keyword/query_keyword` >
帖子标题和内容。检索关键词只补足产品领域，真正入表仍要求评论正文命中选择、体验、产品属性或互动信号。

## 公共清洗层

`common/cleaner.py` 的 `normalize_text` 在零宽字符与 URL 清理之外，还会剥离四类平台噪音：

- `#话题#` 标签
- `[舔屏]`、`[心心眼R]` 这类平台表情占位符
- emoji 字符（含昵称里的 emoji）
- `@昵称` 提及

剥离顺序为「话题 → 表情占位符 → emoji → 提及」，确保 `@EMILY🌟` 这种昵称夹 emoji 的提及能被整段清掉。
`raw_text` 原样保留在 `comment_clean_base` 作为审计回溯，`clean_text` 才是下游唯一输入。

偏好抽取另有低信息量闸门：`is_low_information` 会丢弃实义字符数低于 `LOW_INFO_MIN_CHARS`（默认 6）
或只剩语气词的文本，这类评论计入 `summary.preference_skipped_low_information`，不写入
`product_preference_events`（其路由记录仍保留在 `comment_router_result`，便于区分"未命中"与"被质量闸门拦下"）。

## 运行

先用少量数据验证：

```bash
python -m comment_pipeline.run_pipeline --limit 100
```

正式运行：

```bash
python -m comment_pipeline.run_pipeline
```

只验证、不建表和写库：

```bash
python -m comment_pipeline.run_pipeline --limit 100 --dry-run
```

默认复用已经写入 `comment_router_result` 的评论。需要重新路由时使用
`--reprocess`。

## 输出

- `comment_clean_base`：公共清洗、平台统一、基础品牌及产品识别结果。
- `comment_router_result`：`choice` 与 `product_preference` 多标签路由结果。
- `catfood_choice_comments_filtered_v2`：复用历史V2表，保存猫粮四类Choice结果；通过来源唯一键跳过已经处理的数据。
- `product_preference_events`：仅保存玩具类用品的结构、互动、收益、用户证明、痛点和偏好；表本身已表达领域，不再保存 `product_type`。
- `var/catfood_choice_comment_artifacts/<run_id>/`：两条 Pipeline 的 CSV 和 `summary.json`。

## 已知待办

- `evidence_text` 保存清洗后正文；小红书 `xiaohongshu_raw_comments.comment_text` 中仍存在
  多行拼接的整块评论区文本（粒度不统一），当前按单条评论处理，尚未拆条。
- `product_preference_events` 未冗余 `brand` / `product_category` / `run_id`，下游聚合需回 join `comment_clean_base`。
- `catfood_decision_comment_labels` 里现存 23071 行的 `label_version` 是 `decision_comment_qwen_v2`，
  与 `decision_comment_labeler.py` 的 `decision_comment_rules_v1` 不一致。因此该脚本一旦运行，
  会按版本号另起一套规则标签（约 1.9 万行），不与 Qwen 行冲突但会新增一个版本；这是脚本原有行为，
  与本次文本口径切换无关，是否需要合并版本待定。

## HTTP API 入口

`POST /api/comment-clean-sync/run` 已切换到本目录的统一 Pipeline，不再直接调用旧的
`scripts/filter_catfood_choice_comments.py`。

| 入口 | 触发方式 | 说明 |
|---|---|---|
| `comment_pipeline/run_pipeline.py` | `python -m comment_pipeline.run_pipeline` | 清洗 → 路由 → 两条 Pipeline |
| `comment_pipeline/run_pipeline.py` | `POST /api/comment-clean-sync/run` | 公共清洗 → Router → Choice / Product Preference，随后同步四张表 |

统一 Pipeline 使用 `common/cleaner.normalize_text`，并保留了原 Choice 脚本的两处词典级修正：

- 剥离噪音后再判定，`@秃鹫`（→掉毛）、`@减肥猫`（→肥胖）这类昵称驱动的病症误命中不再发生。
- 单字「吐」加否前瞻 `吐(?!槽|司|露|舌)`，避免「吐槽」被算成呕吐；`吐了` / `吐黄水` 等词条不受影响。

表内 `comment_text` 仍是原文（留痕），`normalized_text` 才是清洗后文本；下游消费请读 `normalized_text`。

## 下游打标脚本的文本口径（增量切换，不回填历史）

`scripts/{need,decision,experience,switch}_comment_labeler.py` 已改为读 `normalized_text`：

- `iter_source_rows` 取 `COALESCE(NULLIF(TRIM(normalized_text),''), comment_text)` 并别名为
  `label_text`；源表没有 `normalized_text` 列时自动退回 `comment_text`。
- `source_text(row)` 会把文本再走一次公共清洗层，所以即使历史行的 `normalized_text` 本身也脏
  （28.7% 的行带 `[表情]`），取到的仍是干净文本，**无需回填源表**。
- **`content_hash` 仍按原文 `comment_text` 计算**。历史行的哈希本来就是 `MD5(comment_text)`，
  口径保持一致，已打标评论才会继续被跳过，历史行不会被本次切换改写。
- `load_existing_hashes()` 优先读 `content_hash` 列；该列尚未建出时（首次非 dry-run 才由
  `ensure_target_table` 补列）直接 `SELECT DISTINCT MD5(comment_text)`，效果一致。
- 历史哈希现在 **dry-run 也会加载**，所以可以先 `--dry-run` 看清会跳过多少历史行、只新增多少行。
- `experience_comment_labeler.py` 原本没有任何跳过逻辑（每次全量覆盖 upsert），
  已补上按 `source_comment_id` 的增量跳过，否则切换文本口径会重写全部历史行。

只读验算结果（`--dry-run`）：

| 脚本 | 历史行 | 本轮将写入 | 与历史 id 重叠 | 落库文本含噪音 |
|---|---:|---:|---:|---:|
| need | 29116 | 1316 | 0 | 0 |
| decision | 0（现存是 qwen_v2 版本） | 18970 | 0 | 0 |
| experience | 17727 | 1120 | 0 | 0 |
| switch | 5196 | 261 | 0 | 0 |
