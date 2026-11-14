"""Back up, remove notes/topic, and put normalized mapping columns first."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store


def migrate(backup_dir):
    backup=Path(backup_dir);backup.mkdir(parents=True,exist_ok=False)
    with store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM knowledge_import_batch WHERE status='running'")
            if cur.fetchone()['n']:raise RuntimeError('请先等待采集任务结束')
            payload={'database':store.database_name(),'tables':{}}
            for table in ('knowledge_import_batch','knowledge_point_source'):
                cur.execute(f'SHOW CREATE TABLE `{table}`');schema=cur.fetchone()
                cur.execute(f'SELECT * FROM `{table}` ORDER BY id');rows=cur.fetchall()
                payload['tables'][table]={'schema':schema,'rows':rows}
            (backup/'data.json').write_text(json.dumps(payload,ensure_ascii=False,default=str))
            cur.execute('''ALTER TABLE knowledge_point_source
                DROP COLUMN notes, DROP COLUMN topic,
                MODIFY original_condition TEXT NOT NULL AFTER batch_id,
                MODIFY species_code VARCHAR(32) NULL AFTER original_condition,
                MODIFY breed_code VARCHAR(255) NULL AFTER species_code,
                MODIFY life_stage_codes JSON NOT NULL AFTER breed_code,
                MODIFY need_codes JSON NOT NULL AFTER life_stage_codes,
                MODIFY category_code VARCHAR(255) NULL AFTER need_codes,
                MODIFY topic_code VARCHAR(255) NULL AFTER category_code,
                MODIFY content TEXT NOT NULL AFTER topic_code,
                MODIFY standardization_meta JSON NULL AFTER content''')
            cur.execute('SELECT * FROM knowledge_point_source ORDER BY id');new=cur.fetchall()
            expected=[{k:v for k,v in r.items() if k not in ('notes','topic')} for r in payload['tables']['knowledge_point_source']['rows']]
            assert new==expected,'保留列数据核验失败'
        conn.commit()
    return {'backup':str(backup),'rows':len(new),'all_retained_fields_unchanged':True}


if __name__=='__main__':print(json.dumps(migrate(sys.argv[1]),ensure_ascii=False))
