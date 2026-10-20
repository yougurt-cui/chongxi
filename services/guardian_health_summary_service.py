"""Model-assisted selection of traceable facts, never model-invented conclusions."""
import json
import logging

from openai import OpenAI
from app_config import get_chat_model_config

logger = logging.getLogger(__name__)


def build_overview(reports, comparisons):
    facts = []
    # Equal statements can be shared by products without implying a chronology.
    for kind, section in (('advantage', 'advantages'), ('attention', 'attentions')):
        groups = {}
        for index, report in enumerate(reports):
            for item in (report.get('presentation') or {}).get(section, []):
                key = (item['title'], item['summary'])
                groups.setdefault(key, []).append(index)
        for (title, text), indices in groups.items():
            indices = list(dict.fromkeys(indices))
            labels = '、'.join(reports[i]['label'] for i in indices)
            facts.append({'id': f'f{len(facts)}', 'kind': kind, 'title': title,
                          'text': labels + ('均：' if len(indices) > 1 else '：') + text,
                          'subject_indices': indices})
    for comparison in comparisons:
        for section, kind in (('complements', 'advantage'), ('shortcomings', 'attention')):
            for item in comparison.get(section) or []:
                text = item['text'].replace('原粮', comparison['left']).replace('目标粮', comparison['right'])
                facts.append({'id': f'f{len(facts)}', 'kind': kind, 'title': '配方差异',
                              'text': text, 'subject_indices': comparison.get('subject_indices', [])})
    chosen = {kind: [f['id'] for f in facts if f['kind'] == kind][:2]
              for kind in ('advantage', 'attention')}
    model_name = None
    cfg = get_chat_model_config()
    if facts and cfg.get('api_key'):
        try:
            options = {'extra_body': {'thinking': {'type': 'disabled'}}} if cfg.get('provider') == 'deepseek' else {}
            response = OpenAI(api_key=cfg['api_key'], base_url=cfg['base_url'], timeout=20, max_retries=0).chat.completions.create(
                model=cfg['model'], temperature=0, response_format={'type': 'json_object'},
                messages=[{'role': 'system', 'content':
                    '为多款猫粮选择整体解读的重点。只能从输入事实选择ID，不生成新结论。'
                    '优先共同特点和有依据的差异，兼顾不同产品。不得推测吃粮时间顺序或实际健康效果。'
                    '输出JSON：advantage和attention两个ID数组，各最多2项；没有依据时空数组。'},
                    {'role': 'user', 'content': json.dumps(facts, ensure_ascii=False)}], **options)
            selected = json.loads(response.choices[0].message.content)
            valid = {f['id']: f['kind'] for f in facts}
            for kind in chosen:
                values = selected[kind]
                if (not isinstance(values, list) or len(values) > 2 or len(set(values)) != len(values)
                        or any(not isinstance(v, str) or valid.get(v) != kind for v in values)
                        or (chosen[kind] and not values)):
                    raise ValueError('invalid overview evidence')
            chosen = selected
            model_name = cfg['model']
        except Exception as error:
            logger.warning('guardian_overview_fallback error=%s', type(error).__name__)
    by_id = {f['id']: f for f in facts}
    result = {'title': '猫咪的整体饮食解读',
              'advantages': [by_id[i] for i in chosen['advantage']],
              'attentions': [by_id[i] for i in chosen['attention']],
              'unavailable_subjects': [r['label'] for r in reports if r.get('status') == 'unavailable'],
              'model_name': model_name, 'mode': 'model_evidence_selection' if model_name else 'evidence_fallback',
              'note': '以下是配方特点的归纳，不代表实际吃粮效果；记录未证明时间顺序时不推断持续改善。'}
    lines = ['**' + result['title'] + '**']
    for key, title in (('advantages', '✅ **好在哪里？**'), ('attentions', '❗ **哪里需要留心？**')):
        if result[key]:
            lines.append(title)
            lines.extend(item['text'] for item in result[key])
    if not facts:
        lines.append('现有资料不足以归纳整体优势或留心点，以下逐款说明可用资料。')
    if result['unavailable_subjects']:
        lines.append('资料不足：' + '、'.join(result['unavailable_subjects']))
    lines.append(result['note'])
    result['text'] = '\n\n'.join(lines)
    return result
