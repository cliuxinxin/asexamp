# 发布到自己的 Cloudflare 账号

如果正在使用 Cloudflare 的 GitHub 导入页面，请使用 [Workers Builds 页面填写说明](CLOUDFLARE_BUILDS.md)。下文是手动 GitHub Actions / 本机 CLI 的备选路径。

此路径使用你的 Workers、D1、R2 和 Cloudflare Access。代码与本地 Python 入口共存，本地继续运行 `python3 start.py`；云端不会读取或上传本机 `.env`、聊天数据库及模型密钥。

## 一次性账号配置

1. 在 Cloudflare 账号中启用 Workers 和 R2，确认账号的 `workers.dev` 子域名。默认应用域名为 `asexamp.<你的子域名>.workers.dev`，也可用 `TCG_WORKER_NAME` 改应用名。
2. 在 Cloudflare Zero Trust 中建立 Self-hosted Access 应用，保护上面的完整域名及所有路径。配置 Allow 策略，只允许你的邮箱或指定成员登录。记录团队地址 `https://<团队名>.cloudflareaccess.com` 和应用的 AUD。发布脚本不会自动建立 Access 应用或修改访问策略。
3. 创建限定到此账号的 API Token，授予 Workers Scripts 编辑、D1 编辑和 Workers R2 Storage 编辑权限。账号须已有可用的 Workers 子域名。Token 只用于发布，不是模型 API Key。

Access 必须在首次访问前配置好。未经验证的请求会得到 401，缺少验证配置会得到 503，不会开放聊天数据。认证按 Access 的稳定用户 ID 隔离；更换团队会产生新的用户身份空间。官方说明：[Access JWT 验证](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)。

## 通过 GitHub 发布

合并包含此流程的 PR 后，在仓库 Settings → Environments 新建 `cloudflare-production`，添加以下配置。也可使用同名的仓库 Actions Secrets 和 Variables。

| 名称 | 类型 | 内容 |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | Secret | 上一步的发布 Token |
| `CLOUDFLARE_ACCOUNT_ID` | Secret | 32 位 Cloudflare Account ID |
| `TCG_ACCESS_TEAM_DOMAIN` | Variable | 包含 `https://` 的团队地址 |
| `TCG_ACCESS_AUD` | Variable | Access 应用的 64 位 AUD |
| `TCG_WORKER_NAME` | Variable，可选 | 默认 `asexamp`，与 Access 域名保持一致 |

在 Actions → Deploy to Cloudflare → Run workflow 执行发布。此流程现在仅手动触发，避免与 Cloudflare 原生 GitHub 集成重复发布。后续自动更新由 Workers Builds 监听其配置的生产分支。

流程先运行测试与类型检查，再检查资源、应用迁移、构建并发布 Worker。首次发布自动创建 D1 和 R2；后续复用已部署项目的绑定。首次发布生成 `TCG_SECRET_KEY`，后续发布保留它，避免已保存的模型 Key 无法解密。不要删除或随意替换这个秘密变量。

发布 Token 放在 GitHub Secrets 或安全的本地进程环境中，不要写入仓库或聊天。可在 Cloudflare 控制台查看 Worker 日志；工作流成功后会输出实际的 `workers.dev` 地址。

## 从本机发布

准备 Node.js 24，将上表配置通过安全的进程环境注入后，在项目根目录执行：

```bash
npm ci
npm test
npx tsc -p cloud/tsconfig.json
npm run deploy:cloudflare
```

部署器把账号资源绑定写入被 Git 忽略的 `.cloudflare/wrangler.json`，其中不包含发布 Token 或模型 Key。`wrangler.jsonc` 是模板；不要绕过部署器直接对该模板执行远程发布。

无需账号即可检查部署包：

```bash
npm run check:cloudflare
```

此命令为 Wrangler dry-run，仅构建与检查，不会发布服务。`npm run build:cloudflare` 始终构建 Access 验证入口；默认 `npm run build` 保留 Sites 构建方式。两种构建都输出到 `dist/`，不会覆盖本地 Python 使用的 `frontend/dist/`。

## 首次运行与中断恢复

发布成功后，打开工作流输出的地址，完成 Access 登录。在“模型与设置”保存并测试模型连接，再上传一份小需求验证分析、SSE 输出和请求核查。Key 加密存入 D1，后续不需要反复填写。

首次发布如果在资源创建后、Worker 发布前中断，再次运行可能提示同名资源归属未确认。先在 Cloudflare 控制台核对资源确为本项目，再显式指定下表变量恢复；部署器不会仅按名称接管已有资源。

| 可选 Variable / 环境变量 | 用途 |
|---|---|
| `TCG_D1_DATABASE_ID` | 明确复用的 D1 数据库 UUID |
| `TCG_R2_BUCKET_NAME` | 明确复用的 R2 存储桶名称 |

如果原绑定资源已被删除，部署会停止，不会自动创建空数据库冒充原记录。若同名 Worker 不属于此项目，使用新 Worker 名称并同步修改 Access 应用域名。

模型单次请求上限仍为 60 分钟。Workers 或网络可能提前断开；云端页面关闭后任务可能暂停，重新打开会从已保存检查点恢复，未完成的模型调用可能重发。云端和本地的数据互不自动同步。详细执行方式与容量边界见 [云端架构说明](CLOUD.md)。
