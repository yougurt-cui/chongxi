"""Opt-in food classification → cat health candidates. Legacy growth jobs stay paused."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid
import pymysql
from app_config import get_mysql_config
from comment_pipeline.common.cleaner import normalize_text, is_low_information
from comment_pipeline.llm_router import Classifier, VERSION, fingerprint, validate_events


def dumps(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)


def connect():
    return pymysql.connect(**get_mysql_config(database='csv_labeling'),cursorclass=pymysql.cursors.DictCursor,autocommit=False)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS comment_classification_runs (
            run_id VARCHAR(64) PRIMARY KEY, version VARCHAR(64) NOT NULL, model VARCHAR(128) NOT NULL,
            status VARCHAR(32) NOT NULL, summary JSON NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at DATETIME NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
        cur.execute('''CREATE TABLE IF NOT EXISTS comment_classification_results (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            comment_id BIGINT NOT NULL, input_hash CHAR(64) NOT NULL, version VARCHAR(64) NOT NULL,
            model VARCHAR(128) NOT NULL, last_run_id VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL, input_snapshot JSON NOT NULL,
            classification JSON NOT NULL, health_annotation JSON NULL, candidate_ids JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_input (comment_id,input_hash), KEY idx_run(last_run_id), KEY idx_status(status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
        # Explicit provenance avoids interpreting clean comment IDs as legacy Choice IDs.
        cur.execute('SHOW COLUMNS FROM cat_disease_clue_candidates')
        columns={r['Field'] for r in cur.fetchall()}
        for name,definition in {'source_table':'VARCHAR(64) NULL','source_comment_id':'BIGINT NULL','source_router_result_id':'BIGINT UNSIGNED NULL'}.items():
            if name not in columns:cur.execute('ALTER TABLE cat_disease_clue_candidates ADD COLUMN '+name+' '+definition)
    conn.commit()


def eligible(route):
    return 'pet_food' in route['domains'] and 'health_observation' in route['processors'] and route['species']=='cat'


def process(row,classifier):
    classification=classifier.classify(row)
    result={'classification':classification,'health_annotation':None,'status':'route_failed'}
    if classification['status']!='success':return result
    route=classification['result'];result['status']='classified'
    if eligible(route):
        health=classifier.tag(row);result['health_annotation']=health
        result['status']='tagged' if health['status']=='success' else 'tag_failed'
    return result


def save_result(conn,row,result,run_id,model):
    with conn.cursor() as cur:
        cur.execute('''INSERT INTO comment_classification_results
            (comment_id,input_hash,version,model,last_run_id,status,input_snapshot,classification,health_annotation,candidate_ids)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id),last_run_id=VALUES(last_run_id),status=VALUES(status),
            classification=VALUES(classification),health_annotation=VALUES(health_annotation)''',
            (row['comment_id'],fingerprint(row,model),VERSION,model,run_id,result['status'],dumps(row),dumps(result['classification']),
             dumps(result['health_annotation']) if result['health_annotation'] else None,'[]'))
        router_id=cur.lastrowid;ids=[];inserted=0
        if result['status']=='tagged':
            for event in result['health_annotation']['result']:
                if event['observation'] in {'absent','uncertain'}:continue
                comment_hash=hashlib.md5(row['clean_text'].encode()).hexdigest()
                cur.execute('''SELECT id FROM cat_disease_clue_candidates WHERE comment_hash=%s AND brand_name=%s
                    AND symptom_category=%s AND symptom_name=%s AND effect_direction=%s FOR UPDATE''',
                    (comment_hash,event['brand_name'],event['symptom_category'],event['symptom_name'],event['effect_direction']))
                existing=cur.fetchone()
                if existing:
                    ids.append(existing['id'])
                    continue
                # 0 means not sourced from the legacy Choice table; the explicit source columns carry the ID.
                cur.execute('''INSERT INTO cat_disease_clue_candidates
                    (source_candidate_id,comment_hash,platform,external_id,brand_name,search_keyword,mentioned_brands,
                     review_text,symptom_category,symptom_name,effect_direction,confidence,evidence_text,model_name,prompt_version,
                     review_status,source_table,source_comment_id,source_router_result_id)
                    VALUES (0,%s,%s,%s,%s,'',%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING','comment_clean_base',%s,%s)
                    ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)''',
                    (comment_hash,row['platform'],row['source_id'],event['brand_name'],event['brand_name'],
                     row['clean_text'],event['symptom_category'],event['symptom_name'],event['effect_direction'],event['confidence'],
                     event['evidence_text'],model,VERSION,row['comment_id'],router_id))
                inserted+=int(cur.rowcount==1);ids.append(cur.lastrowid)
        cur.execute('UPDATE comment_classification_results SET candidate_ids=%s WHERE id=%s',(dumps(ids),router_id))
    conn.commit()
    result.update(result_id=router_id,candidate_ids=ids,new_candidates=inserted)
    return result


def run(*,limit=100,scan_limit=1000,workers=4,output_dir='var/comment-food-health'):
    if not 1<=limit<=10000 or not limit<=scan_limit<=20000 or not 1<=workers<=8:raise ValueError('limit/scan_limit/workers超出范围')
    classifier=Classifier();model=classifier.config['model'];conn=connect();locked=False
    run_id='food_health_'+datetime.now().strftime('%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:8]
    root=Path(output_dir)/run_id;root.mkdir(parents=True,exist_ok=True)
    stats=Counter();domains=Counter();processors=Counter();species=Counter();symptoms=Counter();results=[]
    def consume(row,result,cached=False):
        stats['scanned']+=1;stats['cached']+=int(cached)
        route=result['classification']
        if route['status']!='success':stats['route_failed']+=1
        else:
            r=route['result'];domains.update(r['domains']);processors.update(r['processors']);species.update([r['species']])
            if 'pet_food' in r['domains']:stats['pet_food']+=1
            if eligible(r):stats['health_eligible']+=1
        if result['status']=='tag_failed':stats['tag_failed']+=1
        if result['status']=='tagged':
            stats['tagged_comments']+=1
            for event in result['health_annotation']['result']:
                stats['health_events']+=1;symptoms.update([event['symptom_name']]);stats['observation_'+event['observation']]+=1
        stats['new_candidates']+=result.get('new_candidates',0)
        stats['linked_candidates']+=len(result.get('candidate_ids',[]))
        packet={'comment_id':row['comment_id'],'input':row,**result};results.append(packet)
        with (root/'results.jsonl').open('a') as f:f.write(dumps(packet)+'\n')
        print(dumps({'run_id':run_id,**stats}),flush=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK('comment-food-health-trial',0) acquired")
            if not cur.fetchone()['acquired']:raise RuntimeError('宠物食品试跑任务正在执行')
            locked=True
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute('INSERT INTO comment_classification_runs (run_id,version,model,status) VALUES (%s,%s,%s,%s)',(run_id,VERSION,model,'running'))
            # Food words select a bounded test pool only; the LLM makes the final domain decision.
            cur.execute('''SELECT comment_id,platform,source_table,source_id,raw_text,clean_text,source_title,source_content,source_keyword,source_created_at
                FROM comment_clean_base WHERE CONCAT_WS(' ',clean_text,source_keyword,source_title)
                REGEXP '猫粮|狗粮|主粮|猫条|狗条|冻干|罐头|宠物零食|换粮|喂粮|羊奶|粮'
                ORDER BY comment_id DESC LIMIT %s''',(scan_limit,))
            rows=list(cur.fetchall())
        conn.commit()
        # Deduplicate the cleaned comment within a trial, including cross-platform copies.
        seen=set();pool=[]
        for row in rows:
            row['clean_text']=normalize_text(row['raw_text'] or row['clean_text'])
            text=row['clean_text']
            if is_low_information(text) or len(text)>1000:stats['noise_or_long']+=1;continue
            if text in seen:stats['duplicate_text']+=1;continue
            seen.add(text);pool.append(row)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            cursor=0
            while stats['pet_food']<limit and cursor<len(pool):
                batch=pool[cursor:cursor+min(workers,limit-stats['pet_food'])];cursor+=len(batch)
                pending=[]
                for row in batch:
                    with conn.cursor() as cur:
                        cur.execute('SELECT * FROM comment_classification_results WHERE comment_id=%s AND input_hash=%s',
                                    (row['comment_id'],fingerprint(row,model)))
                        cached=cur.fetchone()
                    conn.commit()
                    if cached and cached['status']=='tagged':
                        health=json.loads(cached['health_annotation']) if isinstance(cached['health_annotation'],str) else cached['health_annotation']
                        try:validate_events({'events':health['result']},row)
                        except ValueError:cached=None
                    if cached and cached['status'] not in {'route_failed','tag_failed'}:
                        result={k:json.loads(cached[k]) if isinstance(cached[k],str) else cached[k] for k in ('classification','health_annotation','candidate_ids')}
                        result['status']=cached['status'];result['result_id']=cached['id']
                        with conn.cursor() as cur:cur.execute('UPDATE comment_classification_results SET last_run_id=%s WHERE id=%s',(run_id,cached['id']))
                        conn.commit();consume(row,result,True)
                    else:pending.append((row,executor.submit(process,row,classifier)))
                for row,future in pending:
                    result=future.result()
                    try:save_result(conn,row,result,run_id,model)
                    except Exception:conn.rollback();raise
                    consume(row,result)
        status='completed' if stats['pet_food']==limit and not stats['route_failed'] and not stats['tag_failed'] else 'partial_failed'
        summary={'run_id':run_id,'status':status,'model':model,'version':VERSION,'target_pet_food':limit,'scan_limit':scan_limit,
                 'sample_order':'latest food-keyword recall; not an accuracy benchmark',**stats,
                 'domains':dict(domains),'processors':dict(processors),'species':dict(species),'symptoms':dict(symptoms),
                 'results_file':str(root/'results.jsonl'),'candidate_table':'csv_labeling.cat_disease_clue_candidates',
                 'classification_table':'csv_labeling.comment_classification_results'}
        (root/'summary.json').write_text(dumps(summary)+'\n')
        with conn.cursor() as cur:cur.execute('UPDATE comment_classification_runs SET status=%s,summary=%s,finished_at=NOW() WHERE run_id=%s',(status,dumps(summary),run_id))
        conn.commit();return summary
    except BaseException:
        conn.rollback()
        if locked:
            try:
                with conn.cursor() as cur:cur.execute("UPDATE comment_classification_runs SET status='interrupted',finished_at=NOW() WHERE run_id=%s AND status='running'",(run_id,))
                conn.commit()
            except Exception:pass
        raise
    finally:
        if locked:
            with conn.cursor() as cur:cur.execute("SELECT RELEASE_LOCK('comment-food-health-trial')")
        conn.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit',type=int,default=100,help='模型确认的宠物食品评论数量')
    parser.add_argument('--scan-limit',type=int,default=1000)
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--output-dir',default='var/comment-food-health')
    args=parser.parse_args();print(dumps(run(**vars(args))),flush=True)


if __name__=='__main__':main()
