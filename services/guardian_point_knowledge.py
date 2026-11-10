"""Read-only retrieval of model knowledge; never import the ingestion writers.

The source provider belongs to the import batch, not standardization_meta.provider.
Successful standardization validates dictionary mappings, not the underlying claims.
"""
from __future__ import annotations

import json
import logging
import os
import re

import pymysql

from app_config import get_mysql_config

logger = logging.getLogger(__name__)
SOURCE_TABLE = 'knowledge_point_source_detail'
REVIEW_NOTE = '以下内容由 AI 知识资料整合，尚未专业验证，仅供养宠参考。'
DICTIONARIES = ('dict_breed', 'dict_life_stage', 'dict_user_need', 'dict_knowledge_topic')
PROVIDERS = {'deepseek', 'qwen', 'doubao', 'file'}


def database_name(env, default):
    value = os.getenv(env, default)
    if not re.fullmatch(r'[A-Za-z0-9_]+', value):
        raise ValueError('Invalid knowledge database name')
    return '`' + value + '`'


def _connect():
    return pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor,
                           connect_timeout=3, read_timeout=5, autocommit=True)


def parsed(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (ValueError, TypeError):
        return default


def names(row, field):
    return [str(row.get(field) or ''), *str(row.get('aliases_pipe') or '').split('|')]


def mentioned(word, query):
    if len(word) < 2 or word == 'ALL':
        return False
    return any(not re.search(r'不是|并非|没有|不需要|不考虑', query[max(0, m.start()-5):m.start()])
               for m in re.finditer(re.escape(word), query, re.I))


def scope_for(plan, dictionaries):
    """Explicit question subjects override selected-pet facts; never infer a breed."""
    query = plan['query']
    pet = dict(plan.get('pet_scope') or {})
    breeds = [r for r in dictionaries['dict_breed'] if r.get('status') == '启用'
              and not r['breed_code'].startswith('all_')]
    explicit = [r for r in breeds if any(mentioned(n, query) for n in names(r, 'breed_name'))]
    profile = next((r for r in breeds if str(pet.get('breed', '')).lower() in
                    [s.lower() for s in names(r, 'breed_name') + [r['breed_code']]]), None)
    if len(explicit) == 1:
        breed = explicit[0]
        if not profile or profile['breed_code'] != breed['breed_code']:
            pet = {'animal_type': breed['species_code']}
        pet['breed_code'] = breed['breed_code']
        pet['breed'] = breed['breed_name']
    elif not explicit and profile:
        pet['breed_code'] = profile['breed_code']
    elif explicit:
        # A multi-breed question may use species-wide knowledge only.
        pet = {'animal_type': plan.get('species')}
    age = re.search(r'(\d+(?:\.\d+)?)\s*(岁|个月|月龄)', query)
    if age:
        pet['age_months'] = float(age[1]) * (12 if age[2] == '岁' else 1)
    stage_words = {'kitten': r'幼猫', 'puppy': r'幼犬|幼狗', 'adult': r'成猫|成犬|成年', 'senior': r'老年猫|老年犬|老年狗'}
    stages = {code for code, pattern in stage_words.items() if re.search(pattern, query)}
    if len(stages) == 1:
        pet['explicit_stage'] = next(iter(stages))
        if not age:
            pet.pop('age_months', None)
    return pet


def applicable(row, pet, stages):
    species = pet.get('animal_type')
    if species not in ('cat', 'dog') or row.get('species_code') != species:
        return False
    breed = row.get('breed_code')
    if breed != 'all_' + species and (not breed or breed != pet.get('breed_code')):
        return False
    codes = parsed(row.get('life_stage_codes'), [])
    if not isinstance(codes, list) or not codes or not all(isinstance(code, str) for code in codes):
        return False
    allowed = {s['stage_code']: s for s in stages if s['species_code'] == species and s.get('status') == '启用'}
    if any(code not in allowed for code in codes):
        return False
    raw = row.get('original_condition') or ''
    # The live data contains 'unspecified' mappings even when the raw condition
    # says adult. Recover explicit restrictions instead of treating it as all ages.
    if codes == ['unspecified']:
        inferred = []
        for code, pattern in (('adult', r'成猫|成年|成犬'), ('kitten' if species == 'cat' else 'puppy', r'幼猫|幼犬'),
                              ('senior', r'老年')):
            if re.search(pattern, raw):
                inferred.append(code)
        if inferred:
            codes = inferred
        elif row.get('topic_code') == 'life_stage_nutrition':
            return False
    if not set(codes).intersection({'all_stages', 'unspecified'}):
        age = pet.get('age_months')
        explicit = pet.get('explicit_stage')
        matched = explicit in codes if explicit else False
        if age is not None:
            matched = any(s.get('age_min_months_inclusive') is not None
                          and float(age) >= float(s['age_min_months_inclusive'])
                          and (s.get('age_max_months_exclusive') is None or float(age) < float(s['age_max_months_exclusive']))
                          for code, s in allowed.items() if code in codes)
        if not matched:
            return False
    # More restrictive original numeric age ranges survive broad dictionary bins.
    bounds = re.search(r'(\d+)\s*[–—~-]\s*(\d+)\s*(岁|月龄|个月)', raw)
    minimum = re.search(r'(?:≥|满)(\d+)\s*(岁|月龄)|([0-9]+)\s*(岁|月龄)以上', raw)
    if bounds:
        factor = 12 if bounds[3] == '岁' else 1
        if pet.get('age_months') is None or not float(bounds[1])*factor <= float(pet['age_months']) <= float(bounds[2])*factor:
            return False
    if minimum:
        number, unit = (minimum[1], minimum[2]) if minimum[1] else (minimum[3], minimum[4])
        if pet.get('age_months') is None or float(pet['age_months']) < float(number)*(12 if unit == '岁' else 1):
            return False
    # Diagnosis/pregnancy restrictions cannot be established from a breed or a need tag.
    if re.search(r'确诊|心衰|怀孕|妊娠|哺乳', raw):
        return False
    return True


def retrieve(plan):
    from services import guardian_knowledge_service as qa
    if not plan.get('terms'):
        return {'status': 'no_match', 'items': [], 'source_table': SOURCE_TABLE}
    source = database_name('GUARDIAN_KNOWLEDGE_DATABASE', 'chongxi_model_knowledge')
    dictionary = database_name('GUARDIAN_DICTIONARY_DATABASE', 'chongxi_knowledge_dictionary')
    try:
        with _connect() as conn, conn.cursor() as cur:
            dictionaries = {}
            for table in DICTIONARIES:
                cur.execute(f'SELECT * FROM {dictionary}.{table}')
                dictionaries[table] = cur.fetchall()
            pet = scope_for(plan, dictionaries)
            query = plan['query']
            topic_codes = {r['topic_code'] for r in dictionaries['dict_knowledge_topic']
                           if any(mentioned(n, query) for n in names(r, 'topic_name'))}
            needs = {r['need_code'] for r in dictionaries['dict_user_need']
                     if any(mentioned(n, query) for n in names(r, 'need_name'))}
            broad_feeding = bool(re.search(r'喂养|饮食|营养|选粮|主粮', query)) and not (topic_codes or needs)
            if broad_feeding:
                topic_codes.update(('life_stage_nutrition', 'food_selection', 'feeding_method'))
            # Match the actual subject matter, not just a breed appearing in every row.
            stop = {'猫咪', '狗狗', '布偶', '成猫', '幼猫', '成年', '老年', '我家', '这个', '时候', '问题', '一下',
                    '注意', '要注', '应该', '应当', '如何', '怎么', '什么', '要点', '有关', '的蛋', '的心', '健康'}
            for breed in dictionaries['dict_breed']:
                for name in names(breed, 'breed_name'):
                    stop.update(name[i:i+2] for i in range(len(name)-1))
            terms = [t for t in plan.get('terms', []) if t not in stop][:18]
            if not terms and not topic_codes and not needs:
                return {'status': 'no_match', 'items': [], 'source_table': SOURCE_TABLE}
            if pet.get('animal_type') not in ('cat', 'dog'):
                return {'status': 'no_match', 'items': [], 'source_table': SOURCE_TABLE}
            parts, values = [], []
            for term in terms:
                pattern = '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                parts.append('(k.content LIKE %s)'); values.append(pattern)
            for code in sorted(topic_codes):
                parts.append('(k.topic_code=%s) * 5'); values.append(code)
            for code in sorted(needs):
                parts.append('JSON_CONTAINS(k.need_codes,%s) * 4'); values.append(json.dumps(code))
            score = ' + '.join(parts)
            cur.execute(f'''SELECT k.*,b.model AS source_model,b.task_id,
                ({score}) AS relevance FROM {source}.knowledge_point_source_detail k
                JOIN {source}.knowledge_import_batch b ON b.id=k.batch_id
                WHERE b.status='success' AND k.source_provider=b.provider
                  AND JSON_UNQUOTE(JSON_EXTRACT(k.standardization_meta,'$.status'))='success'
                  AND k.species_code=%s AND k.breed_code IN (%s,%s)
                  AND k.category_code <> 'product_goods'
                HAVING relevance>0 ORDER BY relevance DESC,k.id DESC LIMIT 240''',
                [*values, pet['animal_type'], 'all_' + pet['animal_type'], pet.get('breed_code') or ''])
            rows = cur.fetchall()
        selected, seen = [], {}
        for row in rows:
            content = row.get('content') or ''
            if (row.get('source_provider') not in PROVIDERS or not applicable(row, pet, dictionaries['dict_life_stage'])
                    or not content or len(content) > 2000 or qa.UNSAFE_SOURCE.search(content)
                    or qa.UNVERIFIED_MEDICAL.search(content) or qa.NEGATIVE_CLAIM.search(content)
                    or qa.GROUP_CLAIM.search(content)):
                continue
            if (topic_codes or needs) and not broad_feeding:
                # A weak word such as 注意 must not introduce an unrelated provider.
                focused = [t for t in terms if any(t in n for r in dictionaries['dict_knowledge_topic']
                           if r['topic_code'] in topic_codes for n in names(r, 'topic_name'))]
                focused += [t for t in terms if any(t in n for r in dictionaries['dict_user_need']
                            if r['need_code'] in needs for n in names(r, 'need_name'))]
                if (row['topic_code'] not in topic_codes and not needs.intersection(parsed(row.get('need_codes'), []))
                        and not any(t in content for t in focused)):
                    continue
            if plan.get('anchors') and not (topic_codes or needs) and not any(t in content for t in plan['anchors']):
                continue
            row = {**row, 'content': '适用条件：' + row['original_condition'] + '\n' + content,
                   'topic': row['category_code'], 'subtopic': row['topic_code'],
                   'evidence_status': '待验证', 'status': '待审核', 'source_table': SOURCE_TABLE,
                   'conditions_json': json.dumps({'original_condition': row['original_condition']}, ensure_ascii=False)}
            duplicate_key = (row['source_provider'], row['content'])
            if duplicate_key in seen:
                seen[duplicate_key].setdefault('duplicate_point_ids', []).append(row['id'])
                continue
            seen[duplicate_key] = row
            selected.append(row)
        # Reserve candidates for each available provider before filling by rank.
        chosen, providers = [], set()
        for row in selected:
            if row['source_provider'] not in providers:
                chosen.append(row); providers.add(row['source_provider'])
        for row in selected:
            if len(chosen) >= 12:
                break
            if row not in chosen:
                chosen.append(row)
        return {'status': 'matched' if chosen else 'no_match', 'items': chosen,
                'source_table': SOURCE_TABLE, 'scope': pet}
    except pymysql.MySQLError as exc:
        logger.warning('guardian_point_knowledge_unavailable error=%s', type(exc).__name__)
        return {'status': 'unavailable', 'items': [], 'source_table': SOURCE_TABLE}
