"""Conservatively initialize unclassified community search terms; dry-run by default."""
import argparse
import collections
import json
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import community_experience_service as svc

RULES = {
    '原因与疾病': r'原因|怎么回事|为什么|病因|疾病|综合征|感染|寄生虫|肠胃炎|牙周炎|牙龈炎|皮炎|肾病|糖尿病|胰腺炎|尿路结石',
    '做法与护理': r'怎么喂|如何喂|怎么养|如何养|怎么护理|如何护理|怎么清洁|如何清洁|怎么洗|如何洗|怎么刷|如何刷|怎么换粮|如何换粮|怎么复水|如何复水|复水方法|喂养方法|喂食方法|护理方法|清洁方法',
    '产品与配方': r'猫粮|狗粮|犬粮|主食罐|冻干|配料表|配方粮|营养标签|营养成分|产品测评',
    '症状与表现': r'症状|软便|腹泻|拉稀|便血|呕吐|尿血|尿频|流口水|牙龈红肿|便秘|食欲不振|呼吸困难|咳嗽|打喷嚏|眼屎|泪痕|黑下巴|便便臭|便臭|放屁多|排气增多',
    '行为与习惯': r'过度理毛|舔毛过度|乱抓|乱咬|躲藏|乱尿|夜叫|攻击行为|行为习惯|行为异常|分离焦虑',
    '结果与变化': r'前后对比|前后变化|改善过程|变化记录|恢复过程|恢复记录|效果记录|变化对比',
}
COMPILED = {key: re.compile(pattern) for key, pattern in RULES.items()}


def classify(term):
    matches = [key for key, pattern in COMPILED.items() if pattern.search(term)]
    # Multiple signals are ambiguous; do not choose one arbitrarily.
    return matches[0] if len(matches) == 1 else '待确认'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with svc.store.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(CONCAT('exp_topics:',LEFT(SHA2(DATABASE(),256),40)),10) AS acquired")
        if cur.fetchone()['acquired'] != 1:
            raise RuntimeError('experience topic refresh busy')
        try:
            cur.execute("SELECT topic_key,position_no,search_term FROM experience_search_term WHERE term_type=%s ORDER BY topic_key,position_no", ('待确认',))
            rows = cur.fetchall()
            counts = collections.Counter()
            examples = collections.defaultdict(list)
            changes = []
            for r in rows:
                kind = classify(r['search_term'])
                counts[kind] += 1
                if len(examples[kind]) < 12:
                    examples[kind].append(r['search_term'])
                if kind != '待确认':
                    changes.append((kind,r['topic_key'],r['position_no'],r['search_term'],'待确认'))
            updated = 0
            if args.apply:
                cur.executemany('''UPDATE experience_search_term SET term_type=%s
                    WHERE topic_key=%s AND position_no=%s AND BINARY search_term=BINARY %s AND term_type=%s''', changes)
                updated = cur.rowcount
                if updated != len(changes):
                    raise RuntimeError('terms changed during initialization; transaction rolled back')
                conn.commit()
            print(json.dumps({'applied':args.apply,'counts':counts,'updated':updated,'examples':examples},ensure_ascii=False))
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.execute("SELECT RELEASE_LOCK(CONCAT('exp_topics:',LEFT(SHA2(DATABASE(),256),40)))")

if __name__ == '__main__':
    main()
