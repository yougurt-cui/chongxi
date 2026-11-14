"""Read screenshot-derived recommendation spreadsheets without changing the workbook."""
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import posixpath
import re
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import zipfile
from vendor.pet_product_import.import_products import extract_record, digest

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REQUIRED = {'种类', '品种', '需求', '品牌', '商品名', '推荐关系', '推荐比例', '场景排名',
            '生命阶段', '需求代码', '截图提问', '参考经验人数', '来源图片', '核验状态'}


def read_rows(path, sheet_name='产品推荐关系'):
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(t.text or '' for t in node.findall('.//s:t', NS))
                       for node in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', NS)]
        relationships = {node.get('Id'): node.get('Target') for node in
                         ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        sheets = ET.fromstring(archive.read('xl/workbook.xml')).findall('s:sheets/s:sheet', NS)
        sheet = next((s for s in sheets if s.get('name') == sheet_name), None)
        if sheet is None: raise ValueError('未找到工作表：' + sheet_name)
        target = relationships[sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')]
        target = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        rows = []
        for row in ET.fromstring(archive.read(target)).findall('s:sheetData/s:row', NS):
            values = {}
            for cell in row.findall('s:c', NS):
                if cell.find('s:f', NS) is not None: raise ValueError('来源数据存在公式，请先导出为确定值')
                value = cell.find('s:v', NS)
                value = value.text if value is not None else ''
                if cell.get('t') == 's': value = strings[int(value)]
                if cell.get('t') == 'inlineStr':
                    value = ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
                values[re.sub(r'\d', '', cell.get('r'))] = (value or '').strip()
            if any(values.values()): rows.append((int(row.get('r')), values))
    if not rows: raise ValueError('工作表为空')
    headers = rows[0][1]
    if set(headers.values()) != REQUIRED or len(headers) != len(REQUIRED):
        raise ValueError('Excel 字段与截图推荐模板不一致')
    return [(number, {label: cells.get(col, '') for col, label in headers.items()}) for number, cells in rows[1:]]


def numeric(value, field, *, integer=False, maximum=None):
    if value == '': return None
    try: result = Decimal(str(value))
    except InvalidOperation: raise ValueError(field + '不是有效数字')
    if not result.is_finite() or result < 0 or (maximum is not None and result > maximum):
        raise ValueError(field + '超出有效范围')
    if integer:
        if result != result.to_integral_value() or result == 0: raise ValueError(field + '必须是正整数')
        return int(result)
    return float(result.quantize(Decimal('0.000001')))


def normalize_row(values, *, file_name, file_hash, sheet_name, row_number):
    if values['种类'] not in {'猫', '狗'}: raise ValueError('种类必须明确为猫或狗')
    species = 'cat' if values['种类'] == '猫' else 'dog'
    breeds = {'不限（未指定品种）': 'all_' + species, '不限': 'all_' + species,
              '布偶猫': 'ragdoll'}
    if values['品种'] not in breeds: raise ValueError('品种尚无明确编码：' + values['品种'])
    stages = {'不限': 'unspecified', '未明确': 'unspecified', '幼猫': 'kitten', '幼犬': 'puppy',
              '成年': 'adult', '成猫': 'adult', '成犬': 'adult', '老年': 'senior', '全生命阶段': 'all_stages'}
    if values['生命阶段'] not in stages: raise ValueError('生命阶段尚无明确编码')
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', values['需求代码']): raise ValueError('需求代码无效')
    if not all(values[k] for k in ('品牌', '需求', '推荐关系', '截图提问', '来源图片')):
        raise ValueError('缺少品牌、需求、推荐关系或截图来源')
    unknown = values['商品名'] in {'', '未明确', '未指定', '未知', '不限'}
    product_name = '未明确' if unknown else values['商品名']
    signals = {'recommendation_ratio': numeric(values['推荐比例'], '推荐比例', maximum=1),
               'scene_rank': numeric(values['场景排名'], '场景排名', integer=True),
               'experience_count': numeric(values['参考经验人数'], '参考经验人数', integer=True),
               'source_image': values['来源图片'], 'need_name': values['需求']}
    source = {'id': row_number, 'batch_id': None, 'source_provider': 'community_screenshot',
              'source_model': None, 'source_type': 'community_screenshot',
              'source_file': file_name, 'file_sha256': file_hash, 'source_sheet': sheet_name,
              'source_row': row_number, **signals, 'need_codes': [values['需求代码']],
              'spreadsheet_review_status': values['核验状态'], 'spreadsheet_row': values,
              'source_context': {'question': values['截图提问'], 'evidence_status': 'unverified_community',
                  'structured_item': {'brand': values['品牌'], 'product_name': product_name,
                      'reason': values['推荐关系'], 'applicable_conditions': [values['种类']]}}}
    record = extract_record({'content': values['推荐关系']}, source, {
        'species_code': species, 'breed_code': breeds[values['品种']], 'life_stage_code': stages[values['生命阶段']]})
    record['entity_level'] = 'brand' if unknown else 'product'
    if unknown: record['product_key'] = digest(['brand', species, record['brand'].casefold()])
    record['source_key'] = digest(['xlsx_screenshot', file_hash, sheet_name, row_number, record['product_key']])
    for recommendation in record['recommendations']:
        recommendation['relation_type'] = 'community_reference'
    return record


def normalize_workbook(path, sheet_name='产品推荐关系'):
    path = Path(path)
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    records, errors = [], []
    rows = read_rows(path, sheet_name)
    for row_number, values in rows:
        try:
            records.append(normalize_row(values, file_name=unquote(path.name), file_hash=fingerprint,
                                         sheet_name=sheet_name, row_number=row_number))
        except (ValueError, TypeError, KeyError) as exc:
            errors.append({'sheet': sheet_name, 'row': row_number, 'message': str(exc)})
    return {'records': records, 'errors': errors, 'source_rows': len(rows), 'file_sha256': fingerprint}
