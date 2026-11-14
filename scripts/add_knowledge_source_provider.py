"""Back up then populate the original model provider from each source batch."""
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
            cur.execute('ALTER TABLE knowledge_point_source ADD COLUMN source_provider VARCHAR(32) NULL AFTER batch_id')
            cur.execute('UPDATE knowledge_point_source s JOIN knowledge_import_batch b ON b.id=s.batch_id SET s.source_provider=b.provider')
        conn.commit()
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) n FROM knowledge_point_source WHERE source_provider IS NULL');assert cur.fetchone()['n']==0
            cur.execute('ALTER TABLE knowledge_point_source MODIFY source_provider VARCHAR(32) NOT NULL AFTER batch_id')
            cur.execute('SELECT * FROM knowledge_point_source ORDER BY id');new=cur.fetchall()
            assert [{k:v for k,v in r.items() if k!='source_provider'} for r in new]==payload['tables']['knowledge_point_source']['rows']
        conn.commit()
    return {'rows':len(new),'backup':str(backup),'retained_data_unchanged':True}


if __name__=='__main__':print(json.dumps(migrate(sys.argv[1]),ensure_ascii=False))
