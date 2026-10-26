# 需求 36：两层知识库定时生产

独立命令执行：来源帖子/评论 → knowledge_base → 社区互证 → kd_problem_guides。复用已有管线，不在 Flask 导入或请求中启动调度器，避免多个 Web worker 重复跑。

```sh
.venv/bin/python scripts/run_knowledge_schedule.py --max-posts 500
.venv/bin/python scripts/run_knowledge_schedule.py --status
```

第一层按帖子 ID 循环扫描并逐篇保存进度；没有 ingest_after 截断，因此旧帖新增、修改、删除评论也会进入指纹检查。每次扫描最多 500 篇，每篇最多 4 批评论（可配置），未变化的帖子和已完成的评论不调用提取模型。达到表尾后下一轮回到开头；旧帖变化的发现延迟取决于帖子总量和扫描预算，不能宣称实时处理。上线后根据运行耗时和积压调整预算或调度频率。

变化主题进入持久化待校验队列；失败不会从队列消失，下轮继续。第二层按 topic/subtopic 内容指纹跳过未变化数据，模型失败保留已有知识卡。没有有效来源的旧主题会把知识卡标记为无有效来源，防止继续召回。正常生产模式固定 llm，不能定时运行 demo 或自动授予专业已验证状态。

本机 flock 与 MySQL GET_LOCK 双重互斥。knowledge_schedule_runs 保存开始/结束、状态、扫描/更新/跳过数量、失败阶段与错误类型；knowledge_schedule_state 保存帖子游标；knowledge_schedule_topics 保存待校验主题。中断的 running 记录在下次取得锁后标为 interrupted。默认失败重试一次；独立主题继续处理，存在失败时退出码 1。日志不保存密钥、原始评论或用户档案。

## 安装调度（Linux/systemd）

在服务器仓库内执行：

```sh
bash scripts/install_knowledge_schedule.sh
systemctl list-timers chongxi-knowledge.timer --no-pager
journalctl -u chongxi-knowledge.service --since today
```

默认每天北京时间 02:00，随机延迟不超过 120 秒；停机错过的运行由 Persistent 补跑。日历通过 KNOWLEDGE_JOB_CALENDAR 修改（systemd 日历格式，必须带明确时区），预算通过 KNOWLEDGE_JOB_MAX_POSTS 修改。安装脚本只启用 timer，不立即全量跑模型。主机 .env 沿用现有模型/数据库配置；systemd 任务最长 12 小时，超时停止后下次从保存的帖子进度继续。

单纯推送或部署 Web 后端不会自动启用生产 timer，首次上线需执行上述安装脚本；撤销调度使用 `systemctl disable --now chongxi-knowledge.timer`。部署脚本包含新增模块的语法检查。离线测试使用本地隔离数据库与模拟模型验证处理逻辑；真实模型问答、小程序联调单独记录，不把模拟测试当作真实模型效果验收。
