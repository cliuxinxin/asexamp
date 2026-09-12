# v2.8.0 交付验证

验证时间：2026-09-11。基于 v2.7.1 的独立修改分支；固定工作流版本仍为 7，人工确认契约仍为 2。

## 本轮结果

| 范围 | 结果 |
| --- | --- |
| 后端定向回归 | 139 通过；1 条已有 Starlette 弃用提示 |
| 前端定向交互 | 29 通过，无跳过 |
| TypeScript 与 Vite 生产构建 | 通过，最终 dist 从空目录构建 |
| 连续 HTTP / LangGraph 操作 | 6 条通过，包含 Auto 与人工流程 |
| 独立代码审查 | 已修复发现的范围、分支、自动复用、资料排除及上下文问题，并复核对应复现 |
| 真实模型 | 未验证用户实际模型服务 |
| 浏览器视觉验收 | 本地页面访问受策略限制，未完成；未把 DOM 测试称作截图验收 |
| 历史前端全量测试 | 尝试未通过，未全部迁移。抽查的两项澄清/微调旧断言在基线 v2.7.1 同样失败；不能由此认定其余失败均为历史问题 |

## 六条连续操作

1. 生成到用例草稿确认；保存场景修改；旧用例确认被拒绝；预览、应用；确认最新草稿；评审并完成原任务。
2. 场景确认时回改需求理解；定向更新原场景；新增独立规则纳入理解和场景；继续生成用例。
3. 提交澄清；理解条目、报告与引用实际更新；场景和用例读取该理解。
4. 无修改的 Auto 执行原有全部阶段。
5. 从场景页直接通过聊天补充规则；预览归属需求理解；重复消息不重复保存资料或调用模型；应用后保留场景确认点。
6. 新 Auto 任务尝试复用上游已变化的场景；生成前暂停；统一更新后明确继续，使用最新场景完成。

测试使用真实 HTTP、LangGraph、SQLite、上传、版本与确认请求，仅控制外部模型响应。它们证明执行契约，不证明任意自然语言都能被实际模型正确理解。

## 额外风险检查

- 明确指定的成果分支不会被替换为较新的同级分支；所选行不会静默扩大为整个分支。
- 合法的跨分支修改预览仍可在工作区查看和应用；应用保留版本及取消检查。
- 只改报告措辞不使场景过期；修改业务规则会保守标记关联需求，局部同步后其余变化仍可见。
- 本轮明确排除的旧资料不会被强制采用；运行开始后上传的新资料仍进入待处理状态。
- 大报告不被完整复制到每次修改指令。可选展示字段使用有界投影；必要业务规则继续受真实容量检查，不静默丢弃。
- 前端检查单一成果展示、单一应用入口、阶段自动跟随、手动回看、分支确认归属、评审导航、项目模式选择及紧凑聊天回执。

## 复测命令

从项目目录运行：

```bash
python -m pytest -q tests/test_change_journey_v280.py tests/test_workspace_changes_v280.py tests/test_requirement_refresh_v280.py tests/test_manual_gates_v271.py tests/test_manual_journey_v271.py tests/test_framework_workflow_v270.py tests/test_framework_reconciliation_v270.py tests/test_conversation_boundary_v260.py tests/test_framework_turn_v270.py tests/test_framework_context_v270.py tests/test_framework_impact_v270.py tests/test_conversation_project_v260.py tests/test_workflow_v25.py tests/test_conversation_workflow_v260.py tests/test_framework_http_v270.py tests/test_workspace_coverage_v2512.py
```

从 frontend 目录运行：

```bash
node --import tsx --test tests/workspace-v280.test.tsx tests/phase-navigation-v280.test.tsx tests/human-gates-v271.test.tsx tests/coverage-contract-v271.test.tsx tests/source-impact.test.tsx
npm run build
```

现场演示操作见 [DEMO-v2.8.0.md](DEMO-v2.8.0.md)。升级时先停止旧服务，保留原数据目录及模型配置，使用新目录启动。
