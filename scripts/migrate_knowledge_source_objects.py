"""Back up and migrate flat source rows to the seven-column object table."""
import json
from datetime import datetime
from pathlib import Path
from services import model_knowledge_import_service as store
from services.knowledge_source_assembly_service import DDL, refresh


def main():
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute('SHOW COLUMNS FROM knowledge_point_source')
        columns = {r['Field'] for r in cur.fetchall()}
        if 'content' in columns:
            cur.execute("SELECT COUNT(*) n FROM knowledge_import_batch WHERE status='running'")
            if cur.fetchone()['n']:
                raise RuntimeError('采集任务尚未完成，暂不迁移')
            cur.execute('SELECT * FROM knowledge_point_source ORDER BY id')
            rows = list(cur.fetchall())
            cur.execute('SHOW CREATE TABLE knowledge_point_source')
            schema = cur.fetchone()
            folder = Path('/root/db_backups') / ('knowledge-source-objects-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
            folder.mkdir(parents=True,mode=0o700)
            backup=folder/'source.json'
            backup.write_text(json.dumps({'schema':schema,'rows':rows},ensure_ascii=False,default=str))
            backup.chmod(0o600)
            cur.execute('RENAME TABLE knowledge_point_source TO knowledge_point_source_detail')
            cur.execute(DDL)
            conn.commit()
            cur.execute('SELECT * FROM knowledge_point_source_detail ORDER BY id')
            assert list(cur.fetchall()) == rows, '原始明细校验失败'
            print('backup:',folder)
        else:
            cur.execute(DDL)
    print(json.dumps(refresh(),ensure_ascii=False))


if __name__ == '__main__':main()
