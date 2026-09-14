# 提示词配置

运行时使用的主要提示词位于 `backend/tcg/prompts/`。测试专家可以用文本编辑器修改 YAML 中的措辞，不需要修改 Python，也不需要重启服务。正在执行的模型调用使用发起时读取的版本；下一次调用读取保存后的配置。已经保存的成果不会因改提示词自动重写。

| 文件 | 配置项 | 用途 |
| --- | --- | --- |
| `chat.yaml` | `system` | 对话意图、工具使用、确认与跳过规则、回复形式 |
| `planner.yaml` | `system` | 将当前请求拆成有序能力步骤，保留目标/约束，不规划未来自动确认 |
| `native.yaml` | `system` | 原生工具调用的通用约定 |
| `business.yaml` | `policy` | 业务依据、格式示例、人工字段保护 |
| `business.yaml` | `understand` | 需求提取、澄清建议与真实互斥选项 |
| `business.yaml` | `generate_relationships`、`generate_direct_cases`、`generate_rows` | 场景/用例生成，以及跳过场景后直接生成用例 |
| `business.yaml` | `review` | 生成评审建议；数据应用仍由后台确认流程控制 |
| `business.yaml` | `revise`、`revise_add`、`revise_independent`、`revise_linked` | 修改、新增与关联保留 |
| `business.yaml` | `complete_fields` | 仅补缺失的 AI 字段 |
| `business.yaml` | `estimate`、`explain` | 用例数量估算、成果解释与总结 |
| `analysis.yaml` | `diagrams` | 业务流程图、领域思维导图、状态转换图 |
| `evidence.yaml` | `repair` | 证据引用的定向修复 |

文件格式如下，使用 UTF-8，保持两层缩进：

```yaml
version: 1
prompts:
  system: |
    You are TCG's conversational test-design assistant.
    Speak Chinese naturally. Summarize proposed changes before acceptance.
```

`version: 1` 是文件格式版本。保留各文件现有的键名；文字可以调整，例如让总结更简短、要求评审先解释风险、或改善专业术语。`|` 后面的内容是普通文本，JSON 的花括号、Mermaid、`${...}`、`{{...}}` 都会原样发送，不做变量替换，也不能运行 Python 或模板表达式。

工具参数 Schema、证据 ID 校验、版本检查、人工确认、可跳过的阶段、人工字段保护由 Python 实现。改提示词不会新增工具、解除这些约束或改变 API 数据结构；这类行为变更仍需修改和验证业务代码。

## v3.0.14 提示词结构修复

本修复更新 `native.yaml`、`business.yaml` 和 `analysis.yaml`：明确报告字段嵌套在 `report` 对象内，澄清选项放在对应建议对象内，三图使用各自的 `title` 和 `mermaid` 字段；同时消除输出字段的点号简写。全局规则只在当前工具声明 `report` 时适用，估算、解释和字段补全等工具继续遵守各自的 Schema。

已有 v3.0.14 安装只需替换上述三个 YAML 文件的完整内容。下一次模型调用会读取新提示词，正在执行的调用不会中途切换；等待当前调用结束后，可点击“重试当前步骤”。严格结构校验继续保留，非法结果不会因本次修复而被自动放行。

本次验证覆盖提示词解析、热加载、实际请求携带的提示词，以及嵌套字段与根层字段的 Schema 对照；没有连接用户的真实模型。明确层级可减少输出歧义，但不能保证消除所有模型结构错误或带来固定幅度的速度提升。

缺失文件、重复键、空文本、不支持的格式版本、非法 YAML 或超限文件会明确报出文件名及配置项。单文件上限 128 KiB，单项上限 65536 字符；不会静默使用空提示词或旧的硬编码内容。请保存完整文件后再发送请求，避免请求读取到编辑中间状态。业务批次缓存会包含通用 Policy、原生 System 和当前阶段提示词，修改后不会复用这些提示词旧版本对应的批次结果。

模型日志 `model.transport_start` 和保存的请求包含：

- `prompt_templates`：实际参与本次请求的 YAML 文件、配置键、格式版本、内容 SHA-256。
- `system_prompt_sha256`：本次最终系统提示词的 SHA-256。
- 原有 `call_id` / `run_id`：用于关联请求、校验与重试。

这些元数据不会发送到模型，也不会把业务原文额外打印到常规日志。需要核对某次重试具体采用的内容时，可使用对应调用的请求记录。修改配置后，可先在新会话用一份小需求跑“需求理解 → 场景 → 用例 → 评审”，核对建议及人工确认节点；不要把提示词措辞变化误当成已经验证的流程行为变化。
