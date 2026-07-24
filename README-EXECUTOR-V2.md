# 网盘同步执行器 v2

这是为“网盘同步管理中心”提供真实转存能力的独立执行器。它只启动协议
v2 服务，不暴露原项目的旧管理页，也不要求用户打开开发者工具手工复制
Cookie。

## 功能

- 百度网盘、夸克网盘、UC 网盘、迅雷云盘官方网页登录；
- 自动识别登录完成状态，在执行器内部加密保存会话；
- 将“电视剧/动漫名称、源分享链接、目标目录”按幂等键持久化；
- 首次转存、定时源目录指纹比较、新增文件补存；
- 创建自己的分享链接并做匿名访问校验；
- SQLite 持久化队列、失败指数退避、服务重启后恢复；
- 实现管理站要求的 `/v1/*` 协议 v2 接口。

登录窗口优先用于扫描官方网盘二维码。账号密码、Cookie 和 token 不会返回
管理站，也不应通过聊天或文档传递。

## Render 部署

仓库根目录的 `render.yaml` 会创建：

- 一个 Docker Web Service（Starter 实例）；
- 一个挂载到 `/var/lib/netdisk-executor` 的 5 GB 持久磁盘；
- Render 自动生成的 `EXECUTOR_TOKEN` 与 `EXECUTOR_MASTER_KEY`；
- `/livez` 健康检查和提交后自动部署。

Render 免费 Web Service 会休眠且不能挂载持久磁盘，因此不适合持续监控。
创建 Blueprint 前请先确认 Render 控制台展示的月度费用。

部署完成后，将下面两项安全写入管理站运行环境：

- `NETDISK_EXECUTOR_URL`：Render 服务的 HTTPS 地址；
- `NETDISK_EXECUTOR_TOKEN`：Render 自动生成的执行器访问令牌。

不要把令牌写入仓库、聊天消息或截图。

## 本地协议检查

```bash
export EXECUTOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export EXECUTOR_MASTER_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export EXECUTOR_DATA_DIR=/tmp/netdisk-executor
cd backend
uvicorn app.executor_v2.app:app --host 127.0.0.1 --port 10000
```

另一个终端请求：

```bash
curl -H "Authorization: Bearer $EXECUTOR_TOKEN" \
  http://127.0.0.1:10000/v1/health
```

## 安全边界

- 执行器必须只通过 HTTPS 暴露；
- 登录窗口令牌位于 URL fragment，不会进入 HTTP 访问日志，10 分钟后失效；
- 网盘会话使用独立主密钥加密后才写入磁盘；
- 日志不记录 Cookie、密码或网盘 token；
- 最短检查周期为 1 小时，并带随机抖动与指数退避；
- 持久磁盘只允许单实例访问，不应开启多实例扩容。

## 验收

上线前需要使用四个网盘账号各完成一次真实验收：

1. 官方登录；
2. 读取一个测试分享；
3. 转存到按剧名创建的目标目录；
4. 创建自己的分享链接并匿名打开；
5. 给源分享新增一个测试文件；
6. 手动检查并确认只补存新增文件；
7. 重启服务，确认账号加密会话、任务和状态仍在。

未完成某家网盘的真实账号验收前，不应把该家标记为“已跑通”。

## 许可证与来源

本修改基于
[OzoO0/cloud-auto-save-x](https://github.com/OzoO0/cloud-auto-save-x)，
继续按 GNU Affero General Public License v3 发布。请保留 `LICENSE`，
线上对应源码位于
[`executor-v2` 分支](https://github.com/hzf803520-glitch/netdisk-auto-save-x/tree/executor-v2)。
