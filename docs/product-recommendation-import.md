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

每行代表「种类 × 品种 × 年龄阶段 × 需求 × 品牌 × 产品」的唯一组合。
先展开来源记录的推荐条件，再按六维合并。不同口味或型号保留独立产品；`all_stages`、`unspecified` 不猜测展开到具体年龄。不同品种、年龄或需求不混合。

- 独立筛选列：`species_code`、`breed_code`、`life_stage_code`、`need_code`、`brand`、`product_name`。
- 身份：`recommendation_key` 唯一标识六维分组，`product_key` 标识规范化产品。
- 内容：`recommendation_reason` 和 `product_description` 按原文去重并列；不调用大模型改写理由或生成新结论。
- 推荐来源：`sources` JSON 数组，每项保留 `source_key`、`knowledge_source_id`、`original_source_id`、`batch_id`、`source_model`、`source_provider`、`source_question`、证据状态、该来源在本分组下的推荐理由，以及原始来源条目。
- 计数：`source_count` 为来源记录数，`model_count` 为不同提供方与模型组合数。
- `relation_types` 保留“明确产品适配”与“提问场景”的区别，不把提问品种当作独家适用结论。
- `raw_json.records` 保留可完整重建的标准化来源；`input_hash` 用于重复导入检测。
- 审核：`verification_status` 和 `review_status` 默认 `pending`；原来源的 `supported` 不等于人工审核通过。

查询示例：

```sql
SELECT species_code, breed_code, life_stage_code, need_code,
       brand, product_name, recommendation_reason, source_count, model_count, sources
FROM chongxi_product.product_recommendation
WHERE species_code = 'cat' AND breed_code = 'ragdoll'
ORDER BY life_stage_code, need_code, brand, product_name;
```

不写入正式猫粮目录，不自动参与线上推荐。单条来源有多个需求或年龄条件时会出现在多个分组内；来源条目数、来源关联数和分组行数是不同的计数。

## 运行

在项目环境安装根目录 `requirements.txt` 后，从项目根目录执行。MySQL 配置复用项目 `.env`；原始知识库由 `MODEL_KNOWLEDGE_DATABASE` 配置。

只读预览（不建库、不建表、不写入）：

```bash
python scripts/import_product_recommendations.py --from-db --dry-run --output /tmp/product-recommendations.json
python scripts/import_product_recommendations.py --from-db --source-id 2 --dry-run
python scripts/import_product_recommendations.py --from-db --after-id 0 --limit 10 --dry-run
python scripts/import_product_recommendations.py --input vendor/pet_product_import/example_products.json --dry-run
```

省略 `--dry-run` 仍为只读预览。`--output` 保存标准化来源 `records`、合并结果 `grouped_records` 和 `errors`，终端输出数量统计。

确认预览无错误后入库：

```bash
PRODUCT_KNOWLEDGE_DATABASE=chongxi_product python scripts/import_product_recommendations.py --from-db --apply
```

第一次 `--apply` 初始化独立候选库；需要建库、建表及 DML 权限。也可以预先执行 `vendor/pet_product_import/schema.sql`（文件默认数据库名为 `chongxi_product`）。旧来源粒度单表需要先显式迁移：

```bash
python scripts/merge_product_recommendations.py --dry-run
python scripts/merge_product_recommendations.py --apply
```

迁移拒绝自动处理人工审核记录或被外键引用的旧表。先保存完整 JSON 备份和原建表语句到 `var/product-import-backups/`，建立唯一临时表，写入合并结果并核对全部来源，再原子切换表名。旧表保留为 `product_recommendation_backup_<时间>_<标识>`，不自动删除；业务只使用 `product_recommendation` 一张表。已经迁移过的表重复执行只报告 `already_migrated`。

解析存在任何错误则整体不写入。写入阶段在同一事务内执行，任一记录失败整体回滚。DDL 的初始化单独执行，不属于数据事务。导入使用数据库命名锁防止并发覆盖。增量导入先保留现有来源，再替换本次输入的同来源记录并重新分组；改变年龄或需求时会移除已无来源的旧分组，不删除其他来源。完全相同的分组返回 `unchanged`；人工审核过的分组若需要改变或删除则整体报冲突并回滚。统计 `imported`/`unchanged` 按分组计数。源数据中已删除且本次输入没有出现的来源不会自动清理。

退出码：0 成功；1 解析或执行失败；2 参数不正确。运行只读预览不会初始化旧知识表或触发模型调用。
