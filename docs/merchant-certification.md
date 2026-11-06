# 商家认证

入口：`/pipeline-review.html#merchant`，沿用工作台登录权限（data_admin/member）及现有管理员令牌校验。

管理员录入微信 ID、手机号、商家类型、店铺跳转方式和链接后保存为已认证。支持分页查询、编辑、停用与恢复。商家类型可自由填写；跳转方式支持网页、微信小程序、其他方式。此功能保存认证和店铺资料，不自动验证微信账号归属，也不执行小程序跳转。

数据存储在主库 `merchant_certification`，含创建/修改人员与时间。服务首次使用会幂等建表，也可在部署时执行 `services.merchant_certification_service.ensure_table()`。

接口均位于 `/api/admin/merchant-certifications`：GET 查询、POST 新增、PUT `/<id>` 编辑、POST `/<id>/status` 停用或恢复。所有五项资料必填，手机号支持 11 位大陆手机号及 +86 前缀，网页链接须为完整 HTTP(S) 地址。

验证：`tests/test_merchant_certification.py`；浏览器使用唯一临时记录完成真实数据库新增、编辑、查询及停用/恢复验证后，清除测试记录。

## 按昵称选择微信用户

表单支持按小程序已保存的昵称、手机号或 OpenID 查找用户。管理员查询接口：`GET /api/admin/merchant-certifications/users?q=昵称`，返回最多20个有效用户及 `has_more`。同名用户逐项展示，必须手动选择；选择后微信 ID 回填为该用户的 OpenID，有已保存手机号时也会回填手机号。

选择后保存请求附带 `selected_user_id`，服务端校验用户有效且 OpenID 一致。绑定最终按稳定 OpenID 保存，昵称变化不会改变商家身份。该查询仅使用本项目已登录用户数据，不通过微信昵称查询外部微信账号。
