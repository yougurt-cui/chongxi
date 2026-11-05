"""Shared pet identity vocabulary for vision suggestions and profile selectors."""
import math
import re

VERSION = 'pet-identity-v1'
# Stable identifiers, Chinese display names, and exact aliases. No fuzzy breed guessing.
CAT_BREEDS = [
    ('cat_domestic', '普通家猫', 'domestic cat|house cat'),
    ('cat_domestic_short', '普通短毛家猫', 'domestic shorthair|domestic short hair|短毛家猫'),
    ('cat_domestic_long', '普通长毛家猫', 'domestic longhair|domestic long hair|长毛家猫'),
    ('cat_chinese', '中华田园猫', '田园猫|chinese domestic cat'),
    ('cat_lihua', '中国狸花猫', '狸花猫|chinese li hua|dragon li'),
    ('cat_mixed', '混种猫', 'mixed breed cat|mixed breed|mixed|混血猫'),
    ('cat_british_short', '英国短毛猫', '英短|british shorthair'),
    ('cat_british_long', '英国长毛猫', '英长|british longhair'),
    ('cat_american_short', '美国短毛猫', '美短|american shorthair'),
    ('cat_ragdoll', '布偶猫', '布偶|ragdoll'),
    ('cat_persian', '波斯猫', '波斯|persian|persian cat'),
    ('cat_exotic', '异国短毛猫', '加菲猫|加菲|exotic shorthair'),
    ('cat_siamese', '暹罗猫', '暹罗|siamese'),
    ('cat_maine', '缅因猫', '缅因|maine coon'),
    ('cat_norwegian', '挪威森林猫', 'norwegian forest cat'),
    ('cat_siberian', '西伯利亚猫', 'siberian|siberian cat'),
    ('cat_bengal', '孟加拉豹猫', '孟加拉猫|bengal|bengal cat'),
    ('cat_russian_blue', '俄罗斯蓝猫', 'russian blue'),
    ('cat_scottish_fold', '苏格兰折耳猫', '折耳猫|scottish fold'),
    ('cat_sphynx', '斯芬克斯猫', '无毛猫|sphynx'),
    ('cat_devon', '德文卷毛猫', '德文|devon rex'),
    ('cat_abyssinian', '阿比西尼亚猫', 'abyssinian'),
    ('cat_birman', '伯曼猫', 'birman'),
    ('cat_unknown', '猫品种待确认', 'unknown|unknown breed|不确定|未知|其他'),
]
DOG_BREEDS = [
    ('dog_chinese', '中华田园犬', '田园犬|chinese rural dog|chinese village dog'),
    ('dog_mixed', '混种犬', '混血犬|混血狗|串串|mixed breed dog|mixed breed|mixed|mutt'),
    ('dog_poodle', '贵宾犬', '贵宾|泰迪|poodle|toy poodle|miniature poodle|standard poodle'),
    ('dog_bichon', '比熊犬', '比熊|bichon frise|bichon'),
    ('dog_maltese', '马尔济斯犬', '马尔济斯|马耳他犬|maltese'),
    ('dog_shihtzu', '西施犬', '西施|shih tzu'),
    ('dog_pomeranian', '博美犬', '博美|pomeranian'),
    ('dog_corgi', '威尔士柯基犬', '柯基|柯基犬|corgi|welsh corgi|pembroke welsh corgi'),
    ('dog_shiba', '柴犬', 'shiba inu|shiba'),
    ('dog_akita', '秋田犬', '秋田|akita'),
    ('dog_golden', '金毛寻回犬', '金毛|金毛犬|golden retriever'),
    ('dog_labrador', '拉布拉多寻回犬', '拉布拉多|拉布拉多犬|labrador|labrador retriever'),
    ('dog_husky', '西伯利亚哈士奇', '哈士奇|husky|siberian husky'),
    ('dog_samoyed', '萨摩耶犬', '萨摩耶|samoyed'),
    ('dog_malamute', '阿拉斯加雪橇犬', '阿拉斯加|alaskan malamute'),
    ('dog_border', '边境牧羊犬', '边牧|border collie'),
    ('dog_german', '德国牧羊犬', '德牧|german shepherd|german shepherd dog'),
    ('dog_sheltie', '喜乐蒂牧羊犬', '喜乐蒂|shetland sheepdog|sheltie'),
    ('dog_french', '法国斗牛犬', '法斗|french bulldog'),
    ('dog_english_bulldog', '英国斗牛犬', '英斗|english bulldog|bulldog'),
    ('dog_pug', '巴哥犬', '八哥犬|巴哥|pug'),
    ('dog_chihuahua', '吉娃娃', 'chihuahua'),
    ('dog_yorkshire', '约克夏梗', '约克夏|yorkshire terrier|yorkie'),
    ('dog_schnauzer', '雪纳瑞犬', '雪纳瑞|schnauzer|miniature schnauzer'),
    ('dog_dachshund', '腊肠犬', '腊肠|dachshund'),
    ('dog_beagle', '比格犬', '比格|beagle'),
    ('dog_cocker', '可卡犬', '可卡|cocker spaniel|american cocker spaniel|english cocker spaniel'),
    ('dog_pekingese', '北京犬', '京巴|京巴犬|pekingese'),
    ('dog_chow', '松狮犬', '松狮|chow chow'),
    ('dog_dalmatian', '大麦町犬', '斑点狗|dalmatian'),
    ('dog_rottweiler', '罗威纳犬', '罗威纳|rottweiler'),
    ('dog_doberman', '杜宾犬', '杜宾|doberman|doberman pinscher'),
    ('dog_unknown', '犬种待确认', 'unknown|unknown breed|不确定|未知|其他'),
]
BREEDS = {'cat': CAT_BREEDS, 'dog': DOG_BREEDS}
COAT_VALUES = {
    'color': {'白色': 'white|纯白', '黑色': 'black|纯黑', '灰色': 'gray|grey|蓝灰色',
              '橘色': 'orange|ginger|橙色', '棕色': 'brown', '奶油色': 'cream',
              '黑白': 'black and white|黑白色', '橘白': 'orange and white|橘白色',
              '黑白橘': 'black white orange', '其他': 'other'},
    'pattern': {'纯色': 'solid', '虎斑': 'tabby|狸花纹|虎斑纹', '三花': 'calico|三花猫',
                '玳瑁': 'tortoiseshell|tortie|玳瑁猫', '双色': 'bicolor|bi-color',
                '重点色': 'colorpoint|pointed', '奶牛花色': '奶牛猫|奶牛|cow pattern',
                '斑点': 'spotted|spots', '其他': 'other'},
    'length': {'短毛': 'short|shorthair|short hair|short-haired',
               '长毛': 'long|longhair|long hair|long-haired', '中长毛': 'medium|medium long',
               '无毛': 'hairless', '其他': 'other'},
}
COLOR_BREED_ALIASES = {'三花猫': ('黑白橘', '三花'), '三花': ('黑白橘', '三花'),
                       '橘猫': ('橘色', ''), '橘白猫': ('橘白', '双色'),
                       '奶牛猫': ('黑白', '奶牛花色'), '玳瑁猫': ('', '玳瑁'),
                       'calico': ('黑白橘', '三花'), 'tortoiseshell': ('', '玳瑁')}


