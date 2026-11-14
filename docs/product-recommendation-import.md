# 产品推荐知识标准化

改编自 `pet_product_import.zip`，入口为 `scripts/import_product_recommendations.py`，解析器保存在 `vendor/pet_product_import/import_products.py`。

## 输入与输出

支持压缩包的扁平 JSON 示例，以及查询导出的完整对象数组：

```sql
SELECT * FROM knowledge_point_source
WHERE category = 'product_recommendation'
ORDER BY id;
```

数据库对象的 `sources` 是「产品原文条目 → 原始模型来源」的嵌套 JSON。解析器逐来源处理，使用 `source_context.structured_item` 的商品名、品牌、理由、条件；缺结构字段时仅尝试原始条目的分号格式。不会从汇总正文推测商品。

来源组的物种、品种、生命阶段保留为关联场景；猫狗共用来源在已明确的对象物种下分别处理。扁平数据不能判断物种或存在猫狗混合歧义时报告错误。不同模型的适用条件和需求不互相混用。

默认仅写入独立库的一张表：`chongxi_product.product_recommendation`。

每行代表「一个知识场景下，一个原始来源对一款产品的推荐」。同一产品由不同模型或不同问题推荐时分别保留，不丢失推荐来源。

- 产品信息：`product_key`、`species_code`、`brand`、`product_name`、`product_description`。
- 推荐内容：`recommendations` JSON 数组，每个元素包含品种、生命阶段、需求、推荐理由、关系类型及证据状态。无具体需求使用 `general_recommendation`。
- 推荐来源：`source_model`、`source_provider`、`source_question`、`batch_id`、`source_evidence_status`。
- 来源追溯：`knowledge_source_id`（知识对象 ID）、`original_source_id`（原始推荐来源 ID）、`source_key`、`input_hash`、`raw_json`（含完整原始来源）。
- 审核状态：`verification_status` 和 `review_status`，默认 `pending`。

例如查询某款产品的推荐来源：

```sql
SELECT brand, product_name, source_model, source_provider, source_question,
       knowledge_source_id, original_source_id, recommendations
FROM chongxi_product.product_recommendation
WHERE product_name LIKE '%六种鱼%';
```

来源键包含原始来源 ID、知识对象 ID、产品身份，避免跨场景来源丢失。所有候选默认 `pending`；原始 `supported` 等标记单独保留，不提升为人工核验状态。不写入正式猫粮目录，不自动参与线上推荐。

## 运行

在项目环境安装根目录 `requirements.txt` 后，从项目根目录执行。MySQL 配置复用项目 `.env`；原始知识库由 `MODEL_KNOWLEDGE_DATABASE` 配置。

只读预览（不建库、不建表、不写入）：

```bash
python scripts/import_product_recommendations.py --from-db --dry-run --output /tmp/product-recommendations.json
python scripts/import_product_recommendations.py --from-db --source-id 2 --dry-run
python scripts/import_product_recommendations.py --from-db --after-id 0 --limit 10 --dry-run
python scripts/import_product_recommendations.py --input vendor/pet_product_import/example_products.json --dry-run
```

省略 `--dry-run` 仍为只读预览。`--output` 保存 `records` 和 `errors`，终端输出数量统计。

确认预览无错误后入库：

```bash
PRODUCT_KNOWLEDGE_DATABASE=chongxi_product python scripts/import_product_recommendations.py --from-db --apply
```

第一次 `--apply` 初始化独立候选库；需要建库、建表及 DML 权限。也可以预先执行 `vendor/pet_product_import/schema.sql`（文件默认数据库名为 `chongxi_product`）。若已有旧版三表结构则明确报错，不覆盖或删除原数据；应选择新的独立库或自行评估迁移。新版仅创建这一张表。

解析存在任何错误则整体不写入。写入阶段在同一事务内执行，任一记录失败整体回滚。DDL 的初始化单独执行，不属于数据事务。相同来源和输入重复运行返回 `unchanged`；来源改变时更新待审核记录，已人工审核的记录则报冲突，保留旧数据。

退出码：0 成功；1 解析或执行失败；2 参数不正确。运行只读预览不会初始化旧知识表或触发模型调用。
