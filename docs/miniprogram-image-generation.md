# 宠物参考图生图接口

登录鉴权沿用小程序 Bearer token，任务仅允许所属账号查询。

## 创建

`POST /api/miniprogram/image-generations`，multipart/form-data：

- `image`：一张 JPG/PNG，≤10MiB，宽高均 >14px，总像素 ≤3600 万，宽高比 1:16–16:1。实际解码检查，自动转正、透明底填白并缩至最长边 2048px。
- `prompt`：1–300 个 Unicode 字符，去除首尾空白，300 为产品限制。
- `request_id`：16–64 位字母、数字、下划线或连字符。同账号相同 ID 和输入只创建一次；重试必须复用 ID。

202 响应：`{"ok":true,"item":{"id":"32位任务ID","status":"pending","image_url":null,"error":null}}`。

## 查询

`GET /api/miniprogram/image-generations/{id}`，返回相同结构，状态为 `pending`、`generating`、`succeeded` 或 `failed`。成功的 `image_url` 为现有图片存储地址；失败的 `error` 可展示。查询其他账号的任务返回 404。

错误：400 输入不合法；401 未登录；404 无此任务；429 当前账号已有任务或进程队列满；503 配置缺失/服务暂不可用。客户端退出页面不会取消服务端任务。

## 运行配置

沿用 `get_ark_image_config()`：`ARK_API_KEY`（或 `VOLCENGINE_ARK_API_KEY`）、`ARK_BASE_URL`、`ARK_IMAGE_MODEL`。默认模型为 `doubao-seedream-4-0-250828`。参考图以 JPEG data URI 发送到 Ark `/images/generations`，请求 2048x2048 单图、b64_json 返回；结果转 JPEG 后通过现有 moment image 存储服务持久化。需要已有 MySQL、图片存储配置及 Pillow/requests 依赖。

首次请求创建 `miniprogram_image_generation` 表，数据库账号需建表权限。任务记录在 MySQL；执行队列在进程内，每进程 2 个工作线程、最多 4 个运行/等待任务；数据库唯一索引限制每账号一个活跃任务。部署重启会中断内存队列，不自动重放有费用的模型请求；创建或查询时将该账号超过 10 分钟的活跃任务标记失败，用户可重试。多进程部署的总并发为进程数乘以 2。

验证：`python -m pytest tests/test_miniprogram_image_generation.py -q`。测试覆盖真实图片解析、Flask 上传/查询、权限隔离、幂等与工作线程失败释放；MySQL、Ark 和结果存储使用替身，不能代替线上数据库及模型验收。
