# 宠物知识蒸馏管线（/api/pet-knowledge）

目标：把抖音帖子+评论蒸馏成原子知识库，服务两个用途——科普选题、大模型问答的先验证知识。

来源：workspace/pet_knowledge_pipeline 的 MVP，适配 csv_labeling MySQL 后整合进本 app。
2026-10-24 精简：9 表 → 4 表（见下）。

## 架构

| 组件 | 位置 |
|---|---|
| 服务层 | `services/pet_knowledge_pipeline_service.py` |
| API | `api/pet_knowledge_api.py`（blueprint `/api/pet-knowledge`，main.py 已注册） |
| 知识表 | csv_labeling 共 4 张：knowledge_tasks / knowledge_base / knowledge_sources / knowledge_comment_items |
| 来源表 | `douyin_raw_content` / `douyin_raw_comments`；仅唯一非空标题允许临时桥接 |
| LLM | config.yaml `openai.qwen_api_key`，默认模型 qwen-plus |

## 四表职责

| 表 | 粒度 | 职责 |
|---|---|---|
| `knowledge_tasks` | 每帖一行 | 抽取台账：content_hash 指纹增量、status/attempts/error、**raw_json 存 LLM 原始输出**（审计） |
| `knowledge_base` | 每个知识点一行 | 主表：跨帖合并后的标准知识，`evidence_json` 存帖子粒度独立证据 |
| `knowledge_sources` | 知识点×帖子 | 来源关联（unique knowledge_id+post_id），支撑 source_count |
| `knowledge_comment_items` | 每条评论一行 | 清洗 / 提问分流 / 蒸馏消费状态（pending/done/failed）/ **votes_json 存证据立场**（origin+independent） |

已删除的 5 张旧表：`knowledge_candidates`（原始抽取→tasks.raw_json）、`knowledge_candidate_sources`、
`knowledge_clean_comments` + `knowledge_questions` + `knowledge_comment_tasks`（三合一→comment_items）、
`knowledge_evidence_votes`（帖子粒度→base.evidence_json，评论粒度→item.votes_json）。

## 数据流

```
douyin_raw_content(按 ingest_after/keyword_pattern 过滤)
  → 读评论(唯一标题桥接；歧义数据隔离) → 内容指纹比对(变了吗?)
  → LLM 抽取原子观点(≤12条/帖, subtopic 枚举, 提问句/编造数字过滤)
  → validate_items 校验 → 保守合并(同桶相似度≥0.94)
  → 直接写 knowledge_base + knowledge_sources（原始输出存 tasks.raw_json）
```

评论蒸馏走动态批次（不再持久化批次表）：

```
唯一关联的一篇帖子
  → 评论清洗/精确去重 → knowledge_comment_items（幂等 upsert，is_question/is_noise 分流）
  → run 时按 status=pending 动态切 25 条/批 → 千问提炼 + 来源ID校验 + 二次问句拦截
  → merge 进 knowledge_base/knowledge_sources，origin 立场写回条目 votes_json
  → 批次成功 items 置 done；失败 attempts+1，3 次后置 failed（可重跑）
```

## 接口

### POST /api/pet-knowledge/run （admin）

```bash
curl -X POST http://127.0.0.1:8000/api/pet-knowledge/run \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: $MINIPROGRAM_ADMIN_TOKEN" \
  -d '{"ingest_after":"2026-10-01 14:31:01","keyword_pattern":"%养宠%"}'
```

| 参数 | 说明 |
|---|---|
| `ingest_after` | 只处理该时间之后的帖子（配合采集入库的时间线） |
| `keyword_pattern` | source_keyword LIKE 模式 |
| `extractor` | `llm`（默认，真抽取）/ `demo`（规则演示，不花钱） |
| `limit` / `batch` | 限流用 |

**增量**：任务表存每帖内容指纹（管线版本+标题+正文+全部评论），内容和分类体系都未变化时直接 skip，不调 LLM。响应：`{"scanned","updated","skipped","failed","posts":[...]}`。

耗时参考：14 帖约 5 分钟（每帖一次 LLM 调用，15-20s）。

### POST /api/pet-knowledge/verify （admin）

社区证据校验：按 topic×subtopic 分桶，再把知识与证据材料拆成小批次调用千问。证据分为两种独立粒度，存储位置不同：

- **帖子粒度**：作者标题/正文直接赞同或反对知识；证据引用 `post:<id>`，写 `knowledge_base.evidence_json`。
- **评论粒度**：某条具体评论赞同或反对知识；证据引用 `comment:<id>`，写对应 `knowledge_comment_items.votes_json`（role=independent）。

互证判定（与旧版一致）：

| 条件 | evidence_status |
|---|---|
| 赞同≥2 帖且无反对 | 社区经验 |
| 有反对，中低风险 | 有争议 |
| 有反对，**高风险** | 保持待验证（证据仍会保存） |

