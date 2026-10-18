"""Schema initialization for the versioned nutrition-mechanism knowledge base."""

from __future__ import annotations

from typing import Any

import pymysql

from app_config import get_feature_mysql_config, get_mysql_config


KNOWLEDGE_TABLES = (
    "nutrition_knowledge_release",
    "nutrition_evidence_source",
    "nutrition_health_concern",
    "nutrition_mechanism",
    "nutrition_concern_mechanism",
    "nutrition_mechanism_rule",
    "nutrition_mechanism_action",
    "nutrition_kb_import_batch",
    "nutrition_kb_staging_row",
    "nutrition_kb_change_log",
)
USER_ASSESSMENT_TABLE = "user_food_mechanism_assessment"


def _connect_feature():
    return pymysql.connect(
        **get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor
    )


def _connect_business():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor
    )


KNOWLEDGE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS nutrition_knowledge_release (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      knowledge_version VARCHAR(32) NOT NULL,
      title VARCHAR(255) NOT NULL,
      description TEXT NULL,
      status VARCHAR(16) NOT NULL DEFAULT 'draft',
      published_by VARCHAR(128) NULL,
      published_at DATETIME NULL,
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_nutrition_release_version (knowledge_version),
      KEY idx_nutrition_release_status (status, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS nutrition_evidence_source (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      source_code VARCHAR(64) NOT NULL,
      source_type VARCHAR(32) NOT NULL,
      title VARCHAR(512) NOT NULL,
      organization VARCHAR(255) NULL,
      authors TEXT NULL,
      publication_year SMALLINT UNSIGNED NULL,
      url_or_doi VARCHAR(1000) NULL,
      citation_text TEXT NULL,
      evidence_grade VARCHAR(16) NULL,
      applicable_species VARCHAR(32) NULL,
      review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
      reviewed_by VARCHAR(128) NULL,
      reviewed_at DATETIME NULL,
      status VARCHAR(16) NOT NULL DEFAULT 'active',
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_nutrition_evidence_code (source_code),
      KEY idx_nutrition_evidence_review (review_status, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS nutrition_health_concern (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      concern_code VARCHAR(64) NOT NULL,
      concern_name VARCHAR(255) NOT NULL,
      concern_type VARCHAR(32) NOT NULL,
      species VARCHAR(32) NOT NULL DEFAULT 'cat',
      parent_id BIGINT UNSIGNED NULL,
      description TEXT NULL,
      urgent_level VARCHAR(16) NOT NULL DEFAULT 'routine',
      knowledge_version VARCHAR(32) NOT NULL,
      review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
      status VARCHAR(16) NOT NULL DEFAULT 'active',
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_nutrition_concern_version (concern_code, knowledge_version),
      KEY idx_nutrition_concern_lookup (species, concern_type, status),
      KEY idx_nutrition_concern_parent (parent_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS nutrition_mechanism (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      mechanism_code VARCHAR(64) NOT NULL,
      mechanism_name VARCHAR(255) NOT NULL,
      mechanism_category VARCHAR(64) NOT NULL,
      mechanism_description TEXT NOT NULL,
      causal_strength VARCHAR(32) NOT NULL DEFAULT 'plausible',
      applicable_species VARCHAR(32) NOT NULL DEFAULT 'cat',
      life_stage_scope VARCHAR(64) NULL,
      knowledge_version VARCHAR(32) NOT NULL,
      review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
      status VARCHAR(16) NOT NULL DEFAULT 'active',
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_nutrition_mechanism_version (mechanism_code, knowledge_version),
      KEY idx_nutrition_mechanism_lookup
        (mechanism_category, applicable_species, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS nutrition_concern_mechanism (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      concern_id BIGINT UNSIGNED NOT NULL,
      mechanism_id BIGINT UNSIGNED NOT NULL,
      relation_type VARCHAR(32) NOT NULL,
      direction VARCHAR(16) NOT NULL DEFAULT 'increase',
      base_weight DECIMAL(8,4) NULL,
      evidence_grade VARCHAR(16) NULL,
      population_scope VARCHAR(255) NULL,
      prerequisite_json JSON NULL,
      exclusion_json JSON NULL,
      source_id BIGINT UNSIGNED NULL,
      knowledge_version VARCHAR(32) NOT NULL,
      review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
      status VARCHAR(16) NOT NULL DEFAULT 'active',
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_nutrition_concern_mechanism
        (concern_id, mechanism_id, relation_type, knowledge_version),
      KEY idx_nutrition_relation_mechanism (mechanism_id, status),
      KEY idx_nutrition_relation_concern (concern_id, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS nutrition_mechanism_rule (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      rule_code VARCHAR(64) NOT NULL,
      mechanism_id BIGINT UNSIGNED NOT NULL,
      rule_role VARCHAR(16) NOT NULL DEFAULT 'supporting',
      feature_scope VARCHAR(32) NOT NULL,
      feature_code VARCHAR(128) NOT NULL,
      comparison_operator VARCHAR(16) NOT NULL,
      threshold_value DECIMAL(16,6) NULL,
      threshold_text VARCHAR(255) NULL,
      threshold_unit VARCHAR(32) NULL,
      weight DECIMAL(8,4) NOT NULL,
      required TINYINT(1) NOT NULL DEFAULT 0,
      position_max INT NULL,
      condition_json JSON NULL,
      explanation_template TEXT NULL,
      evidence_grade VARCHAR(16) NULL,
      source_id BIGINT UNSIGNED NULL,
      knowledge_version VARCHAR(32) NOT NULL,
      review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
      status VARCHAR(16) NOT NULL DEFAULT 'active',
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_nutrition_rule_version (rule_code, knowledge_version),
      KEY idx_nutrition_rule_engine
        (mechanism_id, feature_scope, feature_code, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS nutrition_mechanism_action (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      action_code VARCHAR(64) NOT NULL,
      mechanism_id BIGINT UNSIGNED NOT NULL,
      action_type VARCHAR(32) NOT NULL,
      action_text TEXT NOT NULL,
      priority INT NOT NULL DEFAULT 100,
      prerequisite_json JSON NULL,
      contraindication_json JSON NULL,
      evidence_grade VARCHAR(16) NULL,
      source_id BIGINT UNSIGNED NULL,
      knowledge_version VARCHAR(32) NOT NULL,
      review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
      status VARCHAR(16) NOT NULL DEFAULT 'active',
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_nutrition_action_version (action_code, knowledge_version),
      KEY idx_nutrition_action_lookup (mechanism_id, action_type, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS nutrition_kb_import_batch (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      batch_id VARCHAR(64) NOT NULL,
      source_filename VARCHAR(512) NOT NULL,
      source_sha256 CHAR(64) NOT NULL,
      target_knowledge_version VARCHAR(32) NOT NULL,
      import_status VARCHAR(32) NOT NULL DEFAULT 'staged',
      total_rows INT NOT NULL DEFAULT 0,
      valid_rows INT NOT NULL DEFAULT 0,
      invalid_rows INT NOT NULL DEFAULT 0,
      imported_rows INT NOT NULL DEFAULT 0,
      error_summary_json JSON NULL,
      created_by VARCHAR(128) NULL,
      created_at DATETIME NOT NULL,
      completed_at DATETIME NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_nutrition_import_batch (batch_id),
      KEY idx_nutrition_import_file (source_sha256, target_knowledge_version),
      KEY idx_nutrition_import_status (import_status, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS nutrition_kb_staging_row (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      batch_id VARCHAR(64) NOT NULL,
      sheet_name VARCHAR(255) NOT NULL,
      source_row_number INT NOT NULL,
      entity_type VARCHAR(64) NULL,
      entity_key VARCHAR(255) NULL,
      raw_row_json JSON NOT NULL,
      normalized_row_json JSON NULL,
      validation_status VARCHAR(32) NOT NULL DEFAULT 'pending',
      validation_errors_json JSON NULL,
      target_table VARCHAR(128) NULL,
      target_id BIGINT UNSIGNED NULL,
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_nutrition_staging_source
        (batch_id, sheet_name, source_row_number),
      KEY idx_nutrition_staging_validation (batch_id, validation_status),
      KEY idx_nutrition_staging_entity (entity_type, entity_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS nutrition_kb_change_log (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      batch_id VARCHAR(64) NULL,
      knowledge_version VARCHAR(32) NOT NULL,
      entity_type VARCHAR(64) NOT NULL,
      entity_id BIGINT UNSIGNED NULL,
      entity_key VARCHAR(255) NOT NULL,
      change_type VARCHAR(32) NOT NULL,
      before_json JSON NULL,
      after_json JSON NULL,
      changed_by VARCHAR(128) NULL,
      changed_at DATETIME NOT NULL,
      PRIMARY KEY (id),
      KEY idx_nutrition_change_entity (entity_type, entity_key, changed_at),
      KEY idx_nutrition_change_batch (batch_id, changed_at),
      KEY idx_nutrition_change_version (knowledge_version, changed_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)


USER_ASSESSMENT_DDL = """
CREATE TABLE IF NOT EXISTS user_food_mechanism_assessment (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  assessment_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(128) NOT NULL,
  pet_id VARCHAR(64) NULL,
  conversation_id VARCHAR(64) NULL,
  formula_id BIGINT UNSIGNED NOT NULL,
  formula_version INT NULL,
  knowledge_version VARCHAR(32) NOT NULL,
  symptom_snapshot_json JSON NULL,
  diet_context_json JSON NULL,
  matched_mechanisms_json JSON NOT NULL,
  health_concerns_json JSON NULL,
  adjustment_directions_json JSON NULL,
  avoidance_directions_json JSON NULL,
  evidence_limits_json JSON NULL,
  risk_level VARCHAR(16) NOT NULL DEFAULT 'low',
  model_name VARCHAR(128) NULL,
  rule_version VARCHAR(32) NOT NULL,
  source_data_hash CHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_mechanism_assessment (assessment_id),
  KEY idx_user_mechanism_history (user_id, pet_id, created_at),
  KEY idx_formula_mechanism_assessment (formula_id, created_at),
  KEY idx_conversation_mechanism_assessment (conversation_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def _apply(conn: Any, statements: tuple[str, ...]) -> None:
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    conn.commit()


def initialize_nutrition_mechanism_tables(
    *, include_user_assessment: bool = True,
) -> dict[str, Any]:
    """Create empty, idempotent schemas; never import or publish knowledge rows."""
    with _connect_feature() as conn:
        _apply(conn, KNOWLEDGE_DDL)
    tables = list(KNOWLEDGE_TABLES)
    if include_user_assessment:
        with _connect_business() as conn:
            _apply(conn, (USER_ASSESSMENT_DDL,))
        tables.append(USER_ASSESSMENT_TABLE)
    return {
        "ok": True,
        "knowledge_database": get_feature_mysql_config().get("database"),
        "business_database": get_mysql_config().get("database") if include_user_assessment else None,
        "tables": tables,
        "excluded_tables": ["catfood_formula_mechanism_profile"],
    }
