-- Run once before deploying source-scoped candidate code. The legacy index
-- conflates same-name products with different formulas. MySQL DDL auto-commits.
ALTER TABLE catfood_standard_product_candidate
  ADD COLUMN candidate_source_id BIGINT NULL,
  DROP INDEX uq_brand_product,
  ADD UNIQUE KEY uq_candidate_source (candidate_source_id),
  ADD KEY idx_candidate_brand_name (brand_id, standard_product_name);
