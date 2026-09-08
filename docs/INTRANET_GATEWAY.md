# 接入只支持最小请求格式的 Chat Completions 网关

适用于使用 `X-API-Key`、`messages[].content` 为文本块数组、响应为普通 Chat Completions JSON 的网关。原有 Ollama 和标准 OpenAI 兼容连接默认行为保留。

在当前实际使用的 `.env` 中配置一次，重启后生效：

```dotenv
TCG_MODEL_PROVIDER=openai
TCG_MODEL_BASE_URL=http://127.0.0.1:1234/api/v1
TCG_MODEL_NAME=gpt-5
TCG_API_KEY=""
TCG_MODEL_AUTH_MODE=headers
TCG_MODEL_HEADERS_JSON='{"X-API-Key":"替换为你的密钥"}'
TCG_MODEL_REQUEST_MODE=minimal
TCG_MODEL_TIMEOUT_SECONDS=3600
```

将示例服务地址替换为你内网网关的地址。可以填基础地址 `/api/v1`，也可以填完整的 `/api/v1/chat/completions`，系统不会重复追加路径。配置的模型名应为网关实际支持的名称。

`headers` 模式只发送配置的自定义认证头，不附加保存过的或环境中的 Bearer Key。`Content-Type: application/json` 自动设置；直接复制这一标准头也可以保存。不要填写其他 Content-Type 或传输控制头。

`minimal` 模式仅发送以下两个顶层字段，模型调用不再额外附加 `temperature`、`response_format`、`stream` 或 `stream_options`：

```json
{
  "model": "gpt-5",
  "messages": [
    {"role": "system", "content": [{"type": "text", "text": "任务约束与输出格式"}]},
    {"role": "user", "content": [{"type": "text", "text": "需求与任务上下文"}]}
  ]
}
```

系统仍在提示词中要求模型输出 JSON，供 LangGraph 后续节点校验。网关返回的 `choices[0].message.content` 必须包含模型回复；支持 HTTP 200 并不等于模型一定能完成结构化任务。连接测试仍会实际检查 `{"ok":true}`。

前端 SSE 保持工作：节点进度和等待状态实时发送，上游完整 JSON 返回后再发送回复正文。本模式没有上游逐 token 流，系统不会伪造逐字输出。标准模式继续使用原有流式调用。

在“查看调用详情 → 查看发送内容 → 请求记录”核查 `request_mode`、`endpoint` 和 `http_request.body`。`messages` 保留方便阅读的 LangChain 文本，`http_request.body.messages` 则是实际发送的文本块数组。认证头的值始终脱敏，HTTP 请求体不进入普通诊断日志。

`.env` 选择顺序为显式 `--env-file`、数据目录 `.env`、项目目录 `.env`，只读取其中一个；进程环境变量再覆盖文件值。设置页面会显示当前文件路径。如果修改未生效，先核查实际读取的文件和进程环境变量，不要把密钥发到聊天或提交到 GitHub。

HTTP 401/403 表示鉴权或权限问题；404 检查地址；400/405/422 检查方法、字段及兼容模式。错误提示和诊断会标出实际 HTTP 状态码。重定向不会被跟随，避免自定义认证头流向另一个地址。

本地集成测试使用模拟严格网关验证请求格式、认证、完整路径、错误提示及 SSE。它不能证明特定公司内网的网络路由、访问权限或真实模型已验证通过。
