CREATE DATABASE IF NOT EXISTS chongxi_product DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE chongxi_product;

-- 每行对应一款产品在一个原始来源及知识场景中的推荐，多个推荐条件保存在 JSON 数组中。
CREATE TABLE IF NOT EXISTS product_recommendation (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  product_key CHAR(64) NOT NULL COMMENT '物种+品牌+产品名的身份哈希，不同来源允许同一产品重复',
  species_code VARCHAR(32) NOT NULL,
  brand VARCHAR(160) NOT NULL,
  product_name VARCHAR(300) NOT NULL,
  product_description TEXT NULL,
  recommendations JSON NOT NULL COMMENT '品种、阶段、需求、推荐理由、关系类型及证据状态数组',
  verification_status ENUM('pending','verified','rejected') NOT NULL DEFAULT 'pending',
  review_status ENUM('pending','verified','rejected') NOT NULL DEFAULT 'pending',
  original_source_id BIGINT NOT NULL,
  knowledge_source_id BIGINT NULL,
  source_key CHAR(64) NOT NULL COMMENT '知识对象+原始来源+产品的唯一标识',
  input_hash CHAR(64) NOT NULL,
  batch_id BIGINT NULL,
  source_model VARCHAR(150) NULL COMMENT '推荐来源模型',
  source_provider VARCHAR(80) NULL COMMENT '推荐来源提供方',
  source_question VARCHAR(500) NULL COMMENT '产生推荐的原始提问',
  source_evidence_status VARCHAR(40) NULL,
  raw_json JSON NOT NULL COMMENT '完整标准化记录及原始推荐来源',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_source (source_key),
  KEY idx_product (product_key),
  KEY idx_knowledge_source (knowledge_source_id),
  KEY idx_original_source (original_source_id),
  KEY idx_brand_product (brand, product_name(100))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
