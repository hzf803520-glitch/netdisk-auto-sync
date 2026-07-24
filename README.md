# 网盘自动转存与追更系统 V2

支持 **百度网盘、夸克网盘、UC 网盘、迅雷云盘**。把包含“电视剧/动漫名称 + 分享链接”的文档导入后台，系统会保存以下完整对应关系：

> 剧名 → 原分享链接 → 转存状态 → 我的新分享链接 → 新提取码 → 最近追更时间

## 已实现

- 上传或粘贴 TXT、CSV、XLSX、DOCX 文档
- 自动识别剧名、平台、分享链接和提取码
- 百度、夸克、UC、迅雷四平台转存
- 成功后自动生成自己的新分享链接
- 剧名和新分享链接永久对应保存在 Neon
- 定时重新检查并转存，成功后刷新新链接
- 新副本转存成功后再清理旧副本，避免先删后失败
- 登录助手：在真实浏览器登录后自动上传登录状态，不需要手动找 Cookie
- Cookie / Token 使用 `COOKIE_ENCRYPTION_KEY` 加密后保存在 Neon
- GitHub Actions 每 15 分钟唤醒 Render 免费实例
- CSV 导出完整“剧名 ↔ 新链接”清单

## 一键部署

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/hzf803520-glitch/netdisk-auto-sync)

部署前先在 Neon 创建免费 Postgres 数据库并复制连接字符串。Neon 连接字符串通常包含：

```text
postgresql://用户名:密码@主机/数据库?sslmode=require&channel_binding=require
```

点击上方按钮后，Render 会读取根目录 `render.yaml`。需要填写：

| Render 环境变量 | 填写内容 |
|---|---|
| `DATABASE_URL` | Neon 的完整连接字符串 |
| `ADMIN_PASSWORD` | 后台管理员密码，建议 16 位以上 |

以下变量由 Render 自动生成：

- `COOKIE_ENCRYPTION_KEY`
- `CRON_SECRET`

`ADMIN_USER` 默认是 `admin`。

## 开启自动追更

部署完成后，在 GitHub 仓库进入：

```text
Settings → Secrets and variables → Actions → New repository secret
```

添加：

| Secret | 值 |
|---|---|
| `RENDER_URL` | Render 完整地址，例如 `https://netdisk-auto-sync.onrender.com` |
| `CRON_SECRET` | Render 环境变量中的同名值 |
| `ADMIN_USER` | `admin`，或你在 Render 设置的账号 |
| `ADMIN_PASSWORD` | Render 后台密码 |

然后进入：

```text
Actions → 定时检查网盘更新 → Run workflow
```

手动运行一次测试。

## 使用流程

1. 打开 Render 地址并输入管理员账号密码。
2. 在“绑定你的网盘账号”区域点击百度、夸克、UC 或迅雷。
3. 下载并解压登录助手，双击 `开始登录.command`。
4. 在弹出的真实浏览器登录，登录成功后回到终端按回车。
5. 上传文档或粘贴“剧名 + 网盘链接”。
6. 点击“立即转存全部”。
7. 在资源对应表查看、复制或导出自己的新分享链接。

## 文档示例

```text
千香 更新至12集 https://pan.quark.cn/s/xxxxxxxx
千香 百度网盘 https://pan.baidu.com/s/xxxxxxxx?pwd=8888
某动漫
https://drive.uc.cn/s/xxxxxxxx
```

同一部剧可以有多个平台链接，后台会分别建立记录，但剧名保持一致。

## 免费方案边界

- Render Free 在没有请求时会休眠，首次唤醒存在冷启动。
- GitHub Actions 的定时任务可能延迟，不能保证严格每 15 分钟准时执行。
- 百度、夸克、UC、迅雷没有向普通个人账号提供统一、稳定的“源分享更新实时推送”，因此免费方案采用定时重新转存，不是 0 秒实时触发。
- 每次到期检查会重新执行转存，成功后更新新分享链接并尝试删除旧副本。平台接口变化、风控、验证码、会员限制或分享失效会在后台日志中显示。
- 登录状态可能被网盘平台定期注销；失效后重新运行登录助手即可。

## 安全说明

- 不要把 Cookie、Token、数据库连接串、管理员密码提交到 GitHub。
- 登录凭证只发送到你自己的 Render 地址，并加密保存在你自己的 Neon 数据库。
- 修改 `COOKIE_ENCRYPTION_KEY` 后，旧凭证将无法解密，需要重新绑定。
- 仓库公开只会公开程序代码，不会公开 Render 或 GitHub Secrets。

## 技术结构

- 管理后台：Python 3.12 标准库 HTTP 服务
- 数据库：Neon PostgreSQL + psycopg
- 转存执行器：Go + `github.com/wgx0307/netdisk`
- 部署：Docker 多阶段构建 + Render Blueprint
- 定时唤醒：GitHub Actions
