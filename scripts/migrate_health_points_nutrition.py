"""Preserve point IDs, inline health classification, restore nutrition links, empty legacy cards."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store
from vendor.chongxi_health_pipeline.task_parser import VERSION, sources_from_dimensions


def main():
    backup=Path(sys.argv[1])
    with store.connect() as conn:
        locks=[]
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) n FROM knowledge_import_batch WHERE knowledge_base='health' AND status='running'")
                assert cur.fetchone()['n']==0,'Health collection is running'
                cur.execute('SELECT DISTINCT batch_id FROM health_knowledge_point ORDER BY batch_id')
                batch_ids=[r['batch_id'] for r in cur.fetchall()]
                for bid in batch_ids:
                    name='chongxi:health-import:'+str(bid)
                    cur.execute('SELECT GET_LOCK(%s,5) acquired',(name,));assert cur.fetchone()['acquired']
                    locks.append(name)
                snapshot={}
                for table in ('health_knowledge_card','health_knowledge_point','health_knowledge_import'):
                    cur.execute('SHOW CREATE TABLE '+table);schema=cur.fetchone()
                    cur.execute('SELECT * FROM '+table+' ORDER BY '+('batch_id' if table=='health_knowledge_import' else 'id'))
                    snapshot[table]={'schema':schema,'rows':list(cur.fetchall())}
                parsed={}
                for bid in batch_ids:
                    cur.execute('SELECT parsed_response FROM knowledge_import_batch WHERE id=%s',(bid,))
                    parsed[bid]={p['source_id']:p for p in sources_from_dimensions(store.decode(cur.fetchone()['parsed_response']))}
                cards={c['id']:c for c in snapshot['health_knowledge_card']['rows']}
                for row in snapshot['health_knowledge_point']['rows']:
                    source=parsed[row['batch_id']][row['source_id']]
                    assert row['content']==source['content'],'Source text mismatch'
                    assert row['card_id'] in cards
                with backup.open('x') as f:
                    os.chmod(backup,0o600);json.dump(snapshot,f,ensure_ascii=False,default=str,indent=2)
                    f.flush();os.fsync(f.fileno())
                cur.execute('''ALTER TABLE health_knowledge_point
                    ADD COLUMN category_code VARCHAR(255) NULL,
                    ADD COLUMN problem_code VARCHAR(255) NULL,
                    ADD COLUMN problem_name TEXT NULL,
                    ADD COLUMN high_risk_warning TINYINT(1) NOT NULL DEFAULT 0,
                    ADD COLUMN nutrition_factors JSON NULL''')
                populated=0;factor_count=0
                for row in snapshot['health_knowledge_point']['rows']:
                    card=cards[row['card_id']];source=parsed[row['batch_id']][row['source_id']]
                    factors=source['nutrition_factors']
                    reasons=store.decode(row['review_reasons']) or []
                    if factors:
                        populated+=1;factor_count+=len(factors)
                        reasons.append('营养指标编码及关联按原模型保留，尚未通过营养指标枚举核验')
                    cur.execute('''UPDATE health_knowledge_point SET category_code=%s,problem_code=%s,problem_name=%s,
                        high_risk_warning=%s,nutrition_factors=%s,review_reasons=%s WHERE id=%s''',
                        (card['category_code'],card['problem_code'],card['problem_name'],card['high_risk_warning'],
                         store.dumps(factors),store.dumps(reasons),row['id']))
                conn.commit()
                cur.execute('SELECT * FROM health_knowledge_point ORDER BY id');after=list(cur.fetchall())
                assert len(after)==len(snapshot['health_knowledge_point']['rows'])
                for old,new in zip(snapshot['health_knowledge_point']['rows'],after):
                    assert all(new[k]==v for k,v in old.items() if k!='review_reasons'),'Existing point changed'
                    assert store.decode(new['nutrition_factors'])==parsed[new['batch_id']][new['source_id']]['nutrition_factors']
                cur.execute('''ALTER TABLE health_knowledge_point DROP FOREIGN KEY fk_health_point_card,
                    DROP COLUMN card_id, MODIFY COLUMN problem_name TEXT NOT NULL,
                    MODIFY COLUMN nutrition_factors JSON NOT NULL''')
                cur.execute('DELETE FROM health_knowledge_card')
                for bid in batch_ids:
                    cur.execute('UPDATE health_knowledge_import SET mapping_version=%s WHERE batch_id=%s',(VERSION,bid))
                conn.commit()
                print(json.dumps({'points_preserved':len(after),'points_with_nutrition':populated,
                    'nutrition_links':factor_count,'cards_removed':len(cards),'backup':str(backup)},ensure_ascii=False))
        finally:
            for name in locks:
                with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(name,))


if __name__=='__main__':main()