def key(value):
    return re.sub(r'[\s_\-]+', '', str(value or '').strip()).casefold()


def confidence(value):
    try:
        number = float(value)
        return round(max(0., min(1., number)), 4) if math.isfinite(number) else 0.
    except (TypeError, ValueError):
        return 0.


def catalog():
    return {'version': VERSION, 'breeds': {
        species: [{'id': code, 'name': name} for code, name, _ in rows]
        for species, rows in BREEDS.items()},
        'appearance': {field: list(values) for field, values in COAT_VALUES.items()}}


def resolve_breed(species, value):
    for code, name, aliases in BREEDS.get(species, []):
        if key(value) in {key(v) for v in [code, name, *aliases.split('|')]}:
            return {'standard_id': code, 'value': name}
    return None


def normalize_identity(data):
    animal = data.get('animal_type') if isinstance(data.get('animal_type'), dict) else {}
    species = str(animal.get('value') or '').lower()
    species = {'猫': 'cat', '猫咪': 'cat', '狗': 'dog', '狗狗': 'dog'}.get(species, species)
    species = species if species in BREEDS else 'unknown'
    raw = data.get('breed') if isinstance(data.get('breed'), dict) else {}
    primary = resolve_breed(species, raw.get('value') or raw.get('standard_id'))
    candidates = []
    candidate_rows = raw.get('candidates') if isinstance(raw.get('candidates'), list) else []
    if primary and confidence(raw.get('confidence')) < .65 and not primary['standard_id'].endswith('unknown'):
        candidate_rows = [raw, *candidate_rows]
        primary = None
    for candidate in candidate_rows:
        if not isinstance(candidate, dict):
            continue
        match = resolve_breed(species, candidate.get('value') or candidate.get('standard_id'))
        if not match or match['standard_id'].endswith('unknown'):
            continue
        score = confidence(candidate.get('confidence'))
        existing = next((x for x in candidates if x['standard_id'] == match['standard_id']), None)
        if existing:
            existing['confidence'] = max(score, existing['confidence'])
        else:
            candidates.append({**match, 'confidence': score})
    candidates.sort(key=lambda x: -x['confidence'])
    is_uncertain = not primary or primary['standard_id'].endswith('unknown')
    fallback = resolve_breed(species, 'unknown') or {'standard_id': '', 'value': ''}
    breed = {**(primary or fallback), 'confidence': confidence(raw.get('confidence')) if not is_uncertain else 0.,
             'candidates': candidates[:5], 'requires_confirmation': True,
             'status': 'uncertain' if is_uncertain else 'suggested'}
    raw_appearance = data.get('appearance') if isinstance(data.get('appearance'), dict) else {}
    inferred_color, inferred_pattern = COLOR_BREED_ALIASES.get(str(raw.get('value') or '').strip().lower(), ('', '')) if species == 'cat' else ('', '')
    appearance = {}
    for field, values in COAT_VALUES.items():
        item = raw_appearance.get(field)
        value = item.get('value') if isinstance(item, dict) else item
        score = confidence(item.get('confidence')) if isinstance(item, dict) else 0.
        if not value and field in {'color', 'pattern'}:
            value = inferred_color if field == 'color' else inferred_pattern
            score = confidence(raw.get('confidence')) if value else 0.
        standard = next((name for name, aliases in values.items()
                         if value and key(value) in {key(v) for v in [name, *aliases.split('|')]}), '')
        appearance[field] = {'value': standard, 'confidence': score if standard else 0.}
    return {'animal_type': {'value': species, 'label': {'cat': '猫咪', 'dog': '狗狗', 'unknown': '未知'}[species],
                            'confidence': confidence(animal.get('confidence'))},
            'breed': breed, 'appearance': appearance, 'catalog_version': VERSION}
