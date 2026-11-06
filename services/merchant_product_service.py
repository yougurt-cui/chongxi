"""Merchant products linked atomically to the existing mini-program community."""
from __future__ import annotations

import io
import json
import re
import uuid
import warnings
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from PIL import Image, UnidentifiedImageError
from werkzeug.datastructures import FileStorage
from services import merchant_certification_service as certification
from services import miniprogram_moment_image_service as images
from services import miniprogram_moment_service as moments
from services.miniprogram_content_review_service import check_moment_payload_with_wechat

PRODUCT_TABLE = 'miniprogram_merchant_product'
RELATION_TABLE = 'miniprogram_merchant_post_product'


class MerchantForbidden(Exception):
    pass


def _connect():
    return certification._connect()


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


@lru_cache(maxsize=1)
def ensure_tables():
    certification.ensure_table()
    moments.init_miniprogram_moment_tables()
    images.init_miniprogram_moment_image_tables()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f'''CREATE TABLE IF NOT EXISTS {PRODUCT_TABLE} (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                product_id VARCHAR(64) NOT NULL,
                user_id CHAR(32) NOT NULL,
                merchant_id BIGINT UNSIGNED NOT NULL,
                post_id CHAR(32) NOT NULL,
                image_ids_json JSON NOT NULL,
                product_name VARCHAR(200) NOT NULL,
                product_content TEXT NOT NULL,
                product_price DECIMAL(10,2) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'published',
                idempotency_key VARCHAR(80) NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE KEY uq_merchant_product_sku (product_id),
                UNIQUE KEY uq_merchant_publish_retry (user_id,idempotency_key),
                INDEX idx_merchant_product_owner (user_id,created_at),
                INDEX idx_merchant_product_merchant (merchant_id,status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
            cur.execute(f'''CREATE TABLE IF NOT EXISTS {RELATION_TABLE} (
                post_id CHAR(32) NOT NULL,
                product_id VARCHAR(64) NOT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (post_id,product_id),
                INDEX idx_merchant_relation_product (product_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
        conn.commit()


def _merchants(cur, user_id, *, lock=False):
    # OpenID is the only identity join. Never accept an OpenID/user ID from a payload.
    cur.execute("""SELECT m.id,m.merchant_type,m.jump_type,m.shop_link FROM merchant_certification m
        JOIN miniprogram_user u ON BINARY u.openid=BINARY m.wechat_id
        WHERE u.id=%s AND u.status='active' AND m.status='active' ORDER BY m.id""" + (' FOR UPDATE' if lock else ''), (user_id,))
    return list(cur.fetchall())


def identity(user_id):
    ensure_tables()
    with _connect() as conn:
        with conn.cursor() as cur:
            merchants = _merchants(cur, user_id)
    return {'user_id': user_id, 'is_merchant': bool(merchants), 'merchants': merchants}


def _merchant(cur, user_id, merchant_id=None, *, lock=False):
    merchants = _merchants(cur, user_id, lock=lock)
    if merchant_id is not None:
        merchants = [merchant for merchant in merchants if merchant['id'] == merchant_id]
    if not merchants:
        raise MerchantForbidden('尚未通过商家认证，或所选商家不属于当前用户')
    if len(merchants) > 1:
        raise ValueError('当前用户有多个认证商家，请传 merchant_id')
    return merchants[0]


def validate_publish(payload):
    if not isinstance(payload, dict):
        raise ValueError('请求体必须是 JSON 对象')
    allowed = {'image_ids', 'product_name', 'product_content', 'product_price', 'merchant_id'}
    if set(payload) - allowed:
        raise ValueError('包含不支持的字段；用户身份由登录态读取')
    data = {}
    for key, maximum in [('product_name', 200), ('product_content', 20000)]:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ValueError(f'{key} 必须为1至{maximum}字的非空文本')
        data[key] = value.strip()
    ids = payload.get('image_ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 9 or any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{32}', v) for v in ids) or len(set(ids)) != len(ids):
        raise ValueError('image_ids 须为1至9个不重复的已上传图片ID（32位字符串）')
    data['image_ids'] = ids
    raw_price = payload.get('product_price')
    if isinstance(raw_price, bool) or raw_price is None:
        raise ValueError('product_price 格式不正确')
    try:
        price = Decimal(str(raw_price))
        if not price.is_finite() or price < 0 or price > Decimal('99999999.99') or price.as_tuple().exponent < -2:
            raise ValueError('价格须为非负数，最多两位小数且不超过99999999.99')
    except (InvalidOperation, ValueError):
        raise ValueError('价格须为非负数，最多两位小数且不超过99999999.99') from None
    data['product_price'] = price.quantize(Decimal('.01'))
    merchant_id = payload.get('merchant_id')
    if merchant_id is not None and (type(merchant_id) is not int or merchant_id <= 0):
        raise ValueError('merchant_id 必须为正整数')
    data['merchant_id'] = merchant_id
    return data


def _image_rows(cur, user_id, image_ids, *, lock=False):
    cur.execute(f"SELECT id,public_url FROM {images.TABLE_NAME} WHERE user_id=%s AND status='active' AND id IN ({','.join(['%s'] * len(image_ids))})" + (' FOR UPDATE' if lock else ''), [user_id, *image_ids])
    found = {row['id']: row for row in cur.fetchall()}
    if len(found) != len(image_ids):
        raise ValueError('部分图片ID不存在或不属于当前用户')
    return [{'file_id': image_id, 'url': found[image_id]['public_url']} for image_id in image_ids]


def upload_image(image_file, user_id):
    ensure_tables()
    with _connect() as conn:
        with conn.cursor() as cur:
            if not _merchants(cur, user_id):
                raise MerchantForbidden('尚未通过商家认证')
    if not image_file:
        raise ValueError('请通过 file 字段上传图片')
    raw = image_file.stream.read(images.MAX_IMAGE_SIZE + 1)
    if not raw or len(raw) > images.MAX_IMAGE_SIZE:
        raise ValueError('图片为空或超过10MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                fmt = image.format
                if image.width * image.height > 20_000_000:
                    raise ValueError('图片像素过大')
                image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning, ValueError):
        raise ValueError('图片无效或像素过大') from None
    if fmt not in {'JPEG', 'PNG', 'WEBP'}:
        raise ValueError('仅支持 JPG、PNG、WEBP')
    extension, content_type = {'JPEG': ('jpg', 'image/jpeg'), 'PNG': ('png', 'image/png'), 'WEBP': ('webp', 'image/webp')}[fmt]
    item = images.upload_moment_image(FileStorage(stream=io.BytesIO(raw), filename=f'product.{extension}', content_type=content_type), user_id=user_id)['item']
    return {'image_id': item['file_id'], 'url': item['url']}


def product_json(row, *, include_content=True):
    result = {key: row.get(key) for key in ('product_id', 'post_id', 'user_id', 'merchant_id', 'product_name', 'status', 'jump_type', 'shop_link', 'merchant_type') if key in row}
    result['product_price'] = format(Decimal(str(row['product_price'])), '.2f')
    value = row['image_ids_json']
    result['image_ids'] = json.loads(value) if isinstance(value, str) else value
    result['images'] = [{'image_id': image_id, 'url': images._build_image_endpoint_url(image_id)} for image_id in result['image_ids']]
    if include_content:
        result['product_content'] = row['product_content']
    for key in ('created_at', 'updated_at'):
        if key in row:
            value = row[key]
            result[key] = value.strftime('%Y-%m-%dT%H:%M:%SZ') if hasattr(value, 'strftime') else str(value)
    return result


def _retry(cur, user_id, key):
    if key:
        cur.execute(f'SELECT * FROM {PRODUCT_TABLE} WHERE user_id=%s AND idempotency_key=%s', (user_id, key))
        return cur.fetchone()
    return None


def publish_product(user_id, payload, *, idempotency_key=None):
    data = validate_publish(payload)
    if idempotency_key is not None and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', idempotency_key):
        raise ValueError('Idempotency-Key 格式不正确，长度为1至80')
    ensure_tables()
    with _connect() as conn:
        with conn.cursor() as cur:
            merchant = _merchant(cur, user_id, data['merchant_id'])
            existing = _retry(cur, user_id, idempotency_key)
            if existing:
                return product_json(existing), False
            media = _image_rows(cur, user_id, data['image_ids'])
            cur.execute('SELECT nickname,avatar_url FROM miniprogram_user WHERE id=%s', (user_id,))
            user = cur.fetchone() or {}
    post_id = uuid.uuid4().hex
    # Reuse the same text/image moderation and callbacks as ordinary community posts.
    check_moment_payload_with_wechat({'user_id': user_id, 'title': data['product_name'], 'content': data['product_content'], 'images': media}, content_id=post_id)
    product_id = 'SKU' + uuid.uuid4().hex.upper()
    now = moments._now()
    with _connect() as conn:
        with conn.cursor() as cur:
            _merchant(cur, user_id, merchant['id'], lock=True)
            existing = _retry(cur, user_id, idempotency_key)
            if existing:
                return product_json(existing), False
            _image_rows(cur, user_id, data['image_ids'], lock=True)
            cur.execute(f'''INSERT INTO {PRODUCT_TABLE} (product_id,user_id,merchant_id,post_id,image_ids_json,
                product_name,product_content,product_price,idempotency_key,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (product_id,user_id,merchant['id'],post_id,dumps(data['image_ids']),data['product_name'],data['product_content'],data['product_price'],idempotency_key,now,now))
            cur.execute(f'''INSERT INTO {moments.TABLE_NAME} (id,user_id,category_code,category_name,category_color,
                title,content,images_json,visibility,author_name,author_avatar,status,created_at,updated_at)
                VALUES (%s,%s,'PRODUCT','商家商品','#D8EEDB',%s,%s,%s,'public',%s,%s,'active',%s,%s)''',
                (post_id,user_id,data['product_name'][:80],data['product_content'],dumps(media),user.get('nickname'),user.get('avatar_url'),now,now))
            cur.execute(f'INSERT INTO {RELATION_TABLE} (post_id,product_id,created_at) VALUES (%s,%s,%s)', (post_id,product_id,now))
            cur.execute(f'SELECT * FROM {PRODUCT_TABLE} WHERE product_id=%s', (product_id,))
            row = cur.fetchone()
        conn.commit()
    return product_json(row), True


def list_my_products(user_id, *, page=1, page_size=20):
    try:
        page, page_size = int(page), int(page_size)
    except (ValueError, TypeError):
        raise ValueError('分页参数不合法') from None
    if not 1 <= page <= 1000000 or not 1 <= page_size <= 100:
        raise ValueError('分页参数不合法')
    ensure_tables()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) AS n FROM {PRODUCT_TABLE} WHERE user_id=%s', (user_id,))
            total = cur.fetchone()['n']
            cur.execute(f'SELECT * FROM {PRODUCT_TABLE} WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s', (user_id,page_size,(page-1)*page_size))
            rows = cur.fetchall()
    return {'items': [product_json(row) for row in rows], 'total': total, 'page': page, 'page_size': page_size}


def product_detail(product_id):
    ensure_tables()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f'''SELECT p.*,m.jump_type,m.shop_link,m.merchant_type FROM {PRODUCT_TABLE} p
                JOIN merchant_certification m ON m.id=p.merchant_id AND m.status='active'
                JOIN miniprogram_user u ON u.id=p.user_id AND u.status='active' AND BINARY u.openid=BINARY m.wechat_id
                JOIN {moments.TABLE_NAME} post ON post.id=p.post_id AND post.status='active' AND post.visibility='public'
                WHERE p.product_id=%s AND p.status='published' ''', (product_id,))
            row = cur.fetchone()
    if not row:
        raise LookupError('商品不存在、已停用或关联帖子不可见')
    return product_json(row)


def bind_product(user_id, product_id, payload):
    if not isinstance(payload, dict) or set(payload) != {'post_id'} or not isinstance(payload['post_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', payload['post_id']):
        raise ValueError('请填写有效的 post_id')
    ensure_tables()
    with _connect() as conn:
        with conn.cursor() as cur:
            # Merchant lock comes first, same order as publishing/revocation.
            active_merchants = _merchants(cur, user_id, lock=True)
            cur.execute(f'SELECT * FROM {PRODUCT_TABLE} WHERE product_id=%s AND user_id=%s AND status=%s FOR UPDATE', (product_id,user_id,'published'))
            product = cur.fetchone()
            if not product or product['merchant_id'] not in {row['id'] for row in active_merchants}:
                raise MerchantForbidden('无权绑定此商品或商家认证已停用')
            cur.execute(f"SELECT id FROM {moments.TABLE_NAME} WHERE id=%s AND user_id=%s AND status='active' FOR UPDATE", (payload['post_id'],user_id))
            if not cur.fetchone():
                raise MerchantForbidden('只能绑定自己有效的社区帖子')
            cur.execute(f'INSERT IGNORE INTO {RELATION_TABLE} (post_id,product_id,created_at) VALUES (%s,%s,%s)', (payload['post_id'],product_id,moments._now()))
        conn.commit()
    return {'product_id':product_id,'post_id':payload['post_id'],'user_id':user_id}


def attach_products_to_moments(rows):
    if not rows:
        return
    ensure_tables()
    ids = [row['id'] for row in rows]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f'''SELECT r.post_id AS linked_post_id,p.*,m.jump_type,m.shop_link,m.merchant_type
                FROM {RELATION_TABLE} r JOIN {PRODUCT_TABLE} p ON p.product_id=r.product_id AND p.status='published'
                JOIN merchant_certification m ON m.id=p.merchant_id AND m.status='active'
                JOIN miniprogram_user u ON u.id=p.user_id AND u.status='active' AND BINARY u.openid=BINARY m.wechat_id
                JOIN {moments.TABLE_NAME} original ON original.id=p.post_id AND original.status='active' AND original.visibility='public'
                WHERE r.post_id IN ({','.join(['%s']*len(ids))}) ORDER BY p.id''', ids)
            links = cur.fetchall()
    grouped = {}
    for link in links:
        grouped.setdefault(link['linked_post_id'], []).append(product_json(link, include_content=False))
    for row in rows:
        row['products'] = grouped.get(row['id'], [])
