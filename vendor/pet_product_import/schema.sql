CREATE DATABASE IF NOT EXISTS chongxi_product DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE chongxi_product;

-- 每行对应种类、品种、年龄阶段、需求、品牌、产品的唯一组合，保留全部推荐来源。
CREATE TABLE IF NOT EXISTS product_recommendation (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  recommendation_key CHAR(64) NOT NULL COMMENT '六维组合的唯一哈希',
  product_key CHAR(64) NOT NULL COMMENT '物种、规范品牌及产品名哈希',
  species_code VARCHAR(32) NOT NULL,
  breed_code VARCHAR(64) NOT NULL,
  life_stage_code VARCHAR(32) NOT NULL,
  need_code VARCHAR(80) NOT NULL,
  brand VARCHAR(160) NOT NULL,
  product_name VARCHAR(300) NOT NULL,
  product_description TEXT NULL,
  recommendation_reason TEXT NULL COMMENT '不同来源的理由去重并列，不生成新的产品结论',
  relation_types JSON NOT NULL COMMENT '保留明确产品适配与提问场景关系的区别',
  verification_status ENUM('pending','verified','rejected') NOT NULL DEFAULT 'pending',
  review_status ENUM('pending','verified','rejected') NOT NULL DEFAULT 'pending',
  source_count INT UNSIGNED NOT NULL,
  model_count INT UNSIGNED NOT NULL,
  sources JSON NOT NULL COMMENT '全部来源、模型、问题、推荐理由、原始条目及证据状态',
  input_hash CHAR(64) NOT NULL,
  raw_json JSON NOT NULL COMMENT '可重建的标准化来源记录',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_recommendation (recommendation_key),
  KEY idx_product (product_key),
  KEY idx_query (species_code, breed_code, life_stage_code, need_code),
  KEY idx_brand_product (brand, product_name(100))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
