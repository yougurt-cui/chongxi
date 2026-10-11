# 软便任务测试说明

本次在 `feature/soft-stool-consultation` 实现软便咨询首期。未改评分模型、未部署、无需新增数据库字段；状态和版本保存在现有聊天 JSON 中。

## 已实现

- 每轮两条回复：后端生成事实卡；规则机制与 LLM 解释、追问或最终行动建议。
- 主粮、罐头、零食分别识别；当前粮与原粮分开，用户纠正会更新上下文。
- 按饮食、症状、整体状态、历史与疾病分组追问；黏液与便血分开，未知不当作否认，无法回答时有限信息收尾。
- 伴随风险先于产品查询；缺表、缺保证值和模型故障可降级。营养差异只比较同口径的标示保证值。
- 支持话题暂停、恢复、结论后追问；逐轮保存两条消息及顺序、事实来源、规则版本。

## 自动测试

在后端仓库目录执行：

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_soft_stool*.py' -v
.venv/bin/python -B -m unittest discover -s tests -p 'test_miniprogram_chat_service.py' -v
.venv/bin/python -B -m unittest discover -s tests
```

最后显示 `OK` 才算通过。新增验收场景在 `tests/test_soft_stool_consultation.py`，不连接生产库、不调用收费模型。

## 本地接口联调

本机独立 MySQL 需要运行，配置为 `127.0.0.1:13306`、`chongxi_dev`。执行：

```bash
.venv/bin/python -B scripts/smoke_soft_stool_local.py
```

脚本只允许连接上述本地开发库，通过 Flask 测试客户端执行真实 API、认证、宠物归属和聊天存储；产品和模型使用离线替代。它创建临时测试用户，检查五轮咨询、消息顺序、结论后追问、话题切换、风险优先及未登录访问，并清理该测试用户的记录。无需启动额外 HTTP 服务。

## 真实模型和前端联调

需补开发模型 Key，以及产品、配方评分和参考池开发数据。用接口调试工具调用本地服务，带本地用户 `Bearer token`，先创建宠物档案及会话，再连续发送消息：

- `POST /api/miniprogram/chat/conversations`，请求 `{"pet_id":"测试宠物ID"}`。
- `POST /api/miniprogram/chat/conversations/{会话ID}/messages`，请求 `{"message":"用户的话"}`。
- `GET /api/miniprogram/chat/conversations/{会话ID}/messages`，检查历史记录。

| 场景 | 验收预期 |
|---|---|
| BK34 软便咨询 | 数值取自开发数据；碳水、蛋白等机制按规则解释，不确定因果 |
| 没换主粮但加罐头；连续短答 | 记录添加事件，结合上一问理解“没有”“两天”“不知道”，不重复问已知项 |
| 饮食变化晚于软便；修改当前粮 | 保留时间反证，刷新当前产品证据，不保留旧比较结果 |
| 产品缺数据、模型异常、伴随风险 | 不编造数据；风险回复不依赖产品查询和模型 |
| 完成咨询与切换话题 | 有下一步、观察与复评条件；可暂停、恢复；消息按用户、事实、解释排列 |

病历补充动作通过 `message.result.actions` 提供：`{"type":"medical_record","label":"补充病历","pet_id":"…"}`。前端需将其接到病历入口；本次未修改小程序 UI。首期不接具体选粮推荐。

离线测试通过不代表真实 LLM 回复质量或生产 BK34 数据已验收。并发幂等、完整历史事件和可配置临床风险分级仍属后续工作。
