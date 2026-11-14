"""Import the seven-sheet standard dictionary without changing model collection tables."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
DATABASE = 'chongxi_knowledge_dictionary'
SPECS = [
    ('物种字典', 'dict_species', ('species_code',)),
    ('品种字典', 'dict_breed', ('species_code', 'breed_code')),
    ('生命阶段字典', 'dict_life_stage', ('species_code', 'stage_code')),
    ('用户需求字典', 'dict_user_need', ('need_code',)),
    ('知识大分类', 'dict_knowledge_category', ('category_code',)),
    ('知识主题字典', 'dict_knowledge_topic', ('category_code', 'topic_code')),
    ('映射样例', 'dict_mapping_example', ('original_condition',)),
]
RENAMES = {'原始条件': 'original_condition', '备注': 'notes'}


def read_workbook(path):
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(n.itertext()) for n in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        rels = {r.attrib['Id']: r.attrib['Target'] for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        result = {}
        for sheet in ET.fromstring(z.read('xl/workbook.xml')).find('s:sheets', ns):
            target = rels[sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']]
            target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            rows = []
            for row in ET.fromstring(z.read(target)).findall('.//s:sheetData/s:row', ns):
                cells = {}
                for cell in row:
                    if cell.find('s:f', ns) is not None:
                        raise ValueError('字典中包含公式，请先确认公式结果')
                    value = cell.findtext('s:v', default='', namespaces=ns)
                    if cell.attrib.get('t') == 's': value = strings[int(value)]
                    if cell.attrib.get('t') == 'inlineStr':
                        value = ''.join(cell.find('s:is', ns).itertext())
                    cells[''.join(c for c in cell.attrib['r'] if c.isalpha())] = value
                if any(cells.values()): rows.append(cells)
            headers = {col: RENAMES.get(value, value) for col, value in rows[0].items()}
            result[sheet.attrib['name']] = [
                {header: row.get(col, '') for col, header in headers.items()} for row in rows[1:]]
    return result


def validate(data):
    if set(data) != {s[0] for s in SPECS}: raise ValueError('工作表与预期不符')
    for sheet, _, keys in SPECS:
        seen = set()
        for row in data[sheet]:
            key = tuple(row[k] for k in keys)
            if not all(key) or key in seen: raise ValueError(f'{sheet} 空编码或重复编码: {key}')
            seen.add(key)
    species = {r['species_code'] for r in data['物种字典']}
    breeds = {(r['species_code'], r['breed_code']) for r in data['品种字典']}
    stages = {(r['species_code'], r['stage_code']) for r in data['生命阶段字典']}
    needs = {r['need_code'] for r in data['用户需求字典']}
    categories = {r['category_code'] for r in data['知识大分类']}
    topics = {(r['category_code'], r['topic_code']) for r in data['知识主题字典']}
    for sheet in ('品种字典', '生命阶段字典'):
        for row in data[sheet]:
            if row['species_code'] not in species: raise ValueError('无效物种关联')
    for row in data['知识主题字典']:
        if row['category_code'] not in categories: raise ValueError('无效分类关联')
    for row in data['生命阶段字典']:
        for field in ('age_min_months_inclusive', 'age_max_months_exclusive'):
            row[field] = Decimal(row[field]) if row[field] != '' else None
        lo, hi = row['age_min_months_inclusive'], row['age_max_months_exclusive']
        if (lo is not None and lo < 0) or (hi is not None and (hi < 0 or (lo is not None and lo >= hi))):
            raise ValueError('无效月龄范围')
    for row in data['映射样例']:
        if (row['species_code'], row['breed_code']) not in breeds: raise ValueError('无效样例品种')
        if (row['category_code'], row['topic_code']) not in topics: raise ValueError('无效样例主题')
        if any((row['species_code'], s) not in stages for s in row['life_stage_codes'].split('|')): raise ValueError('无效样例阶段')
        if any(n not in needs for n in row['need_codes'].split('|')): raise ValueError('无效样例需求')


def import_data(data):
    import pymysql
    from app_config import get_mysql_config
    cfg = get_mysql_config(database=DATABASE)
    cfg.pop('database')
    with pymysql.connect(**cfg, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE IF NOT EXISTS `{DATABASE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
    cfg['database'] = DATABASE
    with pymysql.connect(**cfg, autocommit=False) as conn:
        with conn.cursor() as cur:
            for sheet, table, keys in SPECS:
                columns = list(data[sheet][0])
                definitions = []
                for col in columns:
                    kind = 'VARCHAR(255)' if col in keys or col.endswith('_code') else 'TEXT'
                    if col.startswith('age_'): kind = 'DECIMAL(10,3)'
                    definitions.append(f'`{col}` {kind}' + (' NOT NULL' if col in keys else ' NULL'))
                definitions.append('PRIMARY KEY (' + ','.join(f'`{k}`' for k in keys) + ')')
                cur.execute(f'CREATE TABLE IF NOT EXISTS `{table}` (' + ','.join(definitions) + ') ENGINE=InnoDB')
            # All data writes use one transaction. Reimport updates matching codes, never clears tables.
            for sheet, table, keys in SPECS:
                columns = list(data[sheet][0])
                query = f'INSERT INTO `{table}` (' + ','.join(f'`{c}`' for c in columns) + ') VALUES (' + ','.join(['%s'] * len(columns)) + ') ON DUPLICATE KEY UPDATE ' + ','.join(f'`{c}`=VALUES(`{c}`)' for c in columns if c not in keys)
                cur.executemany(query, [tuple(r[c] for c in columns) for r in data[sheet]])
            # Verify every stored field against the source before committing.
            for sheet, table, keys in SPECS:
                columns = list(data[sheet][0])
                for row in data[sheet]:
                    cur.execute(f'SELECT ' + ','.join(f'`{c}`' for c in columns) + f' FROM `{table}` WHERE ' + ' AND '.join(f'`{k}`=%s' for k in keys), tuple(row[k] for k in keys))
                    if cur.fetchone() != tuple(row[c] for c in columns): raise RuntimeError(f'{table} 写入核验失败')
        conn.commit()
    return {'database': DATABASE, 'tables': {table: len(data[sheet]) for sheet, table, _ in SPECS}, 'total': sum(map(len, data.values()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workbook')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    data = read_workbook(args.workbook)
    validate(data)
    result = {'validated': True, 'sheets': {k: len(v) for k, v in data.items()}}
    if not args.dry_run: result = import_data(data)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
