# v3.0.2：模型连接排查与对话反馈

升级后重启服务，再复现一次问题。旧日志无法补回当时未记录的连接信息；新版能够记录下一次请求实际遇到的错误，但不会据此假定当前环境故障已经解决。

## 找到日志

日志位于实际数据目录下的 `logs/tcg.log`，并同时输出到启动终端。默认从解压目录启动时通常是 `data/logs/tcg.log`；使用 `--data-dir` 时请以启动终端显示的数据目录为准。日志默认 INFO 已包含本次排查信息，无需打开 DEBUG。

1. 启动后搜索 `service.runtime`，检查 Python 解释器、依赖版本、工作目录、数据目录及 CA 文件是否存在。
2. 在「模型与设置」执行连接测试，或在聊天中重现失败。连接测试使用真实的原生工具调用协议。
3. 聊天失败后展开助手消息中的「查看排查信息」，复制问题编号（通常以 `call_` 开头）。
4. 在日志中搜索该编号，核对 `model.transport_start`、`model.transport_error` 和 `model.error`。聊天级错误另有 `chat.turn_failed`；后台生成失败有 `pipeline.failed`。

Linux / macOS：

```bash
tail -n 100 data/logs/tcg.log
grep -F 'call_替换为问题编号' data/logs/tcg.log
```

Windows PowerShell：

```powershell
Get-Content .\data\logs\tcg.log -Tail 100
Select-String -Path .\data\logs\tcg.log -SimpleMatch 'call_替换为问题编号'
```

日志按大小轮转；如果当前文件中找不到较早的编号，也可检查 `tcg.log.1` 等备份文件。

## 关键字段

| 字段 | 用途 |
|---|---|
| `request_id` / `turn_id` / `call_id` | 对应 HTTP 请求、聊天回合和具体模型调用 |
| `endpoint` / `provider` / `model` | 实际请求路径及模型配置，帮助识别路径拼接或模型名称错误 |
| `auth_mode` / `auth_header_names` | 实际使用的认证方式与请求头名称；不记录头值 |
| `http_status` / `provider_request_id` | 上游 HTTP 状态及可用于查网关日志的请求编号 |
| `category` / `error_types` / `stack` | 失败类别、底层异常链和代码位置；不记录异常原文与局部变量 |
| `errno` / `winerror` / `verify_code` | 操作系统连接错误或证书校验代码（底层异常提供时） |
| `timeout_seconds` / `timeout_phase` / `elapsed_ms` | 超时配置、连接/读取/整体超时类别及实际耗时 |
| `content_type` / `response_bytes` | 识别代理登录页、HTML 错误页或不符合模型协议的响应 |

标准服务错误码只记录已知分类；其他错误码记录摘要，不把提供方错误正文或需求内容写入运行日志。

## 按失败类别处理

| `category` | 优先检查 |
|---|---|
| `dns` | 启动服务器的域名解析、内网 DNS、VPN；需要在运行 Python 的环境检查 |
| `connection` | 模型进程、监听地址、端口和防火墙；容器内 `127.0.0.1` 指向该容器 |
| `tls` | 模型证书域名、有效期、Python 信任的 CA；不要通过关闭证书验证绕过错误 |
| `proxy` | 到模型服务的代理链路及网络要求 |
| `timeout` | 对照超时阶段，确认是无法建立连接还是模型响应过慢 |
| `authentication` | API Key、自定义认证头及服务授权（通常 HTTP 401/403） |
| `configuration` | 最终请求地址、模型名称和服务支持的参数（通常 HTTP 400/404/422） |
| `rate_limit` | 并发、配额、限流设置（HTTP 429） |
| `service_unavailable` | 网关和模型上游错误（HTTP 5xx），用上游请求编号进一步定位 |
| `protocol` / `tool_calling` | 响应是否为正确模型协议、网关是否转发原生工具调用 |

**公司网络注意：当前内置模型客户端 `trust_env=False`，直接连接，不读取系统 PAC、HTTP_PROXY / HTTPS_PROXY 或 SSL_CERT_FILE / SSL_CERT_DIR。** 日志会明确记录这一实际行为；本版没有改变网络、代理或 TLS 校验方式。浏览器能访问目标，并不证明启动 Python 的环境能直接访问。对测试注入的客户端，日志会标记 `client_source=injected`，不猜测其实际连接策略。

模型请求日志不包含认证头值、API Key、需求正文或提供方错误正文。已有完整调用快照、结果文件属于另外的调试产物，可能含业务数据；排查连接时只需提供上述运行日志中对应问题编号的记录及一次 `service.runtime`。

## 演示错误与建议回复

1. 使用无效模型地址或错误认证执行一次测试，确认助手在用户消息之后说明失败；展开排查信息核对问题编号。
2. 后台生成阶段失败时，也会留下助手错误消息；已有成果保留，可修正环境后在聊天中要求重试当前步骤。
3. 在人工确认或澄清节点，输入框上方显示当前建议回复。点击某条建议，会填入可编辑草稿并隐藏已选项；已有草稿会保留。
4. 逐项核对后发送。选择建议不会自动调用模型、确认下一阶段或把假设作为已确认事实保存。
5. 当前提示更新后，旧消息里的建议不会继续作为可操作选项。主界面和成果查看方式沿用 v3.0.1。

本轮使用受控 HTTP 响应和真实应用组件验证日志、失败消息、建议和主流程；尚未连接用户实际内网模型。真实浏览器视觉验收受当前环境访问策略限制。
