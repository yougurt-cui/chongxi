"""Product recommendation normalization adapted from pet_product_import.zip."""
import hashlib
import json
import re
import unicodedata

BRAND_ALIASES = {'orijen': '渴望', '渴望(orijen)': '渴望', 'xiwei': '希喂', '希喂(xiwei)': '希喂'}
BREEDS = {'布偶猫': 'ragdoll', '布偶': 'ragdoll', 'ragdoll': 'ragdoll', '英国短毛猫': 'british_shorthair',
          '英短': 'british_shorthair', '美国短毛猫': 'american_shorthair', '美短': 'american_shorthair', '缅因猫': 'maine_coon'}
SCENES = {'food_selection', 'product_recommendation', 'product_overview', 'product_suitability'}


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text(value, field, maximum):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(field + ' 缺失或长度无效')
    return value.strip()


def integer(value, field):
    if type(value) is not int or value <= 0:
        raise ValueError(field + ' 必须为正整数')
    return value


def strings(value, field):
    value = decoded(value) if isinstance(value, str) and value.startswith('[') else value
    if isinstance(value, str):
        value = [s.strip() for s in re.split('[；;]', value) if s.strip()]
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(s, str) for s in value):
        raise ValueError(field + ' 必须为字符串数组')
    return list(dict.fromkeys(s.strip() for s in value if s.strip()))


def species_of(raw, scoped_species=None):
    cats = bool(re.search(r'猫|\bcat\b|\bkitten\b', raw, re.I))
    dogs = bool(re.search(r'狗|犬|\bdog\b|\bpuppy\b', raw, re.I))
    if cats and dogs:
        if scoped_species in {'cat', 'dog'}:
            return scoped_species
        raise ValueError('猫狗混合适用条件需拆分确认')
    return 'cat' if cats else 'dog' if dogs else None


def stages_of(conditions, species):
    raw = '；'.join(conditions)
    if re.search('全年龄|全生命阶段|所有年龄|全阶段', raw):
        return ['all_stages']
    stages = []
    if re.search('幼猫|幼犬|幼年|离乳', raw):
        stages.append('kitten' if species == 'cat' else 'puppy')
    if re.search('成猫|成犬|成年', raw):
        stages.append('adult')
    if re.search('老年|高龄', raw):
        stages.append('senior')
    return stages or ['unspecified']


def extract_record(item, source, scope=None):
    """Never combine different source scopes or infer identity from synthesized summaries."""
    scope = scope or {}
    if not isinstance(item, dict) or not isinstance(source, dict):
        raise ValueError('产品条目或来源格式无效')
    sid = integer(source.get('id'), 'source.id')
    ctx = decoded(source.get('source_context')) or {}
    structured = ctx.get('structured_item') or {}
    if not isinstance(structured, dict):
        raise ValueError('structured_item 必须为对象')
    parts = str(item.get('content') or '').split('；', 2)
    brand = text(structured.get('brand') or (parts[0] if parts else ''), 'brand', 160)
    normalized = unicodedata.normalize('NFKC', brand)
    brand = BRAND_ALIASES.get(normalized.casefold(), normalized)
    name = text(structured.get('product_name') or (parts[1] if len(parts) > 1 else ''), 'product_name', 300)
    if name.casefold().startswith(brand.casefold()):
        name = name[len(brand):].lstrip(' ：:·-')
    name = text(re.sub(r'\s+', ' ', name), 'product_name', 300)
    reason = structured.get('reason') or (parts[2] if len(parts) > 2 else '')
    if not isinstance(reason, str):
        raise ValueError('reason 必须为字符串')
    conditions = strings(structured.get('applicable_conditions') or source.get('original_condition'), 'applicable_conditions')
    question = str(ctx.get('question') or '')
    explicit_species = species_of(name + '；' + '；'.join(conditions), scope.get('species_code'))
    species = scope.get('species_code') or explicit_species or species_of(question)
    if species not in {'cat', 'dog'} or (explicit_species and explicit_species != species):
        raise ValueError('物种无法确认或与知识对象冲突')
    breed = scope.get('breed_code') or next((code for alias, code in BREEDS.items() if alias in question.lower()), 'all')
    relation = 'source_context'
    explicit = {code for alias, code in BREEDS.items() if alias in name.lower()}
    if len(explicit) == 1:
        breed, relation = next(iter(explicit)), 'explicit_product'
    breed = text(breed, 'breed_code', 64)
    stages = [scope['life_stage_code']] if scope.get('life_stage_code') else stages_of(conditions, species)
    needs = [n for n in strings(source.get('need_codes'), 'need_codes') if n not in SCENES] or ['general_recommendation']
    for stage in stages:
        text(stage, 'life_stage_code', 32)
    for need in needs:
        text(need, 'need_code', 80)
    group_id = scope.get('id')
    if group_id is not None:
        integer(group_id, 'knowledge_source_id')
    key = digest([species, re.sub(r'\s+', '', brand).casefold(), re.sub(r'\s+', '', name).casefold()])
    source_key = digest([group_id, sid, key])
    evidence = str(structured.get('evidence_status') or ctx.get('evidence_status') or source.get('evidence_status') or 'pending_verification')
    return {'product_key': key, 'species_code': species, 'brand': brand, 'product_name': name,
            'product_description': reason, 'source_key': source_key, 'knowledge_source_id': group_id,
            'original_source_id': sid, 'sources': [source],
            'source_scope': {k: scope.get(k) for k in ('species_code', 'breed_code', 'life_stage_code', 'need_scene_code')},
            'recommendations': [{'species_code': species, 'breed_code': breed, 'life_stage_code': stage,
                'need_code': need, 'relation_type': relation, 'recommendation_reason': reason,
                'evidence_status': evidence, 'review_status': 'pending'} for stage in stages for need in needs],
            'verification_status': 'pending'}


def normalize(data):
    """Accept flat archive examples or SELECT * exports with nested sources JSON."""
    if not isinstance(data, list):
        raise ValueError('输入必须为数组')
    records, errors, seen = [], [], set()
    for index, row in enumerate(data):
        try:
            if not isinstance(row, dict):
                raise ValueError('知识条目必须为对象')
            grouped = 'category' in row
            if grouped and row['category'] != 'product_recommendation':
                continue
            entries = decoded(row.get('sources')) if grouped else [row]
            if not isinstance(entries, list) or not entries:
                raise ValueError('缺少原始 sources；不从汇总正文猜测产品')
            for entry_index, item in enumerate(entries):
                sources = decoded(item.get('sources'))
                if not isinstance(sources, list) or not sources:
                    raise ValueError('缺少原始模型来源')
                for source in sources:
                    try:
                        record = extract_record(item, source, row if grouped else None)
                        signature = digest(record)
                        if record['source_key'] in seen:
                            previous = next(r for r in records if r['source_key'] == record['source_key'])
                            if digest(previous) != signature:
                                raise ValueError('同一来源指向冲突内容')
                            continue
                        seen.add(record['source_key']); records.append(record)
                    except (ValueError, TypeError, KeyError, AttributeError) as exc:
                        errors.append({'index': index, 'entry_index': entry_index,
                                       'source_id': source.get('id') if isinstance(source, dict) else None,
                                       'error': str(exc)})
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            errors.append({'index': index, 'error': str(exc)})
    return {'records': records, 'errors': errors}


if __name__ == '__main__':
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.import_product_recommendations import main
    raise SystemExit(main())
