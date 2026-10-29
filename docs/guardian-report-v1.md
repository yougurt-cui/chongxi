# 管家统一报告协议 v1（需求 39）

来源：[需求池第 39 项](https://my.feishu.cn/wiki/H5v3wyrKvib1BckmWEjcSrnln8c?sheet=64b243)，实际第 42 行，读取 revision 739。后端基线 `12c301e`，小程序基线 `84e6391`。

## 接口与兼容

继续使用 `POST /api/miniprogram/chat/conversations/<id>/messages`。新报告位于 `message.result.guardian_report`，同一轮的 `messages[].result` 和历史消息保存相同快照。无需新增接口、改变鉴权或数据库迁移。

客户端只识别 `schema="guardian_report"` 且 `version=1`；缺少报告、未知版本或不完整结构时使用原有 `health_analysis`、知识卡片或正文。旧 `reply/content`、`result` 和顶层 `interaction` 保留。报告是展示协议，不改变当前粮档案，也不自动发起推荐或换粮。

## 报告结构

| 字段 | 含义 |
| --- | --- |
| `schema`, `version` | `guardian_report`, `1`，与产品画像版本、计算版本分开 |
| `report_id` | 有 `analysis_id` 时沿用，否则生成；随消息保存，历史回显不重新生成 |
| `scene` | `diet_advice` / `product_analysis` / `ingredient_analysis` / `product_comparison` / `diet_history` |
| `section_order` | 固定 `pet, summary, feeding, community, next_steps` |
| `status` | 产品数据完整为 `ready`，部分可用为 `partial`，没有可用产品依据为 `insufficient_data`；社区状态独立 |
| `pet` | 名称、物种、品种、月龄、阶段、资料状态；不填年龄不猜成年 |
| `summary.text` | 结合宠物资料及产品事实的整体总结，单粮也提供 |
| `feeding` | `mode`、总结文字、产品数组 `products` 和计算差异数组 `comparisons` |
| `community` | `status`、总结、来源条目 `items`、各库读取状态 `source_status`、说明 |
| `next_steps` | 提示、原交互副本和既有动作；按钮执行以消息顶层 `interaction` 为准 |
| `coverage_note` | 历史记录截断或读取不完整的说明 |
| `generation` | `model_summary` / `evidence_fallback`、实际模型、引用的产品/社区 ID |

`feeding.products[]` 包含产品 ID、配方 ID、角色、优势 `strengths`、留心点 `cautions`、六维数据 `dimensions`、结构特征、数据缺口、来源版本。`source.table` 区分统一画像与上传初算快照。初算保留 `result_id/result_stage/quality_flags`，不借用待确认商品的品牌或市场数据。

`comparisons[]` 按第一款与其余款逐一比较；保留两侧产品 ID/名称、每维原始分值和量纲。`delta` 为右减左，只有来源可用、特征/画像/计算版本及量纲相同时才生成。初算参与比较还必须有明确相同的计算版本。较大的分数不统一等于更好，方向由该维 `direction` 说明。数值为结构指标，不是营养含量。

## 数据与生成

- 正式产品以 `catfood_unified_product_profile` 的 active、指定版本数据为依据；批量只读、校验 JSON 哈希，并与本轮最新已发布画像的六维指标核对。缺失或不一致时明确显示资料不足，不在聊天中重建表。
- 初算以当前上传的 `calculation_result` 快照为依据，明确待核对，不使用待确认身份的市场排名。上传入口保留既有与当前粮的比较上下文。
- 社区从 `cat_disease_clues` 按精确品牌聚合反馈数量，从 `kd_problem_guides` 复用现有检索和物种/阶段/审核过滤。知识库沿用第二层成品，不绕回原子 `knowledge_base`。品牌反馈不等于该配方的证据；不向模型传原始评论或用户标识。无结果、不可用和部分可用分别标识。
- 新报告只做一次有 15 秒超时、无自动重试的模型总结；旧多粮概览改用确定性汇总，避免同一分析重复调用模型。模型负责文字，不计算差值。非法结构、长度、未知引用或调用失败时返回确定性报告；没有产品/社区依据时对应模块固定展示缺失说明。
- 各场景继续复用原状态机与 `analysis_id` 校验。旧报告按钮过期、跨用户/宠物/会话拒绝的约束不变。初算不新增未经授权的“确认身份”动作。

## 前端接入

小程序 `mapChatMessage` 校验版本，统一 `guardian-report` 组件展示报告。当前建议/社区经验切换仅改变展示；分值切换不重新请求。结构差异使用各维量纲归一化条形长度，不可比较时只显示原因。下一步按钮沿用原 handler，将原始 `slot/value/analysis_id` 发回后端。

设计图中的品种、阶段在此版本展示档案实际值；修改档案仍走现有入口。现有流程没有的行为不会从设计图按钮文字推导执行。

## 验证与发布

新增 `tests/test_guardian_report.py` 覆盖协议、入口、快照、来源校验、初算、缺失数据、版本差异、社区归因及模型降级。小程序新增 `node scripts/check-guardian-report.cjs`，覆盖版本回退、历史回显、按钮参数、标签与分值切换。

发布前确认目标环境已有最新的统一画像；如需物化，应沿用既有 `scripts/build_unified_product_profiles.py` 的运维流程，不能由聊天接口隐式创建或重算。此次开发未修改线上数据库或部署服务。
