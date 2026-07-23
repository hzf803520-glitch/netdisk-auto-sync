# 百度 + 夸克网盘自动追更平台

免费部署组合：**GitHub + Render Free + Neon Free**。

## 功能

- 批量导入百度、夸克分享链接
- 首次转存后显示真实文件夹名称和保存目录
- 每小时、每3小时、每6小时、每12小时、每天检查
- 手动扫描全部、更新全部、单独更新文件夹
- Cookie 保存到 Neon，并使用 `COOKIE_ENCRYPTION_KEY` 加密
- Render 重启、休眠或重新部署后，任务和 Cookie 不丢失
- GitHub Actions 每小时唤醒 Render 并执行到期任务
- 页面错误使用中文提示，技术详情保留在运行日志

## 一、创建 Neon 免费数据库

1. 登录 Neon，创建一个免费项目。
2. 点击 **Connect**。
3. 复制连接字符串，格式类似：

```text
postgresql://用户:密码@主机/数据库?sslmode=require&channel_binding=require
```

这条连接字符串后面填入 Render 的 `DATABASE_URL`，不要上传到 GitHub。

## 二、在 Render 免费部署

1. Render 点击 **New → Blueprint**。
2. 选择本仓库 `hzf803520-glitch/netdisk-auto-sync`。
3. Render 会读取根目录的 `render.yaml`。
4. 选择 Free 方案，并填写以下环境变量：

| 变量 | 填写内容 |
|---|---|
| `DATABASE_URL` | Neon 的数据库连接字符串 |
| `ADMIN_USER` | 后台登录用户名 |
| `ADMIN_PASSWORD` | 后台登录密码，建议16位以上 |
| `COOKIE_ENCRYPTION_KEY` | 自己设置的随机长密码，建议32位以上，后续不要更换 |
| `CRON_SECRET` | 自己设置的随机长密码，建议32位以上 |

`BAIDU_TRANSFER_DIR` 已默认设置为 `/资源数据`。

5. 点击部署，等待出现绿色的 **Live**。
6. 打开 Render 提供的 `https://...onrender.com` 地址，输入管理员账号密码。
7. 在平台“账号设置”中保存百度和夸克 Cookie。

## 三、配置 GitHub 每小时追更

在本仓库打开：

```text
Settings → Secrets and variables → Actions → New repository secret
```

创建两个 Secret：

| Secret 名称 | 值 |
|---|---|
| `RENDER_URL` | Render 完整地址，例如 `https://netdisk-auto-sync.onrender.com` |
| `CRON_SECRET` | 必须与 Render 中的 `CRON_SECRET` 完全相同 |

然后打开 **Actions → 每小时检查网盘更新 → Run workflow**，手动运行一次测试。

## 安全说明

- 不要把 Cookie、Neon 连接字符串、管理员密码写进代码或提交到 GitHub。
- 本仓库即使公开，也只包含程序代码；所有秘密都放在 Render Environment 和 GitHub Secrets。
- `COOKIE_ENCRYPTION_KEY` 用于加密 Neon 中保存的百度、夸克 Cookie。更换后，旧 Cookie 将无法解密，需要重新保存。

## 免费方案说明

Render Free 闲置后会休眠，下一次请求唤醒通常需要约一分钟。GitHub Actions 每小时触发一次，并在任务运行期间持续检查状态。任务、Cookie 和日志保存在 Neon，不依赖 Render 的临时磁盘。
