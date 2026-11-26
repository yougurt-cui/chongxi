# 宠物参考图生图接口

登录鉴权沿用小程序 Bearer token，任务仅允许所属账号查询。

## 创建

`POST /api/miniprogram/image-generations`，multipart/form-data：

- `image`：一张 JPG/PNG，≤10MiB，宽高均 >14px，总像素 ≤3600 万，宽高比 1:16–16:1。实际解码检查，自动转正、透明底填白并缩至最长边 2048px。
- `prompt`：1–2000 个 Unicode 字符，去除首尾空白，2000 为产品限制。
- `request_id`：16–64 位字母、数字、下划线或连字符。同账号相同 ID 和输入只创建一次；重试必须复用 ID。

202 响应：`{"ok":true,"item":{"id":"32位任务ID","status":"pending","image_url":null,"error":null}}`。

## 查询

`GET /api/miniprogram/image-generations/{id}`，返回相同结构，状态为 `pending`、`generating`、`succeeded` 或 `failed`。成功的 `image_url` 为现有图片存储地址；失败的 `error` 可展示。查询其他账号的任务返回 404。

错误：400 输入不合法；401 未登录；404 无此任务；429 当前账号已有任务或进程队列满；503 配置缺失/服务暂不可用。客户端退出页面不会取消服务端任务。

## 运行配置

沿用 `get_ark_image_config()`：`ARK_API_KEY`（或 `VOLCENGINE_ARK_API_KEY`）、`ARK_BASE_URL`、`ARK_IMAGE_MODEL`。可用 `MINIPROGRAM_IMAGE_API_KEY` 单独配置参考图生图密钥，未配置则沿用共享密钥。小程序参考图生图独立使用 `MINIPROGRAM_IMAGE_MODEL`，默认模型为 `doubao-seedream-5-5-261028`；共享 `ARK_IMAGE_MODEL` 不受此次切换影响。参考图以 JPEG data URI 发送到 Ark `/images/generations`，请求 2048x2048 单图、b64_json 返回；5.5 显式请求 JPEG，不发送其不支持的 `sequential_image_generation`、`stream` 参数，不启用图层拆分；结果转 JPEG 后通过现有 moment image 存储服务持久化。需要已有 MySQL、图片存储配置及 Pillow/requests 依赖。

首次请求创建 `miniprogram_image_generation` 表，数据库账号需建表权限。任务记录在 MySQL；执行队列在进程内，每进程 2 个工作线程、最多 4 个运行/等待任务；数据库唯一索引限制每账号一个活跃任务。部署重启会中断内存队列，不自动重放有费用的模型请求；创建或查询时将该账号超过 10 分钟的活跃任务标记失败，用户可重试。多进程部署的总并发为进程数乘以 2。

验证：`python -m pytest tests/test_miniprogram_image_generation.py -q`。测试覆盖真实图片解析、Flask 上传/查询、权限隔离、幂等与工作线程失败释放；MySQL、Ark 和结果存储使用替身，不能代替线上数据库及模型验收。

## 提示词列表

`GET /api/miniprogram/image-generations/prompts`，公开只读，无需登录：

```json
{"ok":true,"prompt_limit":2000,"items":[{"id":"felt","name":"毛毡","prompt":"完整提示词…"},{"id":"sketch","name":"素描","prompt":"完整提示词…"}]}
```

提示词保存在 `services/miniprogram_image_prompts.py`。按用户提供的文本保留段落、构图要求及 `--ar 1:1`，去掉聊天排版的 Markdown 加粗与转义符。比例仍由生图接口的 `size=2048x2048` 保证。小程序选择标签后填入完整文本，编辑后的文本通过原有 `prompt` 字段提交，无需传风格 ID。先部署服务端列表接口及 2000 字限制，再发布小程序。

## 乘物宠物徽章转绘

服务端配置 `CHENGWU_API_KEY` 后，提示词列表的 `badge_styles` 返回六个可选风格的 `id` 和 `name`。未配置时返回空数组，小程序只展示原头像功能。该 Key 的分组需要有 `gpt-image-2.6-flare` 权限。

徽章模式向同一创建接口提交 `image`、`style` 和 `request_id`。后端校验预设风格，将参考照片规范化为 JPEG，用本地任务 ID 作为乘物的 `Idempotency-Key`。同一账号重传相同输入及请求编号可找回原任务。收到 429 或提交响应不明时，重试应沿用请求编号。换风格需要新编号。

乘物任务 ID 和下次可查询时间保存在自动创建的 MySQL 表 `miniprogram_image_generation_badge`。状态查询按任务加锁，遵守至少 3 秒及上游 `Retry-After`。后台线程收取结果，进程中断后，客户端查询可继续收取已受理任务。成功后保存 1024×1024 PNG 并返回宠析图片地址。乘物任务有效期为 24 小时。遇到结果未知或图片无效的回执，应先核对乘物用量。
