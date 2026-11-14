# 产品推荐解析器

本目录改编自用户提供的 `pet_product_import.zip`，不包含压缩包中的 `__pycache__`。

使用项目统一入口：

```bash
python scripts/import_product_recommendations.py --input vendor/pet_product_import/example_products.json --dry-run
python scripts/import_product_recommendations.py --from-db --dry-run
```

也支持 `python vendor/pet_product_import/import_products.py --from-db --dry-run`。

详细输入格式、数据库字段、幂等语义与入库命令见 [项目说明](../../docs/product-recommendation-import.md)。

`schema.sql` 仅创建 `product_recommendation` 一张表，按种类、品种、年龄阶段、需求、品牌、产品六维合并，同时保存全部推荐来源。来源粒度的旧单表请使用 `scripts/merge_product_recommendations.py` 显式迁移。
