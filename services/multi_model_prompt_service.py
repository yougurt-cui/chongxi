"""Editable prompt overrides; atomic writes, shared process lock and revision checks.

Collection data remains in its existing two tables. Prompt settings live under
var, so editing a template never rewrites a historical task's saved prompt.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG_PATH = ROOT / 'vendor/chongxi_multi_model_mysql/knowledge_bases.json'
SETTINGS_PATH = ROOT / 'var/multi-model/prompt_templates.json'


class TemplateConflict(ValueError):
    pass


def _read():
    seed = json.loads(BASE_CONFIG_PATH.read_text(encoding='utf-8'))
    settings = json.loads(SETTINGS_PATH.read_text(encoding='utf-8')) if SETTINGS_PATH.exists() else {'templates': {}, 'defaults': {}}
    return seed, settings


def _merge(seed, settings):
    bases = copy.deepcopy(seed['bases'])
    for base in bases:
        templates = []
        for item in base['templates']:
            path = (BASE_CONFIG_PATH.parent / item['file']).resolve()
            if not path.is_relative_to(BASE_CONFIG_PATH.parent.resolve()):
                raise ValueError('模板配置路径无效')
            templates.append({k: item.get(k, '') for k in ('code', 'title', 'description')} |
                             {'template': path.read_text(encoding='utf-8'), 'active': True})
        overrides = settings.get('templates', {}).get(base['code'], {})
        known = {item['code'] for item in templates}
        templates = [{**item, **overrides.get(item['code'], {})} for item in templates]
        templates += [item for code, item in overrides.items() if code not in known]
        active = [item for item in templates if item['active']]
        default = settings.get('defaults', {}).get(base['code'])
        if default not in {item['code'] for item in active}:
            default = active[0]['code'] if active else None
        for item in templates:
            item['is_default'] = item['code'] == default
        base['templates'] = templates
        base['default_template'] = default
    return {'default_base': seed.get('default_base') or bases[0]['code'], 'bases': bases}


def _snapshot(seed, settings):
    merged = _merge(seed, settings)
    revision = hashlib.sha256(json.dumps(merged, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {**merged, 'revision': revision}


def list_templates():
    return _snapshot(*_read())


def collection_bases():
    result = list_templates()
    for base in result['bases']:
        base['templates'] = [t for t in base['templates'] if t['active']]
        base['templates'].sort(key=lambda t: not t['is_default'])
    return result


def save_template(payload, *, base_code=None, template_code=None):
    if not isinstance(payload, dict):
        raise ValueError('模板内容必须为对象')
    base_code = base_code or payload.get('knowledge_base')
    title, description, template = (payload.get(k) for k in ('title', 'description', 'template'))
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
        raise ValueError('模板名称须为1–80字')
    if not isinstance(description, str) or len(description) > 500:
        raise ValueError('模板说明不能超过500字')
    if not isinstance(template, str) or not 1 <= len(template) <= 30000 or '{{question}}' not in template:
        raise ValueError('模板须包含 {{question}} 且不超过30000字')
    active, default = payload.get('active', True), payload.get('is_default', False)
    if not isinstance(active, bool) or not isinstance(default, bool):
        raise ValueError('启用和默认设置必须为布尔值')
    if default and not active:
        raise ValueError('停用模板不能设为默认')
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # The lock file stays at a stable inode while settings are atomically replaced.
    with SETTINGS_PATH.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        seed, settings = _read()
        snapshot = _snapshot(seed, settings)
        if payload.get('revision') != snapshot['revision']:
            raise TemplateConflict('模板已被其他操作更新，请刷新模板列表后重试；编辑内容已保留')
        base = next((b for b in snapshot['bases'] if b['code'] == base_code), None)
        if not base:
            raise LookupError('知识库不存在')
        if template_code is not None and not any(t['code'] == template_code for t in base['templates']):
            raise LookupError('模板不存在')
        if template_code is None:
            template_code = 'custom_' + uuid.uuid4().hex[:16]
        if not isinstance(template_code, str) or not re.fullmatch(r'[a-z0-9_]{1,80}', template_code):
            raise ValueError('模板编号无效')
        entry = {'code': template_code, 'title': title.strip(), 'description': description.strip(),
                 'template': template, 'active': active}
        settings.setdefault('templates', {}).setdefault(base_code, {})[template_code] = entry
        if default:
            settings.setdefault('defaults', {})[base_code] = template_code
        updated = _snapshot(seed, settings)
        if not next(b for b in updated['bases'] if b['code'] == base_code)['default_template']:
            raise ValueError('每个知识库至少保留一个启用的模板')
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=SETTINGS_PATH.parent, delete=False) as f:
                tmp = f.name
                json.dump(settings, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SETTINGS_PATH)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
    return {**updated, 'saved_code': template_code, 'saved_base': base_code}
