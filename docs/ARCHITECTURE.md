# TCG 当前代码导航

先读本文件，再按任务查看对应模块。启动与模型配置见根目录 README；可用路由及参数以运行服务的 `/docs` 为准。

| 入口或模块 | 职责 |
| --- | --- |
| `start.py`、`backend/tcg/main.py` | 本地启动、环境与 FastAPI；注册 `native_api.py` 路由，提供构建后的前端。 |
| `frontend/src/main.tsx`、`App.tsx` | React 入口、对话、输入框、当前阶段与成果查看。 |
| `native_chat.py`、`tool_registry.py` | Chat Agent 的原生工具调用；读取、解释、修改、启动或恢复后台流程。 |
| `pipeline.py` | LangGraph 生成节点、原生 `interrupt()` 确认点、检查点恢复。 |
| `native_business.py`、`native_schemas.py` | 需求理解、场景、用例、评审及业务结果校验。 |
| `generation_repair.py`、`generation_candidates.py` | 共用三次内容修复预算；失败草稿与正式成果隔离，保留各轮结果供查看。 |
| `native_model.py`、`model.py`、`model_diagnostics.py` | 模型适配、Tool Calling、服务器响应及诊断记录。 |
| `storage.py`、`operations.py`、`native_migration.py` | SQLite 数据、不可变成果版本、并发版本检查、旧数据兼容。 |
| `native_views.py`、`artifact_read.py` | 当前阶段与成果的读取投影；不建立另一套流程状态机。 |
| `documents.py`、`project_context.py`、`project_facts.py` | 文件解析、项目知识、本会话采用范围及来源。 |
| `profile_edits.py`、`profile_changes.py`、`field_drift.py` | 模板差异建议、确认应用、用例字段与导出列同步。 |
| `ArtifactCard.tsx`、`AnalysisReport.tsx`、`ConversationParts.tsx` | 成果表格、业务图、来源与对话内附件。 |
| `ProfileChangeDialog.tsx`、`MemoryDialog.tsx` | Profile 更改确认、项目知识查看与本会话开关。 |
| `tests/`、`frontend/tests/` | 当前接口、流程及组件验证；不包含已废弃控制器的历史测试。 |

主流程读取资料并生成需求理解、场景、用例和评审；人工确认由原生图中断处理。聊天通过工具查询或修改数据库，继续时生成节点读取最新成果。图状态保存标识和成果指针，文档正文及版本内容保存在 SQLite 中。

聊天上下文通常使用当前状态、近期消息和资料目录，按需读取正文；生成节点使用当前阶段的父成果及关联证据。服务器拒绝可拆分批次的容量后才拆分，不预设本地上下文上限。

`frontend/dist/` 是可直接启动所需的构建文件，`frontend/node_modules/` 是本机依赖。`.rgignore` 让默认代码搜索跳过这些生成目录和业务数据；源码、当前测试及配置仍参与搜索。旧版本设计文档、旧测试归档和已脱离入口的 UI 组件已清理，历史可从 Git 或先前发行包恢复。