`专业已验证` 保持人工专属且不会被自动校验覆盖。某个分桶调用失败时保留该桶旧结果，不用空结果覆盖。

可用 `topic` / `subtopic` 指定范围，或用 `bucket_offset` + `max_buckets` 分段执行。单次模型输入还可通过 `claim_batch`、`comment_batch`、`comments_per_post` 控制。

### POST /api/pet-knowledge/comment-tasks/prepare （admin）

只清洗评论写入 comment_items、分流提问，不调用模型；幂等可重复执行。参数：`post_id`。

### POST /api/pet-knowledge/comment-tasks/run （admin）

按 pending 条目动态切批蒸馏一篇帖子。可用 `batch_size`（默认25）、`max_tasks` 限制本次模型调用批数；已 done 的评论自动跳过，failed 批次重跑 `/run` 即可重试。

### POST /api/pet-knowledge/dedup （admin）

离线语义去重（方案B）：同桶（topic+subtopic+knowledge_type+**risk**）知识点做 embedding 召回
（text-embedding-v3），相似度 ≥ `min_sim`（默认0.85）且**物种/生命周期无硬冲突**的对交 LLM 裁决
"是否同一观点"（payload 带风险与适用条件，prompt 明确禁止跨风险/跨条件合并），确认后合并：

- content 取更长的表述；sources 取并集（重定向+去重）
- `comment_items.votes_json` 与 `base.evidence_json` 里的旧 id 重写到主行
- evidence_status 按合并后证据并集**重算**（口径同 /verify：≥2赞同无反对→社区经验；有反对且非高风险→有争议；专业已验证受保护），support/oppose 取并集去重帖数；审核状态 status 取簇内最优

```bash
# 先 dry_run 看合并计划，确认后去掉 dry_run 执行
curl -X POST .../api/pet-knowledge/dedup -d '{"dry_run": true, "topics": ["日常喂养"]}'
```

参数：`min_sim` / `max_pairs`（单次最多裁决对数，默认200）/ `dry_run` / `topics` / `model`。

### GET /api/pet-knowledge/stats

四表计数（tasks / knowledge / sources / comment_items + pending）+ 主题/子主题/风险/证据分布。

### GET /api/pet-knowledge/list

Query：`topic` / `subtopic` / `risk` / `evidence_status` / `limit` / `offset`。排序：高风险在前、互证多在前。

## 设计要点（改动前必读）

1. **四表无外键约束**，靠逻辑关联（knowledge_id / post_id），同库 csv_labeling 便于 join。
2. **合并分两层（方案A+B，2026-10-24 起）**：抽取时把同 topic 已有知识点（最多40条）喂给模型，同一观点（措辞不同）输出 `same_as_id` 直接对齐——零额外调用防增量变体；存量变体由 `/dedup` 离线清理（embedding 召回 ≥0.85 + LLM 裁决）。表层相似度 0.94 仍是兜底。实测社区知识措辞互异（桶内最大表面相似度仅 0.39），不要试图调表层阈值解决语义合并。
3. **证据可追溯但不再单表**：帖子粒度证据在 base.evidence_json，评论粒度立场在 item.votes_json（origin=知识来源，independent=社区互证）；计数（support/oppose_posts）只由 independent 证据驱动，专业审核状态不被模型覆盖。
4. **指纹含评论 id**：换库/迁移数据必触发全量重抽，属预期行为。
5. **candidates 已删**：单帖原始抽取结果只存 tasks.raw_json；重抽时 unlink_post 删来源关联、孤立知识点标"无有效来源"，merge 时相似条目自动复用原知识点。
6. **批次不持久化**：行级 status（pending/done/failed）就是断点，无需批次表。
7. **合并防幻觉护栏（2026-10-24）**：`same_as_id` 必须通过 topic/subtopic/type/risk/conditions 全等校验才生效，否则回退表层匹配；dedup 桶含 risk、物种/生命周期不相交的候选对被硬过滤（不送模型）。
8. **评论变更自愈（2026-10-24）**：prepare 检测到评论正文变化 → 清 origin 票、done 重置 pending 重新蒸馏；来源中已删除的评论行同步清除。/verify 每桶先清该桶全部旧 independent 票再写本轮结果（上轮命中、本轮未命中的证据不会残留）。

## 已知边界

- 帖子正文多为空（知识在视频里），抽取依赖标题+评论；视频转写接入后可加"作者主张"证据信号。
- douyin_raw_comments 无 aweme_id 外键。当前只使用唯一非空标题，重复、空标题和无法匹配的数据不参与；采集侧仍应尽早补 aweme_id。
- LLM 输出非确定性：同一批数据重跑条数会浮动（±10 条内），属正常。
- 2026-10-24 前的旧 5 张表已 drop（当时全空，无数据迁移）。
