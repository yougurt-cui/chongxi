SET NAMES utf8mb4;
-- 宠析健康解决方案库：以 species + breed + life_stage + need + scenario_code 唯一定位一条场景方案
-- 原始字段保持原文，仅新增 scenario_code 与审核状态；导入后需审核医疗建议
-- MySQL 8.0+ / UTF8MB4
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
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_solution_grain (species, breed, life_stage, need, scenario_code),
  KEY idx_solution_scope (species, breed, life_stage, need),
  KEY idx_review_status (review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='场景级健康解决方案';


INSERT INTO health_solution
  (species, breed, life_stage, need, scenario_code, scenario, possible_causes, solution, verification)
VALUES
  ('cat', 'all', 'all', 'digestive_health', 'stool_odor_normal_stool', '猫咪排便气味明显偏臭，但粪便成形、软硬正常，排便频率每日1-2次，无呕吐、无食欲下降，仅主人主观感觉气味比平时重，持续数天。', CAST('["饮食中蛋白质或脂肪含量较高", "突然换粮或进食不规律", "肠道菌群暂时性波动", "饮水不足导致粪便浓缩", "粪便在猫砂盆中停留时间过长"]' AS JSON), CAST('["记录当前主食品牌、喂食量和喂食时间，观察粪便气味与饮食的关联", "保持猫砂盆每日至少清理1-2次，减少气味蓄积造成的误判", "确保猫咪每日有充足饮水，可增加湿粮或流动水源", "不立即更换主食，先维持现有饮食并观察3-5天", "若气味持续加重或伴随其他异常，再考虑进一步排查"]' AS JSON), CAST('{"review_condition": "维持现有饮食和清理频率3-5天后重新评估粪便气味变化", "expected_result": "猫砂盆清理后气味明显减轻，粪便成形且无其他异常表现", "failure_condition": "气味持续加重，或出现软便、腹泻、呕吐、食欲下降等新症状", "next_action": "记录异常表现并咨询兽医，必要时进行粪便检查"}' AS JSON))
ON DUPLICATE KEY UPDATE id=id;

INSERT INTO health_solution
  (species, breed, life_stage, need, scenario_code, scenario, possible_causes, solution, verification)
VALUES
  ('cat', 'all', 'all', 'digestive_health', 'stool_odor_soft_stool', '猫咪便便臭味明显且粪便偏软、不成形，排便次数增至每日3次以上，无血便、无呕吐，精神和食欲尚可，持续1-2天。', CAST('["饮食突然改变或进食了不易消化的食物", "肠道菌群失衡", "食物不耐受", "寄生虫感染", "应激反应导致消化功能紊乱"]' AS JSON), CAST('["暂停零食和任何新引入的食物，只保留平时吃过的主食", "记录排便次数、粪便性状和饮食内容", "提供充足饮水，观察是否有脱水表现", "不自行使用止泻药或抗生素", "若软便持续超过24-48小时或出现呕吐、精神变差，及时就医并携带新鲜粪便样本"]' AS JSON), CAST('{"review_condition": "调整饮食并观察24-48小时后重新评估粪便性状和排便次数", "expected_result": "粪便逐渐成形，排便次数恢复至每日1-2次，臭味减轻", "failure_condition": "软便持续不改善、出现腹泻、血便、呕吐、食欲下降或精神萎靡", "next_action": "尽快就医，进行粪便检查和必要的消化系统评估"}' AS JSON))
ON DUPLICATE KEY UPDATE id=id;

INSERT INTO health_solution
  (species, breed, life_stage, need, scenario_code, scenario, possible_causes, solution, verification)
VALUES
  ('cat', 'all', 'all', 'digestive_health', 'stool_odor_gas_gagging', '猫咪便便恶臭并伴有频繁排气、腹部胀气，粪便时干时软，进食后偶有干呕，持续一周左右，体重无明显变化。', CAST('["饮食结构不合理或进食过快", "食物不耐受", "肠道菌群紊乱", "毛球或吞入异物刺激", "慢性消化道问题早期表现"]' AS JSON), CAST('["记录每日进食量、排便性状和排气频率，形成简单观察日志", "少量多餐，避免一次进食过多过快", "增加梳毛频率，减少毛球摄入", "不自行更换主食或添加保健品，先观察并记录", "若胀气、干呕持续一周以上或体重下降，安排兽医检查"]' AS JSON), CAST('{"review_condition": "调整喂食方式并记录观察日志7天后重新评估", "expected_result": "排气和腹胀减少，粪便性状趋于稳定，干呕次数下降", "failure_condition": "症状持续或加重，出现体重下降、呕吐、食欲减退", "next_action": "就医进行消化道检查，包括粪便检查和必要的影像学评估"}' AS JSON))
ON DUPLICATE KEY UPDATE id=id;

INSERT INTO health_solution
  (species, breed, life_stage, need, scenario_code, scenario, possible_causes, solution, verification)
VALUES
  ('cat', 'all', 'senior', 'digestive_health', 'stool_odor_senior_constipation', '老年猫便便气味明显加重，粪便偏干，排便间隔延长至2天以上，偶有排便费力表现，饮水量偏少，精神和食欲基本正常。', CAST('["饮水不足导致粪便浓缩", "老年猫肠道蠕动减弱", "饮食中纤维或水分不足", "慢性脱水", "消化道功能退化"]' AS JSON), CAST('["增加湿粮比例或提供流动饮水，鼓励每日饮水", "记录排便间隔、粪便性状和每日饮水量", "保持猫砂盆清洁并放置在安静易达位置", "不自行使用通便药物或灌肠", "若排便间隔持续超过2天或出现排便痛苦表现，及时就医"]' AS JSON), CAST('{"review_condition": "增加水分摄入并观察5-7天后重新评估排便间隔和粪便性状", "expected_result": "排便间隔缩短至每日或隔日一次，粪便不再干硬，气味减轻", "failure_condition": "排便间隔继续延长、出现排便困难、呕吐、食欲下降或精神变差", "next_action": "就医评估是否存在便秘或脱水，必要时进行相关检查"}' AS JSON))
ON DUPLICATE KEY UPDATE id=id;

INSERT INTO health_solution
  (species, breed, life_stage, need, scenario_code, scenario, possible_causes, solution, verification)
VALUES
  ('cat', 'all', 'all', 'digestive_health', 'stool_odor_severe_diarrhea', '猫咪便便恶臭并伴随腹泻，粪便稀水样或带黏液，每日排便超过3次，同时出现食欲下降或精神萎靡，持续不超过24小时。', CAST('["急性胃肠炎", "食物中毒或误食变质食物", "寄生虫感染", "病毒或细菌感染", "严重应激反应"]' AS JSON), CAST('["优先就医，尤其是幼猫、老年猫或出现精神萎靡时", "保留新鲜粪便样本供兽医检查", "暂时禁食不超过12小时，期间保证饮水，具体是否禁食听从兽医建议", "不自行使用任何止泻药、抗生素或处方药物", "记录腹泻次数、性状、呕吐情况和饮水量，供兽医参考"]' AS JSON), CAST('{"review_condition": "就医后按兽医方案执行，24小时内重新评估精神状态和排便情况", "expected_result": "腹泻次数减少，精神状态和食欲恢复，粪便逐渐成形", "failure_condition": "腹泻持续加重、出现血便、频繁呕吐、明显脱水或精神极度萎靡", "next_action": "立即复诊或急诊处理，避免延误病情"}' AS JSON))
ON DUPLICATE KEY UPDATE id=id;

SELECT id,species,breed,life_stage,need,scenario_code,scenario,review_status FROM health_solution WHERE species='cat' AND need='digestive_health';
