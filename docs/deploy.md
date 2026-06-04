# Chongxi Git Deployment

目标：以后执行“提交并部署”时，使用 Git 同步代码，服务器只做 `git pull`、编译检查、重启和健康检查。

## 一次性服务器初始化

前提：本地改动已经提交并推送到 `origin/main`。

```bash
bash scripts/bootstrap_chongxi_git.sh
```

这个脚本会：

- 备份当前 `/home/admin/projects/chongxi`
- 将远端目录初始化为 Git worktree
- 从 `https://github.com/yougurt-cui/chongxi.git` 拉取 `main`
- 保留远端生产配置 `vendor/csv_mysql_labeling/config/config.yaml`
- 保留运行目录和图片目录

## 日常部署

本地提交并推送后执行：

```bash
bash scripts/deploy_chongxi_git.sh
```

这个脚本会在服务器上：

- `git fetch`
- `git pull --ff-only origin main`
- 运行关键 Python 文件编译检查
- 重启 Flask 主服务
- 确认 `15386`、`8501`、`8502` 端口
- 检查 `cat-food-compare.html`
- 检查品牌接口返回数量

## 约束

不要提交：

- `vendor/csv_mysql_labeling/config/config.yaml`
- `.env`
- `.venv/`
- `node_modules/`
- `var/`
- 本地数据、图片、日志、Excel/CSV 原始文件

服务器只保留生产配置和运行数据，不直接修改业务代码。业务代码从本地提交到 Git 后再由服务器 pull。
