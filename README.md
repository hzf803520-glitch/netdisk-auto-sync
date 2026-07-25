# 双网盘自动同步中心

本仓库已使用用户提供的 Next.js 项目完全替换旧版 Python/Go 运行程序。Render 构建时仅解压并运行 `deploy-source/app-lite.part00` 至 `app-lite.part04` 中的应用源码，旧版程序不参与构建或运行。

## 当前能力

- 百度网盘与夸克网盘 Cookie 授权
- 指定转存目录与同步任务
- 共享文档变化识别：新增、修改、删除
- 浏览器变化通知
- 点击“应用变化并增量同步”时，只处理变化的数据
- CSV 导出
- Neon PostgreSQL 持久化
- GitHub Actions 每 15 分钟唤醒 Render 检查共享文档

## Render 环境变量

- `DATABASE_URL`：Neon PostgreSQL 连接地址
- `CREDENTIAL_SECRET`：Cookie 加密密钥，Render 可自动生成
- `CRON_SECRET`：定时检查密钥，Render 可自动生成
- `FIRECRAWL_API_URL`：可选，用于需要网页解析的共享文档
- `SYNC_CONCURRENCY`：同步并发数，默认 3

## GitHub Actions Secrets

- `APP_URL`：Render 固定网址，例如 `https://xxx.onrender.com`
- `CRON_SECRET`：与 Render 中的 `CRON_SECRET` 保持完全一致

## 部署

仓库包含 `render.yaml`，Render 已配置为 GitHub 提交后自动部署。健康检查地址为 `/api/health`。
