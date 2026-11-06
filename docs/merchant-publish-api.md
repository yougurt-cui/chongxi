# 商家商品发布与帖子绑定接口

已接入 chongxi 现有 Flask 服务。基础地址：`https://www.chongxi.cloud`。

压缩包原件存放于 `vendor/chongxi_merchant_publish_api.reference.zip`。示例 FastAPI/JWT/演示用户库未单独启动；生产使用现有小程序登录、商家认证、图片存储及社区帖子。

## 登录和商家身份

以下接口除公开商品详情外，均使用小程序现有登录返回的 Token：

```http
Authorization: Bearer <小程序登录Token>
```

不使用管理员令牌，不接受客户端传 user_id 或 OpenID 代替登录。商家身份按登录用户的 OpenID 与后台认证记录 `wechat_id` 精确匹配，仅 `active` 认证有效。后台应通过昵称点选用户回填 OpenID，勿将普通微信号填作 OpenID。

```http
GET /api/merchant/identity
```

```json
{"ok":true,"code":0,"message":"操作成功","data":{"user_id":"32位内部用户ID","is_merchant":true,"merchants":[{"id":1,"merchant_type":"品牌商","jump_type":"web","shop_link":"https://example.com/shop"}]}}
```

未认证用户仍返回200，`is_merchant=false`，`merchants=[]`。一个用户有多条有效认证时，发布请求必须指定其中一个 `merchant_id`。

## 图片上传

```http
POST /api/images/upload
Content-Type: multipart/form-data
```

字段 `file`，单张图片不超过10MB，最多2000万像素，支持 JPG、PNG、WEBP。上传要求已认证商家，复用现有图片存储配置及公开图片读取地址。

```json
{"ok":true,"code":0,"message":"操作成功","data":{"image_id":"32位字符串图片ID","url":"/api/miniprogram/moment-images/32位字符串图片ID"}}
```

**与压缩包示例的差异：image_id、user_id、post_id 均使用当前系统字符串ID，而非数字。** 也可使用当前用户通过现有 `/api/miniprogram/moment-images` 上传取得的 `file_id`。

## 发布商品并自动生成、绑定社区帖子

```http
POST /api/merchant/products/publish
Content-Type: application/json
Idempotency-Key: <可选，1至80位字母数字或_.:-；同一次发布重试请使用相同值>
```

```json
{
  "image_ids":["上传返回的32位字符串ID"],
  "product_name":"手工冻干鸡胸肉",
  "product_content":"精选鸡胸肉，低温冻干",
  "product_price":"39.90"
}
```

- `image_ids`：1至9张，不重复，且必须属于当前登录用户。
- `product_name`：1至200字；`product_content`：1至20000字。
- `product_price`：建议用十进制字符串，0至99999999.99，最多两位小数。
- `merchant_id`：可选正整数；多个认证商家时必填。
- 请求不能包含 user_id、OpenID、status 等额外字段。
- 文本及图片走现有微信内容审核接口。商品、社区帖子和关联在同一事务中写入。图片异步审核沿用现有回调处理。
- `Idempotency-Key` 仅用于同一次请求的重试；使用过的键会返回原商品，不应用于另一款商品。

成功201；幂等重试返回原商品时200。返回 `data` 包含 `product_id`（SKU开头）、`post_id`、`user_id`、`merchant_id`、`image_ids`、`images`、商品名称、内容、字符串价格、状态及UTC时间。

## 绑定到已有帖子

```http
POST /api/merchant/products/<product_id>/bind
Content-Type: application/json

{"post_id":"已有社区帖子的32位ID"}
```

只能绑定当前用户拥有的商品与有效帖子；重复绑定不会新增重复关系。发布时已经自动绑定新帖子，此接口用于把同一商品再关联到其他已有帖子。

返回：`{"ok":true,"code":0,"message":"操作成功","data":{"product_id":"SKU...","post_id":"...","user_id":"..."}}`。

## 我的商品与详情

```http
GET /api/merchant/products/mine?page=1&page_size=20
GET /api/merchant/products/<product_id>
```

“我的商品”需要登录，page≥1、page_size为1至100。`data` 为 `{total,page,page_size,items}`；一个商品绑定多个帖子不会重复计数。

详情为公开接口，仅展示有效商家、有效用户且原始帖子公开可见的已发布商品，并带 `merchant_type`、`jump_type`、`shop_link`。商家停用或原始帖子被隐藏/删除后，公开详情返回404。

现有社区列表、帖子详情增加：

```json
{"post_type":"product","products":[{"product_id":"SKU...","post_id":"...","product_name":"商品名称","product_price":"39.90","image_ids":["..."],"images":[{"image_id":"...","url":"/api/miniprogram/moment-images/..."}],"merchant_id":1,"jump_type":"web","shop_link":"https://example.com/shop"}]}
```

普通未绑定帖子的 `post_type=normal`、`products=[]`。小程序可依据 `products` 展示商品卡片；本次接入仅后端，不修改小程序界面。

## 路径别名

上面的 `/api/merchant/...` 也支持 `/api/miniprogram/merchant/...`。图片上传另支持 `/api/merchant/images/upload` 和 `/api/miniprogram/merchant/images/upload`。

## 错误格式

```json
{"ok":false,"code":403,"message":"尚未通过商家认证","detail":"尚未通过商家认证"}
```

HTTP 400参数错误或图片归属不符；401未登录/登录失效；403商家或帖子权限不足；404商品不可见；500内部错误。

## 测试与运行

新表：主库 `miniprogram_merchant_product`、`miniprogram_merchant_post_product`；复用 `merchant_certification`、`miniprogram_user`、现有图片与社区帖子表。部署时可运行 `services.merchant_product_service.ensure_tables()` 幂等建表。无需安装 FastAPI、JWT 或运行压缩包的 demo seed/token 脚本。

单测：`tests/test_merchant_publish_api.py`。

真实MySQL联调：`CHONGXI_MERCHANT_MYSQL_TEST=1 .venv/bin/python -m pytest -q tests/test_merchant_publish_mysql.py`。测试自动创建唯一 `qa_merchant_publish_*` 数据库、复制所需空表结构，并在结束后删除测试库及临时上传文件；不会创建真实社区测试帖。测试隔离微信外部审核调用，验证其被调用；微信审核端到端结果以真实用户发布时的响应为准。

小程序调用示例：`examples/merchant-publish-miniprogram.js`。
