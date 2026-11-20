"""Add experience term fields; backfill species without assigning unverified types."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import community_experience_service as service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with service.store.connect() as conn, conn.cursor() as cur:
        if args.apply:
            cur.execute("SELECT GET_LOCK(CONCAT('exp_topics:',LEFT(SHA2(DATABASE(),256),40)),10) AS acquired")
            if cur.fetchone()['acquired'] != 1:
                raise RuntimeError('experience topic refresh busy')
            try:
                service.ensure_term_classification_columns(cur)
                cur.execute("""UPDATE experience_search_term s JOIN experience_topic t USING(topic_key)
                    SET s.category=CASE JSON_UNQUOTE(JSON_EXTRACT(t.object_scope,'$[0]'))
                        WHEN 'cat' THEN '猫' WHEN 'dog' THEN '狗' ELSE '未分类' END""")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.execute("SELECT RELEASE_LOCK(CONCAT('exp_topics:',LEFT(SHA2(DATABASE(),256),40)))")
        cur.execute("""SELECT JSON_UNQUOTE(JSON_EXTRACT(t.object_scope,'$[0]')) species, COUNT(*) n
            FROM experience_search_term s JOIN experience_topic t USING(topic_key) GROUP BY species""")
        print(json.dumps({'applied': args.apply, 'species_counts': cur.fetchall()}, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
