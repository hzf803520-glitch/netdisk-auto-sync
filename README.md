# 双网盘自动同步中心

本仓库使用用户提供的 Next.js 项目作为唯一主程序。Render 构建时先解压 `deploy-source/app-lite.part00` 至 `app-lite.part04`，再覆盖新增的金山文档 Cookie 读取与增量监听模块；旧版 Python/Go 程序不参与构建或运行。

## 当前能力

- 百度网盘与夸克网盘 Cookie 授权
- 指定转存目录与同步任务
- 使用本人金山文档登录 Cookie 读取只有查看权限的共享文档
- 按文档顺序识别并保存前 500 条有效网盘数据
- 识别新增、改名、提取码变化、原链接变化和删除
- 浏览器变化通知
- 点击“应用变化并增量同步”时只处理变化的数据
- 名称变化只更新记录；新增或链接变化才进入转存
- 删除记录时默认保留网盘中已经转存的文件
- CSV 导出
- Neon PostgreSQL 持久化
- GitHub Actions 每 15 分钟唤醒 Render 检查共享文档

## 金山文档 Cookie 使用

Cookie 属于敏感登录信息，只能填写在自己部署的网站后台，不要发送到聊天、GitHub 或公开页面。系统只使用 Cookie 读取当前账号本来就有查看权限的文档，不会修改原共享文档；保存时使用部署密钥加密。

使用顺序：

1. 在金山文档网页中保持登录，并确认共享文档可以正常查看。
2. 在系统的“共享文档监听”区域填写分享链接和完整 Cookie。
3. 将读取数量设置为 500。
4. 点击“使用 Cookie 读取并保存前 500 条”。
5. 后续点击“重新识别变化”，确认变化后再点击“应用变化并增量同步”。

## Render 环境变量

- `DATABASE_URL`：Neon PostgreSQL 连接地址
- `CREDENTIAL_SECRET`：Cookie 加密密钥；未设置时兼容已有的 `COOKIE_ENCRYPTION_KEY`
- `CRON_SECRET`：定时检查密钥
- `FIRECRAWL_API_URL`：可选，供其他网页文档解析使用
- `SYNC_CONCURRENCY`：同步并发数，默认 3
- `CHROMIUM_PATH`：Docker 镜像内默认为 `/usr/bin/chromium`

## GitHub Actions Secrets

- `APP_URL` 或 `RENDER_URL`：Render 固定网址，例如 `https://xxx.onrender.com`
- `CRON_SECRET`：与 Render 中的 `CRON_SECRET` 保持完全一致

## 部署

仓库包含 `render.yaml`，Render 已配置为 GitHub 提交后自动部署，健康检查地址为 `/api/health`。Docker 镜像包含 Chromium，用于携带 Cookie 打开金山文档并滚动读取动态加载内容。
