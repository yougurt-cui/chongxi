-- Target database is selected by the project connection; sample INSERTs are not executed automatically.
CREATE TABLE IF NOT EXISTS health_solution (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '内部主键',
  species VARCHAR(32) NOT NULL COMMENT '种类',
  breed VARCHAR(64) NOT NULL DEFAULT 'all' COMMENT '品种',
  life_stage VARCHAR(32) NOT NULL DEFAULT 'all' COMMENT '生命阶段',
  need VARCHAR(64) NOT NULL COMMENT '需求，沿用原始数据编码',
  scenario_code VARCHAR(100) NOT NULL COMMENT '标准化场景编码，适用于唯一键',
  scenario TEXT NOT NULL COMMENT '观测场景，保留原文',
  possible_causes JSON NOT NULL COMMENT '可能原因数组',
  solution JSON NOT NULL COMMENT '具体解决动作数组',
  verification JSON NOT NULL COMMENT '包含review_condition、expected_result、failure_condition、next_action',
  review_status VARCHAR(32) NOT NULL DEFAULT 'pending_review' COMMENT '待审核、通过、退回等',
  sources JSON NULL COMMENT '来源模型、批次、原始场景方案及原文变体',
  source_count INT UNSIGNED NOT NULL DEFAULT 0,
  model_count INT UNSIGNED NOT NULL DEFAULT 0,
  input_hash CHAR(64) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_solution_grain (species, breed, life_stage, need, scenario_code),
  KEY idx_solution_scope (species, breed, life_stage, need),
  KEY idx_review_status (review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='场景级健康解决方案';
