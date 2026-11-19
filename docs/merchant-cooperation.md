# 商户合作页与线索表单

页面：`web/merchant-cooperation.html`。图片、CSS 和 JS 已内嵌，单文件可直接打开。
独立资源保存在 `web/assets/merchant-cooperation/`；修改 CSS/JS 后须同步内嵌版本。

## 部署与接收

- 将静态页面与本项目 Flask API 部署在同一域名；Nginx 须将 `/api/merchant-leads` 转发至 Flask。
- Flask 已提供 `/merchant-cooperation.html` 路由。
- `POST /api/merchant-leads` 接收 JSON：`company`、`contact`、`phone`、`category`、可选 `message`、布尔 `consent`。
- 手机号为 11 位大陆手机号；经营品类为宠物食品、宠物用品、宠物服务、其他。
- 返回 `201 {"ok": true, "id": "…"}` 才表示成功。验证错误返回 400，过大请求 413，频率限制 429，保存故障 503。
- `MERCHANT_LEADS_DB` 指定 SQLite 文件，默认 `var/merchant-leads.sqlite3`。目录须可写，生产部署请挂载持久化存储并限制文件访问、纳入备份；多实例须使用共享数据库方案，不能使用各容器临时磁盘。
- 相同信息 24 小时内自动去重；每个客户端地址每小时最多新增 10 条。客户端地址采用 Flask 的 `remote_addr`，不直接信任外部 `X-Forwarded-For`。反向代理部署须按实际可信代理配置地址传递，避免全部请求共用代理地址的限额。
- 本地 `file://` 预览不提交个人信息，只提示“信息尚未提交”。静态页面和 API 均部署后才能收集真实线索。

## 查看线索

`GET /api/admin/merchant-leads?limit=100`（最多 500 条），复用现有工作台管理员认证：已登录的工作台账户或 `X-Admin-Token`，令牌来自服务端 `MINIPROGRAM_ADMIN_TOKEN`，不能写入前端。
响应禁用缓存，线索不写入应用日志。记录中包含联系授权版本与提交时间。
当前不自动发送邮件或微信通知；管理员通过接口查看，部署后须安排日常跟进。

## Logo

附件经内置图像编辑工具去掉中文文字和外框，保留蓝色猫脸与放大镜，输出透明 PNG。
`logo.png` 为高分辨率源文件；Figma 中保存了 32/48/64/128/256px 展示规格与可复用主组件。
这是基于所提供 JPG 整理的位图，不是原始矢量文件。
