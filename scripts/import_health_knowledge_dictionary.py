"""Import health enum workbook into separate dictionary tables, preserving existing dictionaries."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.import_knowledge_dictionary import read_workbook, DATABASE

SPECS = [
 ('01_健康分类枚举','dict_health_category','category_code',
  {'分类编码':'category_code','分类名称':'category_name','英文Key':'english_key','定义/覆盖范围':'description','排序':'sort_order','启用状态':'status'}),
 ('02_健康问题枚举','dict_health_issue','issue_code',
  {'问题编码':'issue_code','问题名称':'issue_name','所属分类编码':'category_code','所属分类':'category_name','同义表达（逗号分隔）':'aliases','知识卡默认标题':'default_card_title','是否高风险提示':'high_risk_warning','启用状态':'status'}),
 ('03_知识点类型枚举','dict_health_knowledge_point_type','type_code',
  {'类型编码':'type_code','知识类型':'type_name','定义':'description','示例':'example','排序':'sort_order'}),
]


def prepare(path):
 data=read_workbook(path)
 assert set(data)=={s[0] for s in SPECS}, 'Unexpected workbook sheets'
 result={}
 for sheet,table,pk,columns in SPECS:
  rows=[];seen=set()
  for raw in data[sheet]:
   assert set(raw)==set(columns), 'Unexpected columns in '+sheet
   row={field:raw[label] for label,field in columns.items()}
   assert row[pk] and row[pk] not in seen, 'Empty or duplicate code'
   seen.add(row[pk])
   if 'sort_order' in row:row['sort_order']=int(row['sort_order'])
   if 'high_risk_warning' in row:
    assert row['high_risk_warning'] in ('是','否')
    row['high_risk_warning']=int(row['high_risk_warning']=='是')
   if 'status' in row:assert row['status'] in ('enabled','disabled')
   rows.append(row)
  assert rows
  result[table]=rows
 categories={r['category_code']:r['category_name'] for r in result['dict_health_category']}
 for row in result['dict_health_issue']:
  assert categories.get(row['category_code'])==row['category_name'], 'Category mismatch'
 return result


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('workbook');parser.add_argument('--dry-run',action='store_true')
 args=parser.parse_args();data=prepare(args.workbook)
 if not args.dry_run:
  import pymysql
  from app_config import get_mysql_config
  cfg=get_mysql_config(database=DATABASE)
  with pymysql.connect(**cfg,autocommit=False) as conn:
   with conn.cursor() as cur:
    for _,table,pk,_ in SPECS:
     definitions=[]
     for col in data[table][0]:
      kind='VARCHAR(255)' if col.endswith('_code') or col in ('english_key','status') else 'TEXT'
      if col=='sort_order':kind='INT'
      if col=='high_risk_warning':kind='TINYINT(1)'
      definitions.append(f'`{col}` {kind} NOT NULL')
     definitions.append(f'PRIMARY KEY (`{pk}`)')
     if table=='dict_health_issue':
      definitions.append('CONSTRAINT fk_health_issue_category FOREIGN KEY (category_code) REFERENCES dict_health_category(category_code)')
     cur.execute(f'CREATE TABLE IF NOT EXISTS `{table}` ('+','.join(definitions)+') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin')
    for _,table,pk,_ in SPECS:
     cols=list(data[table][0]);fields=','.join('`'+c+'`' for c in cols)
     query=f'INSERT INTO `{table}` ({fields}) VALUES ('+','.join(['%s']*len(cols))+') ON DUPLICATE KEY UPDATE '+','.join(f'`{c}`=VALUES(`{c}`)' for c in cols if c!=pk)
     cur.executemany(query,[tuple(r[c] for c in cols) for r in data[table]])
     for row in data[table]:
      cur.execute(f'SELECT {fields} FROM `{table}` WHERE `{pk}`=%s',(row[pk],))
      assert cur.fetchone()==tuple(row[c] for c in cols), 'Stored data mismatch'
   conn.commit()
 print(json.dumps({'database':DATABASE,'dry_run':args.dry_run,'tables':{t:len(r) for t,r in data.items()},'total':sum(map(len,data.values()))},ensure_ascii=False))


if __name__=='__main__':main()
