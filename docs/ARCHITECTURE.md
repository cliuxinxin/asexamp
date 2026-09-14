# TCG 当前代码导航

先读本文件，再按任务查看对应模块。启动与模型配置见根目录 README；可用路由及参数以运行服务的 `/docs` 为准。

| 入口或模块 | 职责 |
| --- | --- |
| `start.py`、`backend/tcg/main.py` | 本地启动、环境与 FastAPI；注册 `native_api.py` 路由，提供构建后的前端。 |
| `frontend/src/main.tsx`、`App.tsx` | React 入口、对话、输入框、当前阶段与成果查看。 |
| `native_chat.py`、`tool_registry.py` | Chat Agent 的原生工具调用；读取、解释、修改、启动或恢复后台流程。 |
| `supervisor.py`、`prompts/planner.yaml` | 原生规划工具、按能力分发、跨回合队列与确认后的续跑；只保存指令、标识及简短回执。 |
| `supervisor_recovery.py` | 服务恢复时核对已保存回执，后台流程完成后唤醒后续步骤，不代替 Pipeline 确认。 |
| `ExecutionPlan.tsx` | 聊天内计划卡片，读取实际执行状态，复用现有确认入口。 |
| `pipeline.py` | LangGraph 生成节点、原生 `interrupt()` 确认点、检查点恢复。 |
| `native_business.py`、`native_schemas.py` | 需求理解、场景、用例、评审及业务结果校验。 |
| `generation_repair.py`、`generation_candidates.py` | 共用三次内容修复预算；失败草稿与正式成果隔离，保留各轮结果供查看。 |
| `native_model.py`、`model.py`、`model_diagnostics.py` | 模型适配、Tool Calling、服务器响应及诊断记录。 |
| `storage.py`、`operations.py`、`native_migration.py` | SQLite 数据、不可变成果版本、并发版本检查、旧数据兼容。 |
| `native_views.py`、`artifact_read.py` | 当前阶段与成果的读取投影；不建立另一套流程状态机。 |
| `documents.py`、`project_context.py`、`project_facts.py` | 文件解析、项目知识、本会话采用范围及来源。 |
| `profile_edits.py`、`profile_changes.py`、`field_drift.py` | 模板差异建议、确认应用、用例字段与导出列同步。 |
| `review_proposals.py`、`artifact_previews.py` | 绑定成果版本的评审/修改建议，确认前不修改正式用例；对话消息内展示并接受/拒绝。 |
| `case_columns.py`、`manual_edits.py` | 用例列计划、手动修改证据、Profile 确认与延迟导出。 |
| `InlineChangeCard.tsx`、`TableItemEditor.tsx` | 字段差异预览、可编辑场景/用例表格、步骤和预期配对。 |
| `ArtifactCard.tsx`、`AnalysisReport.tsx`、`ConversationParts.tsx` | 成果表格、业务图、来源与对话内附件。 |
| `ProfileChangeDialog.tsx`、`MemoryDialog.tsx` | Profile 更改确认、项目知识查看与本会话开关。 |
| `traceability.py`、`TraceabilityPanel.tsx` | 全项目/当前会话追溯树，按真实版本比较缺口和待同步；仅读取已存成果。 |
| `prompt_loader.py`、`prompts/*.yaml` | 主要提示词配置、安全读取、热更新及请求版本记录。 |
| `ClarificationQuestions.tsx` | 问题下的明确选项和建议答案；按钮绑定单题，不消费输入草稿。 |
| `tests/`、`frontend/tests/` | 当前接口、流程及组件验证；不包含已废弃控制器的历史测试。 |

主流程读取资料并生成需求理解、场景、用例和评审；人工确认由原生图中断处理。聊天通过工具查询或修改数据库，继续时生成节点读取最新成果。图状态保存标识和成果指针，文档正文及版本内容保存在 SQLite 中。

普通请求先通过原生 `submit_execution_plan` 工具提交有序能力步骤。Supervisor 为每一步选择工具子集，调用现有 Chat Agent 执行；修改预览、Profile 建议和后台生成分别返回真实等待状态。当前的简短同意/拒绝直接处理，其他明确确认表达可由规划入口绑定当前已有确认对象；计划不能批准自身未来生成的预览。后台 Pipeline 保持独立，监督者只观察其完成，不代替用户通过门禁。

SQLite 的 `execution_plan` 记录步骤、原始请求位置、目标及版本、选择范围、等待对象与简短工具回执，不存文档副本。修改确认后更新绑定，再执行导出。导出失败重试不会重复已完成修改；服务中断且无法确定写入结果时暂停核对，不盲目重放。`GET /api/chats/{chat_id}/plans/{plan_id}` 提供前端状态投影，不返回内部提示词、资料或完整回执。

聊天上下文通常使用当前状态、近期消息和资料目录，按需读取正文；生成节点使用当前阶段的父成果及关联证据。服务器拒绝可拆分批次的容量后才拆分，不预设本地上下文上限。

Planner 只接收当前消息、状态和目录、近期对话摘录及少量当前计划摘要；执行步骤再按需读取目标成果。规划增加一次模型请求，跨多个能力时每步各自调用工具代理；简单确认不增加模型请求。计划卡片中的步骤是公开工作安排，不是模型隐含推理。

`frontend/dist/` 是可直接启动所需的构建文件，`frontend/node_modules/` 是本机依赖。`.rgignore` 让默认代码搜索跳过这些生成目录和业务数据；源码、当前测试及配置仍参与搜索。旧版本设计文档、旧测试归档和已脱离入口的 UI 组件已清理，历史可从 Git 或先前发行包恢复。

Human 正常顺序：理解 → 确认理解 → 场景 → 确认场景 → 用例 → 评审建议 → 确认建议 → 应用用例修改。Auto 使用相同建议和提交逻辑自动应用。显式只要草稿或暂停仍保留。评审建议通过 `review_ref` 指针持久化，聊天上下文只载入摘要与编号，详情通过工具按需读取。

独立场景使用 `requirement_ids=[]`，独立用例使用空 `scenario_id`，内部保存经过授权的独立修改说明和真实输入来源。有效父关联正常保留；未知非空父 ID 仍报错。用例导出请求绑定已保存的用例版本及待确认的 Profile 建议，版本过期时不输出旧文件。

显式跳过场景：理解确认节点使用 `skip_to_cases` 条件边进入 `direct_cases`，用例保留真实 `requirement_ids` 及需求版本，`scenario_id` 为空并标注场景已跳过。常规确认节点不变。只要草稿时保存用例后完成；评审拒绝则保留原用例并结束本轮，不应用评审建议。
