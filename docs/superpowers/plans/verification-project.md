# v2.6.0 项目能力验证

日期：2026-09-11。

实现范围：`conversation_project.py`、`project_context.py` 与 `tests/test_conversation_project_v260.py`。执行入口为 `async execute(store, engine, chat, name, args, turn_id=None)`；`turn_id` 使用控制器的稳定 command ID。

## 失败先行与验证记录

先新增真实 Store/Excel 行为测试，执行 stdlib unittest；最初明确失败于 `ModuleNotFoundError: tcg.conversation_project`。实现学习、应用、样例与冻结导出后基础 5 项通过。随后补充真实资料影响与场景联动测试，发现新 Artifact 已纳入资料而等待中的 Run 尚未纳入；与成果能力实现方协调在同一应用事务中补齐 Run 来源与输入版本后通过。

最终命令：

```bash
PYTHONPATH=backend python -m unittest discover -s tests -p 'test_conversation_project_v260.py' -v
PYTHONPATH=backend python -m unittest discover -s tests -p 'test_project_context_v2512.py' -v
python -m py_compile backend/tcg/conversation_project.py backend/tcg/project_context.py tests/test_conversation_project_v260.py
```

结果：新增 10 项通过，既有项目复用 6 项通过。既有补资料测试暴露旧的 project/chat token 键兼容问题；`supplement_run` 现在同时检查两种锁键，防止存在项目修改时创建后继任务。

验证使用真实 SQLite Store、真实模型输出验证与成果提交代码、真实 openpyxl 导出/解析；AI 为可控返回值，未替换领域方法或装入假依赖模块。

## 实际检查的行为

| 行为 | 保存状态与证据断言 |
|---|---|
| 双模板学习并应用 | 两种列、工作表分别保存；Profile v1→v2；没有创建任何生成 Run；模板内容仅放入格式参考 |
| 仅学习、稍后应用 | 模板建议持久保存；学习不改 Profile；旧 expected_version 被拒绝；应用保留更新过的业务范围 |
| 模板字段策略 | 模型把人工备注或实际结果标为 AI 时保留/恢复 manual；等待 Run 使用新 Profile，仍等待且保留 stop_after |
| 新资料纳入 | 真实 Source 的项目、用途与正文正确；跨项目资料被拒绝；单独纳入资料不修改已经发出的 Run 来源快照 |
| 资料定向更新 | `project_source_impact`→`artifact_modify`→`artifact_sync_scenarios`；只有 R1/S1 变化；R2/S2、用例版本与历史修订不变；新引用进入等待 Run；没有后继 Run或用例生成 |
| 全局影响不确定 | 返回 needs_input，说明需要扩大全部需求范围；不修改现有成果 |
| 项目格式样例 | 旧成果版本被拒绝；保存 Profile 新版本；B 新 Run 实际包含样例，但没有旧 scenario_id/refs 或人工执行字段 |
| 同命令重放 | 同 command ID 再次执行返回已保存回执；Profile 不再加版本，AI 不再次调用 |
| 明确使用生成时模板 | 当前会话后来更换 Profile 后，use_snapshot:true 仍按该成果的原工作表和列头导出 |
| 两份冻结 Excel | `files` 返回两个真实 `/api/exports/{id}` URL；冻结记录含真实 XLSX 字节；两个工作表及列头分别符合场景/用例模板；人工备注原样；随后修改源场景不会改变已导出文件 |

## 接口与边界

- `register_routes(app)` 注册 `GET /api/exports/{export_id}`；文件名、内容、版本与模板在导出时冻结，下载时不重新读取当前成果生成。
- `project.update_from_sources` 默认 `targets:['analysis','scenarios']`，只有显式包含 `cases` 才同步既有用例。支持已有附件与明确补充正文，不调用旧 `supplement_run`。
- `project.learn_template` 默认保存建议，`apply:true` 为明确学习并应用；`project.apply_profile` 支持新模板 ID 与旧模板建议 Artifact ID。两类模板独立合并，人工/默认字段策略受保护。
- 新纳入资料与已学习模板的用途记录在当前 chat 的来源角色快照；workflow.start 消费这些角色，模板不成为新业务事实。对等待 Run 的显式模板应用更新其未来阶段 Profile 与输入版本。
- 旧 supplement API 仍是明确的重新理解后继任务；它现在继承持久 stop_after/goal 与控制版本。本次对话定向更新没有把该 API 改名冒充定向处理。

## 未验证部分

当前运行环境没有 FastAPI、LangGraph 与 pytest；依赖安装未能执行（环境报告 network approval was cancelled before a decision was returned），未装假模块绕过。导出服务的真实 Excel 生成、持久字节与读取已验证，但 HTTP 路由的 FastAPI TestClient 执行尚未实测。完整运行图与用户真实内网模型识别效果由具备依赖和模型连接的环境继续验收；本记录不声称其已通过。

## 前端对接检查（静态，已报告给负责方）

检查了 App.tsx、ConversationParts.tsx、conversation.tsx、ClarificationDraftEditor.tsx 与其直接调用者。发现并已交由前端负责方处理的具体问题：导出命令需在 arguments 传成果 ID/版本；显式“生成时配置快照”需要 use_snapshot:true（领域支持及真实 Excel 回归已补）；卡片继续需绑定当前 interrupt/control 版本，防止多次保存请求之后的新 continue 越过后来暂停；会话 Profile 改变后前端不能继续提交陈旧 Profile ID；本地草稿 PATCH 期间再编辑时，成功写入响应需要推进本地 dirtyBase。历史 artifact/case_details 本身已绑定保存的 revision，纯读发送没有锁确认。完整 DOM/构建验证由前端负责方记录。
