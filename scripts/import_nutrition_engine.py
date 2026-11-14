"""Import the nutrition engine workbook as versioned, traceable draft knowledge."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.import_knowledge_dictionary import read_workbook

SHEET='营养健康映射规则'
HEADERS={'规则ID':'rule_code','指标编码':'metric_code','指标名称':'metric_name','变化方向':'change_direction',
 '作用方向编码':'effect_code','营养作用方向':'effect_name','小程序推荐话术':'recommendation_text',
 '主人可以关注什么':'owner_observation','证据等级':'evidence_grade','关联健康问题':'related_health_issues',
 '来源URL':'source_url','审核状态':'source_review_status'}
METRIC_POLARITIES={
 'protein_quality':'beneficial','fermentable_fiber_support':'beneficial',
 'low_fermentable_fiber_support':'beneficial','prebiotic_support':'beneficial',
 'fiber_support':'beneficial','antioxidant_support':'beneficial',
 'protein_pressure':'harmful','fat_pressure':'harmful','carb_burden':'harmful'}
def rule_indicator_type(metric_code,change_direction):
 polarity=METRIC_POLARITIES.get(metric_code)
 if polarity is None or change_direction not in ('increase','decrease'):return None
 favourable=(polarity=='beneficial' and change_direction=='increase') or (polarity=='harmful' and change_direction=='decrease')
 return 'beneficial' if favourable else 'harmful'


BINDINGS={
 'protein_quality':('PROTEIN_QUALITY','protein_quality'),
 'protein_pressure':('PROTEIN_PRESSURE','protein'),
 'fat_pressure':('FAT_BURDEN','fat'),
 'carb_burden':('CARB_BURDEN','carbohydrate'),
 'fiber_support':('FIBER_SUPPORT','fiber'),
 'prebiotic_support':('PREBIOTIC_SUPPORT','prebiotic')}


def load(path):
 data=read_workbook(path)
 if set(data)!={SHEET}:raise ValueError('工作表与预期不符')
 rows=data[SHEET];seen=set();result=[]
 for n,raw in enumerate(rows,2):
  if set(raw)!=set(HEADERS):raise ValueError('列名与预期不符')
  row={v:raw[k] for k,v in HEADERS.items()}
  if not re.fullmatch(r'NER-\d+',row['rule_code']) or row['rule_code'] in seen:raise ValueError('规则编号无效或重复')
  seen.add(row['rule_code'])
  for k in ('metric_code','metric_name','effect_code','effect_name','recommendation_text','owner_observation'):
   if not row[k].strip():raise ValueError('必填字段为空：'+k)
  if row['change_direction'] not in ('increase','decrease','change'):raise ValueError('变化方向无效')
  if row['evidence_grade'] not in ('supported','conditional','limited','insufficient'):raise ValueError('证据等级无效')
  if row['source_review_status'] not in ('','待审核'):raise ValueError('未识别的审核状态')
  if row['source_url'] and not row['source_url'].startswith(('http://','https://')):raise ValueError('来源链接无效')
  result.append((n,raw,row))
 if not result:raise ValueError('工作表为空')
 return result


def ensure_tables(cur):
 from services.nutrition_mechanism_kb_service import KNOWLEDGE_DDL
 for statement in KNOWLEDGE_DDL:
  if any(('CREATE TABLE IF NOT EXISTS '+name+' (') in statement for name in ('nutrition_kb_import_batch','nutrition_kb_staging_row')):
   cur.execute(statement)
 cur.execute('''CREATE TABLE IF NOT EXISTS nutrition_metric_binding (
  knowledge_version VARCHAR(32) NOT NULL, metric_code VARCHAR(96) NOT NULL,
  metric_name VARCHAR(255) NOT NULL,canonical_factor_code VARCHAR(96) NULL,
  metric_kind VARCHAR(64) NOT NULL,source_table VARCHAR(255) NULL,source_field VARCHAR(255) NULL,
  binding_status VARCHAR(32) NOT NULL,binding_note TEXT NOT NULL,
  PRIMARY KEY(knowledge_version,metric_code)
 ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
 cur.execute('''CREATE TABLE IF NOT EXISTS nutrition_effect_rule (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  knowledge_version VARCHAR(32) NOT NULL,rule_code VARCHAR(32) NOT NULL,
  metric_code VARCHAR(96) NOT NULL,metric_name VARCHAR(255) NOT NULL,
  indicator_type ENUM('beneficial','harmful') NULL COMMENT '规则变化评价：正向增加或负担降低为有利，反向为不利，空为待判定',
  change_direction VARCHAR(32) NOT NULL,effect_code VARCHAR(96) NOT NULL,effect_name VARCHAR(255) NOT NULL,
  recommendation_text TEXT NOT NULL,owner_observation TEXT NOT NULL,evidence_grade VARCHAR(32) NOT NULL,
  related_health_issues TEXT NOT NULL,source_url TEXT NOT NULL,source_review_status VARCHAR(32) NOT NULL,
  review_status VARCHAR(32) NOT NULL DEFAULT 'pending',status VARCHAR(32) NOT NULL DEFAULT 'draft',
  source_batch_id VARCHAR(64) NOT NULL,source_sheet_name VARCHAR(255) NOT NULL,source_row_number INT NOT NULL,
  source_sha256 CHAR(64) NOT NULL,
  UNIQUE KEY uq_nutrition_rule(knowledge_version,rule_code),
  CONSTRAINT fk_nutrition_effect_metric FOREIGN KEY(knowledge_version,metric_code)
   REFERENCES nutrition_metric_binding(knowledge_version,metric_code)
 ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
 cur.execute("SHOW COLUMNS FROM nutrition_effect_rule LIKE 'indicator_type'")
 if not cur.fetchone():
  cur.execute("ALTER TABLE nutrition_effect_rule ADD COLUMN indicator_type ENUM('beneficial','harmful') NULL COMMENT '规则变化评价：有利/不利，空为待判定' AFTER metric_name")


def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('workbook',type=Path)
 ap.add_argument('--version',default='nutrition-engine-1110-v1');ap.add_argument('--dry-run',action='store_true')
 args=ap.parse_args();rows=load(args.workbook)
 if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',args.version):raise ValueError('版本名称无效')
 metrics={r['metric_code']:r['metric_name'] for _,_,r in rows}
 if args.dry_run:
  print(json.dumps({'validated':True,'rules':len(rows),'metrics':len(metrics),'version':args.version},ensure_ascii=False));return
 import pymysql
 from app_config import get_mysql_config
 from services.model_knowledge_import_service import database_name
 cfg=get_mysql_config(database=database_name());cfg['cursorclass']=pymysql.cursors.DictCursor
 sha=hashlib.sha256(args.workbook.read_bytes()).hexdigest();batch='nutrition-engine-'+sha[:24]
 with pymysql.connect(**cfg,autocommit=False) as conn:
  with conn.cursor() as c:
   ensure_tables(c)
   for code,name in metrics.items():
    canonical,key=BINDINGS.get(code,(None,None))
    note='同版本两款配方评分之差；评分不是营养含量或临床效果。' if canonical else '尚未建立可靠取数映射；不得用六维总分替代专项指标。'
    c.execute('''INSERT INTO nutrition_metric_binding
     (knowledge_version,metric_code,metric_name,canonical_factor_code,metric_kind,source_table,source_field,binding_status,binding_note)
     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE metric_name=VALUES(metric_name)''',
     (args.version,code,name,canonical,'structural_score' if canonical else 'unresolved',
      'csv_labeling.formula_calculation_run' if canonical else None,
      'result_json $.portrait.dimensions.'+key+'.score' if canonical else None,
      'mapped' if canonical else 'unmapped',note))
   c.execute('''INSERT INTO nutrition_kb_import_batch
    (batch_id,source_filename,source_sha256,target_knowledge_version,import_status,total_rows,valid_rows,invalid_rows,imported_rows,created_by,created_at,completed_at)
    VALUES (%s,%s,%s,%s,'imported',%s,%s,0,%s,'codex:xlsx_import',NOW(),NOW())
    ON DUPLICATE KEY UPDATE completed_at=NOW(),import_status='imported' ''',
    (batch,args.workbook.name,sha,args.version,len(rows),len(rows),len(rows)))
   for n,raw,row in rows:
    record={**row,'indicator_type':rule_indicator_type(row['metric_code'],row['change_direction']),'knowledge_version':args.version,'review_status':'pending','status':'draft',
      'source_batch_id':batch,'source_sheet_name':SHEET,'source_row_number':n,'source_sha256':sha}
    cols=list(record);query='INSERT INTO nutrition_effect_rule ('+','.join(cols)+') VALUES ('+','.join(['%s']*len(cols))+') ON DUPLICATE KEY UPDATE '+','.join(f'{k}=VALUES({k})' for k in cols if k not in ('knowledge_version','rule_code'))
    c.execute(query,tuple(record[k] for k in cols))
    c.execute('SELECT * FROM nutrition_effect_rule WHERE knowledge_version=%s AND rule_code=%s',(args.version,row['rule_code']))
    saved=c.fetchone();assert all(saved[k]==v for k,v in record.items()),'规则逐字段校验失败'
    c.execute('''INSERT INTO nutrition_kb_staging_row
     (batch_id,sheet_name,source_row_number,entity_type,entity_key,raw_row_json,normalized_row_json,validation_status,validation_errors_json,target_table,target_id,created_at,updated_at)
     VALUES (%s,%s,%s,'nutrition_effect_rule',%s,%s,%s,'valid',JSON_ARRAY(),'nutrition_effect_rule',%s,NOW(),NOW())
     ON DUPLICATE KEY UPDATE raw_row_json=VALUES(raw_row_json),normalized_row_json=VALUES(normalized_row_json),target_id=VALUES(target_id),updated_at=NOW()''',
     (batch,SHEET,n,args.version+':'+row['rule_code'],json.dumps(raw,ensure_ascii=False),json.dumps(record,ensure_ascii=False),saved['id']))
  conn.commit()
 print(json.dumps({'database':cfg['database'],'table':'nutrition_effect_rule','version':args.version,'rules':len(rows),
  'metric_bindings':len(metrics),'mapped_metrics':sum(k in BINDINGS for k in metrics),'review_status':'pending','batch_id':batch},ensure_ascii=False))


if __name__=='__main__':main()
