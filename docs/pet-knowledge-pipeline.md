# 宠物知识蒸馏管线（/api/pet-knowledge）

目标：把抖音帖子+评论蒸馏成原子知识库，服务两个用途——科普选题、大模型问答的先验证知识。

来源：workspace/pet_knowledge_pipeline 的 MVP，适配 csv_labeling MySQL 后整合进本 app。

## 架构

| 组件 | 位置 |
|---|---|
| 服务层 | `services/pet_knowledge_pipeline_service.py` |
| API | `api/pet_knowledge_api.py`（blueprint `/api/pet-knowledge`，main.py 已注册） |
| 知识表 | csv_labeling：任务、候选、正式知识、来源、证据票据共 5 张表 |
| 来源表 | `douyin_raw_content` / `douyin_raw_comments`；仅唯一非空标题允许临时桥接 |
| LLM | config.yaml `openai.qwen_api_key`，默认模型 qwen-plus |

## 数据流

```
douyin_raw_content(按 ingest_after/keyword_pattern 过滤)
  → 读评论(唯一标题桥接；歧义数据隔离) → 内容指纹比对(变了吗?)
  → LLM 抽取原子观点(≤12条/帖, subtopic 枚举, 提问句/编造数字过滤)
  → validate_items 校验 → 保守合并(同桶相似度≥0.94)
  → knowledge_candidates(候选) + knowledge_base(标准知识) + knowledge_sources(证据链)
```

评论批次蒸馏采用另一条可断点链路：

```
唯一关联的一篇帖子
  → 评论清洗/精确去重
  → 纯提问写 knowledge_questions（不进入知识候选）
  → 陈述与经验每25条写一个 knowledge_comment_tasks
  → 千问提炼 + 来源ID校验 + 二次问句拦截
  → knowledge_candidates + knowledge_candidate_sources
  → origin 证据写 knowledge_evidence_votes
```

相关表：

- `knowledge_clean_comments`：清洗结果、问题/经验/噪音角色及版本；
- `knowledge_questions`：被隔离的真实用户问题，可供FAQ和选题使用；
- `knowledge_comment_tasks`：评论批次、输入哈希、状态、重试次数；
- `knowledge_candidate_sources`：候选知识到具体评论的来源关系。

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

社区证据校验：按 topic×subtopic 分桶，再把知识与证据材料拆成小批次调用千问。证据分为两种独立粒度：

- **帖子粒度**：作者标题/正文直接赞同或反对知识；证据引用为 `post:<id>`。
- **评论粒度**：某条具体评论赞同或反对知识；证据引用为 `comment:<id>`。

两种证据分别写入 `knowledge_evidence_votes`，保存帖子 ID、证据粒度、证据引用、态度和服务器核对后的原文片段。同一帖子可以出现“作者赞同、评论反对”，不会互相覆盖：

| 条件 | evidence_status |
|---|---|
| 赞同≥2 帖且无反对 | 社区经验 |
| 有反对，中低风险 | 有争议 |
| 有反对，**高风险** | 保持待验证（反对票据仍会保存） |

`专业已验证` 保持人工专属且不会被自动校验覆盖。某个分桶调用失败时保留该桶旧结果，不用空结果覆盖。

可用 `topic` / `subtopic` 指定范围，或用 `bucket_offset` + `max_buckets` 分段执行。单次模型输入还可通过 `claim_batch`、`comment_batch`、`comments_per_post` 控制。

### POST /api/pet-knowledge/comment-tasks/prepare （admin）

只清洗、分流提问并创建评论批次，不调用模型。参数：`post_id`、`batch_size`（默认25）。

### POST /api/pet-knowledge/comment-tasks/run （admin）

创建并运行一篇帖子的评论批次。可用 `max_tasks` 限制本次模型调用数量；已完成且输入未变化的任务自动跳过。

### GET /api/pet-knowledge/stats

四表计数 + 主题/子主题/风险/证据分布。

### GET /api/pet-knowledge/list

Query：`topic` / `subtopic` / `risk` / `evidence_status` / `limit` / `offset`。排序：高风险在前、互证多在前。

## 设计要点（改动前必读）

1. **知识表与来源表同库**（csv_labeling）。四表无外键约束、靠逻辑关联（knowledge_id/post_id），为兼容迁移与轻量部署。
2. **合并是保守的**：0.94 字符相似度 + 全字段相等。实测社区知识天然互异（桶内最大相似度仅 0.39），跨帖互证合并要靠向量召回 + LLM/NLI，这是下一步，不是调阈值能解决的。
3. **证据可追溯**：赞同/反对不仅保存计数，还保存帖子、评论和原文片段；`origin` 仅表示知识来源，`independent` 才参与社区互证，专业审核状态不被模型覆盖。
4. **指纹含评论 id**：换库/迁移数据必触发全量重抽，属预期行为。
5. **噪声兜底**：提问句在 validate_items 硬过滤；品牌软广/个人案例靠 evidence_status=待验证 + 人工审核兜底，上线 QA 前必须过审。

## 已知边界

- 帖子正文多为空（知识在视频里），抽取依赖标题+评论；视频转写接入后可加"作者主张"证据信号。
- douyin_raw_comments 无 aweme_id 外键。当前只使用唯一非空标题，重复、空标题和无法匹配的数据不参与；采集侧仍应尽早补 aweme_id。
- LLM 输出非确定性：同一批数据重跑条数会浮动（±10 条内），属正常。
