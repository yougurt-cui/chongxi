# 投稿自动处理与结果通知

本地实现需求 18–20；21 的部分原料评分与二次计算暂缓。18、19 归投稿模块，20 归 AI 管家。代码已完成本地验收并上传个人开发版候选包；尚未部署生产后端、提交审核或正式发布。真实包装图已通过完整 12 节点处理及手机通知点击验收；Mac 已能通过自动预览加载开发包，通知入口的版本选择仍未确认。

## 有前端同事协作时，需要申请什么

建议管理员将后端联调人员自己的微信加入本小程序项目成员，角色选择“开发者”。当前工程 AppID 是 `wxec9a56b911900aa0`，应先与后台账号核对。只加体验成员不够：体验成员只能使用体验版；项目开发者还可登录后台、使用开发者工具和开发版，并具有开发设置权限，且已包含体验者权限，无需再重复申请体验成员。

微信官方权限表把“开发设置”（域名等）与“开发管理”（提交审核、发布、回退）分开。后端联调无需接管管理员，也不必为了调试申请正式发布/回退权限；这些继续由前端和发布负责人负责。来源：[小程序协同工作和发布](https://developers.weixin.qq.com/miniprogram/dev/framework/quickstart/release.html)。

建议交接清单：

| 向谁索取/协作 | 具体内容 | 用途 |
| --- | --- | --- |
| 小程序管理员 | 将自己的微信号添加为项目开发者；如已有测试小程序也开通对应开发者资格 | 自行扫码登录开发者工具、开发版预览及真机联调 |
| 前端同事 | 联调分支、当前体验版对应提交和版本号、体验版入口、当前连接的后端地址 | 对齐代码和环境，避免以为在测新接口实际运行旧包 |
| 前端/管理员 | 把测试 HTTPS 域名加入实际用到的 request、uploadFile、downloadFile 等合法域名 | 手机真机访问测试后端；开发工具跳过校验不代表正式客户端可用 |
| 模板管理人员 | 在同一 AppID 下添加合适订阅模板，提供模板 ID、完整字段编号/类型及可用状态 | 配置第 19 项，不能只提供模板标题或一张通知效果图 |
| 后端配置负责人 | 同一 AppID 的 AppSecret 已安全配置到测试后端；正式后端沿用正式配置 | 服务端获取微信调用凭据，不把 AppSecret 加入小程序代码或群聊 |
| 管理员/运维 | 需要时配置测试及正式后端的公网出口 IP 白名单 | 避免微信凭据接口拒绝服务器请求 |
| 运维/后端负责人 | 独立测试后端、测试数据库、查看日志与重启测试服务的能力、测试宠物和投稿数据 | 自主排查调度/模型/通知，避免开发测试消耗生产任务 |

开发者个人开发版可以独立调试；团队体验版通常只选一份，仍应约定由谁更新，避免多人相互覆盖。`trial` 仅选择通知跳转体验版，不会隔离后端、数据库或真实通知。无需索取管理员登录密码、管理员微信或为了本功能重置现有 AppSecret；需管理员验证的后台操作由管理员配合完成。

可以转发的申请文字：

> 后续我要独立做后端联调和真机调试，请把我的微信添加为小程序 `wxec9a56b911900aa0` 的项目开发者，不是仅添加体验成员。请前端同步联调分支、体验版版本/入口及测试后端地址，并协助配置测试 HTTPS 合法域名。第 19 项还需要同一小程序的订阅模板 ID 和完整字段信息；AppSecret 请由负责人安全配到测试后端，并按需要设置服务端 IP 白名单。正式审核、发布和回退仍由原负责人负责。

## 配置位置总览

| 内容 | 配在哪里 | 本项目具体位置 |
| --- | --- | --- |
| 后台执行开关、并发、补扫间隔 | 后端根目录 `.env` | 按部署脚本默认目录：服务器 `/home/admin/projects/chongxi/.env` |
| AppID、AppSecret、通知模板 ID、字段映射、跳转版本 | 同一份后端 `.env` | 与上面相同；AppSecret 只留在服务端 |
| 订阅模板的选择/申请 | 微信公众平台对应小程序账号 | 登录 `https://mp.weixin.qq.com/`，核对 AppID 后进入“订阅消息”，从可用模板库添加到账号模板列表 |
| 微信开发者凭据与 IP 白名单 | 同一小程序的开发设置 | 获取/复用 AppSecret，按需要把后端实际公网出口 IP 加入白名单 |
| 小程序 AppID | 小程序工程配置 | `xiaochengxu/project.config.json`，当前为 `wxec9a56b911900aa0` |
| 小程序请求的后端地址 | 小程序工程 `config.js` | `apiBaseUrl` 当前为 `https://chongxi.cloud` |

本机后端文件是 `/Users/dingaimin/workspace/chongxi/chongxi/.env`，与服务器 `.env` 彼此独立。修改本机配置不会影响线上。已只读核对服务器实际目录及配置；本轮没有改写生产代码或配置。

模板 ID 由后端投稿接口返回，小程序在用户点击时读取 `result_notification.template_id` 并调用 `wx.requestSubscribeMessage`。因此不用在小程序 JS 中再写一份模板 ID；但必须发布包含新订阅按钮和回调处理的小程序代码。

配置样例：[.env.submission.example](../.env.submission.example)。只把需要的键合并到现有 `.env`，不能整份覆盖并丢掉数据库、模型、管理员令牌和会话密钥；已有同名项应修改原项，避免重复键。

## 开启后台处理

当前单应用进程部署，在后端环境配置：

```dotenv
CHONGXI_PIPELINE_WORKER=1
CHONGXI_PIPELINE_CONCURRENCY=1
CHONGXI_PIPELINE_SWEEP_SECONDS=60
```

`WORKER=1` 启用，`0` 停用；`CONCURRENCY=1` 表示一次处理一份投稿，投稿内部仍按依赖执行；`SWEEP_SECONDS=60` 仅为漏唤醒、到期重试等的补查间隔，正常新任务会立即唤醒，不等 60 秒。

它作为现有 Flask 后端的后台线程运行，不需要在微信后台开启，也不需要再配置 cron、MQ 或单独启动一个 HTTP 服务。沿用现有 MySQL、OCR/模型凭据和 `MINIPROGRAM_ADMIN_TOKEN`；自动化不会替代这些既有业务依赖。

默认不开启，避免导入模块、单元测试或管理命令意外消费数据库任务。配置后随 Flask 应用启动；开发热重载仅由实际服务子进程启动。不要在多个 Web worker 中随意同时启用；多实例部署应明确唯一调度实例，再单独验证调度与故障行为。

任务保存成功后发送进程内唤醒，执行器从 MySQL 取任务，成功后立即推进下一节点。提交中的投稿在事务完成前不可执行；进程内唤醒丢失则由低频补扫恢复。初始化时为已有 `pipeline_task_node` 增加 claim_token、attempt_count、next_attempt_at，不另建一套任务系统。

认领与取消使用数据库状态条件；节点回报只接受当前执行令牌。`POST /api/orchestrator/dispatch-claim` 的 job 新增 claim_token，外部执行器回报 `/api/orchestrator/tasks/{task_id}/nodes/{node_code}/result` 时必须带回此字段。同步执行接口已接好令牌。升级外部执行器与服务端时需协调，不能继续发送旧轮次的无令牌回报。

只对明确未执行业务的限流和市场重建锁忙安排最多 3 次尝试，间隔 15、30 秒，实际由补扫/唤醒接续。其他失败保留原因；原料资料缺失进入 need_review。中断请求有副作用不确定性，启动/补扫发现失去持有连接的 worker 后，标记待核验，保留已完成输出。通过既有后台核验及重跑本节点恢复，不从头重做整份投稿。

新字段：processing_status、processing_stage、processing_message、result_available。识别状态和整条计算状态分开；小程序在 OCR 后仍可查看计算进度。市场重建仍为全量，锁忙排队；结构标签删除与写入已合为单事务。

## 微信通知配置

先在对应小程序管理后台取得可用订阅模板，再按模板真实字段配置：

- `WECHAT_MINIPROGRAM_APPID`、`WECHAT_MINIPROGRAM_APP_SECRET`：现有微信服务端配置。
- `WECHAT_FOOD_RESULT_TEMPLATE_ID`：实际模板 ID。
- `WECHAT_FOOD_RESULT_TEMPLATE_FIELDS`：JSON，真实微信字段键映射到 product、status、completed_at。product 必须对应 thing 字段，status 对应 phrase/thing，completed_at 对应 time/date。不能照抄其他模板字段编号。
- `WECHAT_MINIPROGRAM_STATE`：formal（默认）、trial 或 developer，与本次发布环境匹配。

假设账号中选好的模板真实字段为“产品名称 thing1、处理状态 phrase2、完成时间 time3”，服务器填写：

```dotenv
WECHAT_MINIPROGRAM_APPID=wxec9a56b911900aa0
WECHAT_MINIPROGRAM_APP_SECRET=填写该小程序的真实AppSecret
WECHAT_FOOD_RESULT_TEMPLATE_ID=填写该账号的真实模板ID
WECHAT_FOOD_RESULT_TEMPLATE_FIELDS='{"thing1":"product","phrase2":"status","time3":"completed_at"}'
WECHAT_MINIPROGRAM_STATE=trial
```

上述字段编号只是示例，必须与该模板实际字段相同。`product` 为投稿产品名，`status` 为“解析完成”，`completed_at` 为北京时间。此外支持 analysis_type（thing，固定“食品配料解析”）及 result_summary（thing，产品名加“解析完成”，总长不超过 20 字），用于已选用的真实 AI 分析模板。模板还有其他必填字段时，必须先扩展代码，不能随便找一个通知模板就直接用。没有合适的模板时按微信后台规则申请，不能用不相干的业务类目模板。

AppSecret 和模板需属于工程绑定的同一 AppID；已有服务端登录凭据可复用，不要为了本功能重置它。后台获取稳定 access_token，接收者 openid 取自当前登录用户，均无需手工填写。若微信返回 40164，需要在该账号 IP 白名单加入服务端实际出口 IP。请求合法域名继续复用已有后端地址，新增服务器出站访问目标为 `api.weixin.qq.com:443`。

官方来源：[订阅消息配置步骤](https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/subscribe-message.html)、[服务端发送及字段限制](https://developers.weixin.qq.com/miniprogram/dev/server/API/mp-message-management/subscribe-message/api_sendmessage.html)、[稳定版凭据及 IP 白名单错误说明](https://developers.weixin.qq.com/miniprogram/dev/server/API/mp-access-token/api_getstableaccesstoken.html)。已核实公开文档；2026-10-17 经用户授权在账号后台开通订阅消息并选用模板，配置见下方最新记录。

模板 ID 或字段映射缺失时隐藏订阅入口；服务端凭据缺失时不会发送。用户在投稿详情点击“结果生成后通知我”时调起微信授权；拒绝不影响上传。页面通过当前登录身份关联投稿，不接受客户端指定接收者 openid。

`POST /api/miniprogram/food-submissions/{id}/result-subscription`，JSON decision 为微信回调的 accept/reject/ban/filter。

首次完整流水线 done、配料有效且投稿仍有效时通知一次；点击跳转该投稿详情。结果晚于授权或授权晚于结果均可由补扫接续。仅生成一份订阅发送记录，重复回报/再次订阅不会重发已发送的通知；43101 标为 declined，其他明确错误记为 failed，传输超时或进程中断记为 unknown，不自动重发。发送失败不回滚解析。

当前不实现订阅额度同步或微信事件回调核验；unknown 必须由运营核验。阶段成功和审核收录不能用完成通知混淆。若未来实施 21，应另定义部分结果事件及文案，不能直接沿用完整结果判断。

## 上线前、上线时与改配置后

| 环境 | Worker | 并发/补扫 | 通知跳转 | 小程序后端地址 |
| --- | --- | --- | --- | --- |
| 只测聊天展示、不消费任务 | 0 | 1 / 60 | 可不配通知 | 对应开发后端 |
| 隔离环境完整联调 | 1 | 1 / 60 | trial（体验版） | 测试后端与测试数据库 |
| 正式环境 | 1 | 先保持 1 / 60 | formal（正式版） | `https://chongxi.cloud` |

`trial/formal` 只控制通知点击后打开哪个小程序版本，不会自动切换服务器或数据库，也不会禁止真实发送。测试数据隔离要靠独立的后端/数据库与小程序 apiBaseUrl。启用 Worker 会处理现有队列中可执行的任务，正式开启前需核对积压任务。

上线顺序：

1. 将后端和小程序改动分别审阅、合入对应发布分支。当前改动仍在本地 feature/submission-automation；现有部署脚本默认拉后端 origin/main，不会自动发布本地未提交的代码。
2. 在微信后台选定模板、取得字段信息及同一小程序的服务端凭据；合并进目标服务器的 `.env`。
3. 部署后端并重启现有 Flask 服务；`.env` 在启动时读取，修改后必须重启。单纯编辑文件不会让已运行进程更换开关/凭据。进程管理器已导出的同名环境变量优先于 Python 读取的 `.env`，检查命令必须在同一运行环境执行。
4. 在开发者工具用对应 AppID 上传小程序体验版，验证订阅授权、处理进度、收到通知和点击详情；正式发布前切 formal 并重启后端，再按微信审核发布流程发布小程序。
5. 上线后无需再配第二套执行开关，继续使用正式 `.env`；后续改并发、间隔或模板参数仍需重启。

按当前仓库发布流程，代码合入 main 后在本机执行 `bash scripts/deploy_chongxi_git.sh`。该脚本在默认服务器目录 source `.env` 后重启 Flask，且包含拉取代码与检查步骤；这是完整部署命令，不是只读取配置的命令，本轮未执行。小程序发布是独立步骤，后端部署不会自动发布小程序。

本地服务由父目录 `local-dev/manage.py` 管理：在 `/Users/dingaimin/workspace/chongxi` 运行 `python3 local-dev/manage.py stop`，再运行 `python3 local-dev/manage.py start` 即读取本机 `.env` 重启。已修正该本地管理脚本对带引号 JSON 值的读取，避免模板映射带着外层引号进入环境后解析失败；未修改现有密钥和开关。

## 配好后怎么检查

在目标后端目录运行只读检查，不连接微信或数据库，不启动 Worker，也不打印密钥：

```bash
.venv/bin/python scripts/check_submission_config.py
```

上线验收配置应满足时可加严格参数，配置缺失则退出非零：

```bash
.venv/bin/python scripts/check_submission_config.py --require-worker --require-notifications
```

这只能证明当前进程环境里的配置齐全，不能证明线上服务已重启、微信模板可用或消息已送达。实际再提交一份测试投稿，确认状态自动从排队到节点执行/待核对/完成；同意订阅后查看微信服务通知，并点击回到同一投稿详情。

服务端日志按现有部署脚本位于 `/home/admin/projects/chongxi/var/flask.log`。通知结果在 MySQL `miniprogram_submission_notice`：waiting 等待结果/凭据，sending 已认领发送，sent 微信接口返回成功，declined 用户拒收/无订阅权限，failed 平台明确报错，unknown 发送结果不确定。sent 是接口成功记录，不等同用户已阅读。只读查看 status、error_code、attempt_count、updated_at 即可排查，不需要导出用户 openid 或密钥。

## 验证

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python scripts/smoke_submission_automation.py
```

SQL smoke 只允许本机 13306 的 chongxi_dev 数据库身份，创建并清理独立测试记录；业务接口和微信发送均使用离线桩。运行时停止其他消费同一开发库的执行器。它验证持久化和调度，不验证真实产品计算正确率。

小程序仓库运行：

```bash
node scripts/check-submission-automation.cjs
node scripts/check-guardian-p0.cjs
node scripts/check-food-submissions.cjs
node scripts/check-contracts.cjs
```

真实模型回复已在开发者工具核对加粗、短段落和观察提醒，手机已确认真实通知及详情跳转。正式环境仍需在部署后做一次验收；本地通过不等于已经上线。

## 2026-10-17 最终本地验收

- 后端 **444 passed，41 subtests passed**；真实 MySQL 烟测通过并发认领、中断恢复、过期回报拒绝、依赖执行、延迟重试、撤销、用户隔离、通知只发送一次及不确定结果不重发。烟测期间已停用其他本地执行器，记录已清理。
- 小程序四组离线检查通过；干净候选包 **0.0.4-rc1017** 已通过微信 CLI 编译上传到本人个人开发版，总包约 1.0 MB、主包约 692 KB。没有选择或覆盖团队体验版，没有提交审核或发布。
- 正式源码 `config.js` 为 `https://chongxi.cloud`，`isMock=false`，域名校验已恢复。`notice1017` 诊断配置、页面诊断文字均已删除。手机此前扫码的 0.0.3 联调包仍显示诊断文字，需要显式加载新包后才会改变；正式候选包连接生产后端，不包含本地测试数据。
- 真实包装样本「爱肯拿 / 牧场盛宴」通过本地客户上传接口保存，真实 Qwen OCR 创建新的识别来源；命中重复产品后按既有后台审核接口确认标准产品，再由 Worker 自动运行剩余节点。最终 12 节点全部 success、task_status=done，没有用合成输出或 skipped 冒充完整计算。
- 上述真实投稿完成后，开发者工具真实请求订阅，用户点允许；微信返回 errcode=0，通知表 sent、attempt_count=1。用户明确确认收到“牧场盛宴解析完成”，手机点击后投稿详情和完整配料正确。
- 第二份真实样本「京东京造 / 单一肉源低敏」已完成 OCR、品牌和配方预检，停在重复产品待审核。此为真实人工审核分支，不会提前发送完成通知；未伪造审核结果或强制完成。
- 管家以真实 deepseek-flash 回复，通过真实云端历史接口和页面历史选择处理器重新加载，在模拟器实际看到重点加粗、短段落、❗观察提醒，无 Markdown 星号外露。标记遵循语义，不要求每条回复凑齐三类。
- 本地模型与产品参考数据使用隔离数据库；只从生产读取产品/原料参考表，没有复制生产用户、宠物、聊天、评论或订单记录。真实测试宠物、投稿和会话仅在本地。
- 21 的部分原料计算和二次结果仍暂缓，未声称已实现。

本轮进一步修复：上传检查也需原子认领及执行令牌；科学特征部分完成必须待审核；重复配方确认后继续剩余计算而非跳过后直接完成；重新计算包含科学特征节点；任务时间统一 UTC；投稿展示优先采用确认后的标准品牌和产品名称；模型样式提示与带编号的语义标记兼容。

### 已配置的真实订阅模板

账号“宠析”，AppID `wxec9a56b911900aa0`。AI问答类目的一次性“AI分析完成提醒”，公共模板编号 74906。现有真实模板可继续用于发布，不需要再申请：

```dotenv
WECHAT_FOOD_RESULT_TEMPLATE_ID=AQFyvm-Rvbp1fMX5O4BUcC1-CCL_NFHYtHSEyH-AjrI
WECHAT_FOOD_RESULT_TEMPLATE_FIELDS='{"thing1":"analysis_type","thing3":"result_summary","time4":"completed_at"}'
WECHAT_MINIPROGRAM_STATE=formal
CHONGXI_PIPELINE_WORKER=1
CHONGXI_PIPELINE_CONCURRENCY=1
CHONGXI_PIPELINE_SWEEP_SECONDS=60
```

上述是**生产最终配置**，尚未写入生产 `.env`。本机仍为 developer，AppSecret 已安全配置且不入库。完成时间按北京时间展示；thing1 为食品配料解析，thing3 为产品名加解析完成。不要重新生成或覆盖现有 AppSecret。

### Mac 微信

本机微信为 4.1.15。此前从通知进入没有加载诊断构建，同一通知在手机上有构建标识与投稿参数；不能因此认定 Mac 不支持通知跳转，也没有证据说明是接口未传 ID。

使用官方「预览 → 自动预览 → PC 自动预览」路径，CLI auto-preview 成功，实际 Mac“宠析”窗口已显示新开发包主页。该调试入口解决了显式加载开发代码包的问题；尚未验证 Mac 从服务通知进入能保持同一开发包及完成详情登录，不能将“自动预览成功”写成“Mac 通知点击通过”。

来源：[PC 小程序测试方法](https://developers.weixin.qq.com/miniprogram/dev/framework/pc/)。手机同 Wi-Fi 调试可使用父目录 `local-dev/phone_gateway.py` 的临时有限路由网关；它不属于正式发布代码，满 60 分钟自动停止。

## 生产发布安排及回退

1. 分别审阅并合入两个仓库的 `feature/submission-automation` 改动。已核对远端最新基线：后端 `6b7d432`、小程序 `776d85b`。当前改动仍在本地，部署脚本拉 main 不会自动拿到这些改动。
2. 生产目录 `/home/admin/projects/chongxi` 存在既有未提交运行补丁：`comment_pipeline/common/cleaner.py`、`comment_pipeline/common/db.py`、`services/orchestrator_service.py`、`tests/test_qwen_ocr_retry.py`、`vendor/csv_mysql_labeling/src/parse_catfood_guarantee.py`，以及其他备份文件。发布负责人先保存 Git diff、所需未跟踪文件、现有 `.env` 和数据库备份，再审阅合并；禁止 reset/clean 或直接覆盖。科学特征状态门禁已纳入本轮代码，另外的评论和 OCR 模型补丁仍需保留。不要直接运行现有 deploy 脚本处理脏工作区。
3. 首次部署后端先保持 `CHONGXI_PIPELINE_WORKER=0`，保留既有数据库、模型、微信凭据；合并真实模板配置。随应用初始化幂等创建通知表和任务新增字段，无需重建全部业务库。检查服务健康、已有投稿/聊天、管理员功能和现有队列；只读核对时没有可执行积压任务，上线时仍需重查。
4. 在约定的测试发布窗口配置唯一执行进程、并发 1、补扫 60 秒，重启后端；目标 `.env` 使用 trial/developer 时只改变通知落地版本，并不隔离生产数据。用专用验收账号提交一份真实包装图，确认审核分支、完整计算、一次通知及点击详情。本人个人开发版不等于团队体验版，切换体验版应与前端发布负责人协调。
5. 正式小程序审核发布前检查其 HTTPS 地址及无诊断文字；与后端配套切 `WECHAT_MINIPROGRAM_STATE=formal` 并重启。在服务器同一运行环境执行 `.venv/bin/python scripts/check_submission_config.py --require-worker --require-notifications`，上线后再检查新任务和通知记录。发布权限/管理员审核流程尚未实际验证。
6. 回退先设置 Worker=0 并重启，阻止接新任务；保存正在执行的节点与通知状态，等待/核验副作用，不盲目重跑或重发 unknown 通知。再按备份恢复代码及必要配置，小程序按平台正常回退流程处理。新增表/列兼容保留，不为代码回退删除任务和通知记录，不直接覆盖上线后新增的业务数据库数据。

可发布候选代码与本地真实验收已准备完成；生产补丁整理、代码合入、配套部署和平台审核发布属于剩余上线步骤。详细发布评估见父目录 `local-dev/releases/requirements18-20-20261017/` 的交付包。
