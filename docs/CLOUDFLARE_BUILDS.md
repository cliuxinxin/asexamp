# Cloudflare 直接连接 GitHub

在 Cloudflare 的 Create an app 页面选择 GitHub 仓库 `cliuxinxin/asexamp`，由 Workers Builds 拉取、构建和发布。本路径使用 Cloudflare 的构建凭据，无需在 GitHub 配置 Actions Secrets。本地仍执行 `python3 start.py`。

## 创建页面怎么填

| 页面字段 | 值 |
|---|---|
| Project name | `asexamp` |
| Build command | `npm run build:cloudflare` |
| Deploy command | `npm run deploy:builds` |
| Builds for non-production branches | 取消勾选 |
| Protect with Cloudflare Access | 开启，允许自己的账号登录 |
| Root directory | 仓库根目录，留空或 `/` |
| Production branch | 先选 `codex/cloudflare-dual-runtime`；包含适配代码的 PR 合并后再切到 `main` |

不要使用默认的 `npm run build`：它是 Sites 专用构建。Workers Builds 当前不会执行 Wrangler 配置中的 custom build，因此页面的 Build command 必须显式填写。参考 [Cloudflare 构建配置](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/)。

在 Advanced settings 的构建变量中添加：

| 名称 | 值 |
|---|---|
| `NODE_VERSION` | `24` |
| `CLOUDFLARE_ACCOUNT_ID` | 当前账号的 32 位 Account ID |

构建 Token 在 Cloudflare 的 API token 选项中选择，不要提交到 Git。需包含 Account 下的 Workers Scripts 编辑、D1 编辑、Workers R2 Storage 编辑权限。Cloudflare 自动生成的默认 Token 权限列表未包含 D1，需要补上 D1 编辑权限或选择已具备这些权限的自定义 Token；已有 Token 可在 My Profile → API Tokens 修改。如果创建页面未提供选择入口，可在首次创建后进入 Worker → Settings → Build 调整 API token 并重试构建。

账号需先在 Storage & databases → R2 → Overview 完成 R2 开通。Wrangler 会创建资源或复用当前 Worker 的绑定。项目名称与 `wrangler.jsonc` 中的 `name` 必须一致。不要为同一 Worker 同时启用 GitHub Actions 和 Workers Builds 自动发布；本仓库的 Actions 发布已改为手动触发。

## 首次发布后的登录配置

`deploy:builds` 先调用 Wrangler 发布，再读取已部署 Worker 的真实 DB 绑定，使用该 UUID 执行迁移，最后生成缺失的 `TCG_SECRET_KEY`。以后发布保留已有密钥；已存在但错误地配置成明文变量的密钥会明确报错，不会替换原值。发布失败或迁移失败会停止流程。

创建页的 Access 开关配置的是登录保护。我们的 Worker 还需要验证这层登录传入的 JWT，所以首次部署完成后，进入 **Worker → Settings → Variables and Secrets**，添加两项 **运行时变量**：

| 名称 | 值的来源 |
|---|---|
| `TCG_ACCESS_TEAM_DOMAIN` | Zero Trust 团队完整地址，形如 `https://your-team.cloudflareaccess.com` |
| `TCG_ACCESS_AUD` | 实际保护此 Worker 的 Access 应用的 Application Audience (AUD) Tag |

团队信息位于 Zero Trust → Settings。AUD 位于 Zero Trust → Access controls → Applications → 对应应用 → Configure → Additional settings。若同时配置了多条 Access 规则，使用实际匹配网站访问地址的那条应用的 AUD。参考 [AUD 获取与 JWT 验证](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)。

这两项必须放在 **运行时 Variables and Secrets** 中；Build variables 只在构建期间存在。保存并应用变量后再打开网站。首次配置前出现登录配置缺失或 401/503 是预期的拒绝访问行为，不表示模型生成已启动。Access 应用的允许名单和登录方式仍需配置正确。

`TCG_SECRET_KEY` 由发布脚本自动保存为 Secret，不需要复制到页面、GitHub 或本机。不要删除或替换它，否则已经加密保存的模型 Key 无法解密。

## 后续更新

向 Cloudflare 配置的生产分支提交代码，会触发构建与发布。部署日志可在 Worker 的 Deployments/Builds 页面查看。首次成功后，通过 Access 登录聊天界面，在“模型与设置”保存模型连接，上传小文档检查 SSE 过程与结果。

本路径的自动化测试覆盖迁移目标、错误中止和密钥保留，Wrangler dry-run 验证构建包；是否能在你的账号成功发布，还取决于实际 Token 权限、R2 开通状态和 Access 配置。详细云端执行方式见 [架构说明](CLOUD.md)。
