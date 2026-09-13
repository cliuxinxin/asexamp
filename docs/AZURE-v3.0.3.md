# v3.0.3：Azure OpenAI 连接

本版增加环境变量控制的 Azure 连接。聊天代理、后台生成、评审、估算、模板学习和连接测试共用同一模型适配器；原网关与 Ollama 连接继续保留。

## 配置和启动

在当前使用的 `.env` 中加入以下内容，替换占位值：

```env
AZURE_OPENAI_API_KEY="你的 Azure API Key"
AZURE_OPENAI_ENDPOINT="https://你的资源名.openai.azure.com/"
AZURE_API_VERSION="2025-01-01-preview"
AZURE_OPENAI_DEPLOYMENT="你的模型部署名称"
```

前三项为必填。`AZURE_OPENAI_DEPLOYMENT` 可以省略，省略时使用 `TCG_MODEL_NAME`，再没有则使用设置页面以前保存的模型名称。这里的值必须是 Azure 实际部署名称，不保证与基础模型名称相同。找不到部署名称时会明确提示补充，不会猜测。

地址只填资源根地址；省略 `https://` 会自动补齐，末尾 `/` 可保留。不要填 `/openai/v1`、部署路径或 `chat/completions`。请求按所填版本发送，版本是否支持当前部署由 Azure 校验，不会替你更换版本。

修改后重启 TCG：

```bash
python start.py --data-dir /path/to/existing/data
```

也可明确选择配置文件：

```bash
python start.py --data-dir /path/to/existing/data --env-file /path/to/config.env
```

Windows 示例：`py -3.12 start.py --data-dir D:\TCG-data --env-file D:\TCG-config\.env`。

打开“模型与设置”，应看到 **Azure OpenAI · 环境配置**、资源地址、部署名称与 API 版本。点击“测试连接”；测试实际要求模型完成一次原生工具调用。配置由环境管理时页面只显示和测试，不覆盖 `.env`。

## 连接选择

- 先选一个配置文件：显式 `--env-file` / `TCG_ENV_FILE`，否则数据目录 `.env`，否则程序目录 `.env`。不会同时合并多个文件。
- 同名进程环境变量覆盖所选文件中的值，包括显式空值。
- 任一 Azure 字段有效非空即启用 Azure 配置校验；完整时优先使用 Azure，原 `TCG_MODEL_PROVIDER`、地址、密钥、认证模式、自定义请求头不参与 Azure 连接。
- 配置不完整时明确列出缺项，不会静默切回原网关。四个 Azure 字段全为空或不存在时使用原连接。
- `TCG_MODEL_TIMEOUT_SECONDS`、输出配置继续生效。Azure 请求使用 `max_completion_tokens`；原网关仍使用 `max_tokens`。选择服务端限制模式时不附带输出上限参数。
- 原 `settings.json` 不被改写。要切回原网关，移除或注释全部 Azure 字段（包括进程环境变量），重启即可。

## 日志排查

请求地址形如：

```text
https://your-resource.openai.azure.com/openai/deployments/your-deployment/chat/completions?api-version=2025-01-01-preview
```

检查 `logs/tcg.log` 的 `model.transport_start`：

| 日志字段 | Azure 连接应显示 |
| --- | --- |
| `provider` | `azure` |
| `endpoint` | 包含所选部署及 `api-version` 的完整请求地址 |
| `deployment` | 实际部署名称 |
| `api_version` | `.env` 中设置的 API 版本 |
| `auth_header_names` | `["api-key"]` |

认证失败检查 Azure Key 与资源是否对应；`DeploymentNotFound` 检查部署名称；`InvalidApiVersionParameter` 检查 API 版本。错误日志保留安全的服务错误码、HTTP 状态及 Azure `apim-request-id`，不记录密钥值或服务错误正文。界面错误继续作为助手消息显示，可按调用编号查日志。

微软的[REST 参考](https://learn.microsoft.com/en-us/azure/foundry/openai/reference-preview)说明了日期版部署路径、`api-version` 参数与 `api-key` 鉴权；[工具调用说明](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/function-calling?view=foundry-classic)说明部署及工具调用要求。

## 本版验证

已用可控 HTTP 响应验证环境配置、认证隔离、原生工具调用、错误日志，以及“上传需求 → 聊天启动 → 理解 → 场景 → 用例 → 评审”的自动与人工流程。人工流程的四个确认点均通过对话继续。原网关与 Ollama 的适配器回归、前端设置测试、TypeScript 和生产构建通过。

测试没有连接你的真实 Azure 资源。你的示例未提供可用资源地址和部署名称；部署是否支持当前 API 版本、原生工具调用及配额需要启动后通过连接测试确认。
