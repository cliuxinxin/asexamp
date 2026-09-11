# v2.7.1 定向验证

日期：2026-09-11。真实 FastAPI HTTP、LangGraph、SQLite 检查点；模型和路由响应受控。所有下列统计针对本次改动，不代表全量历史测试。

## 最终结果

- 后端：98 passed，0 failed；一条已有 Starlette/AnyIO 弃用提示。
- 前端：36 passed，0 failed。
- TypeScript 检查与 Vite 生产构建通过；保留已有 Mermaid 分块体积提示。
- `git diff --check` 通过。

```bash
python -m pytest -q --show-capture=no --tb=short tests/test_manual_gates_v271.py tests/test_mode_contract_v271.py tests/test_selection_contract_v271.py tests/test_dependency_diagnostics_v271.py tests/test_manual_journey_v271.py tests/test_framework_workflow_v270.py tests/test_framework_storage_v270.py tests/test_framework_context_v270.py tests/test_framework_turn_v270.py
```

```bash
cd frontend
node --import tsx --test tests/human-gates-v271.test.tsx tests/coverage-contract-v271.test.tsx tests/conversation-v260.test.tsx tests/settings-capacity.test.tsx tests/source-impact.test.tsx
npm run build
```

## 关键证明

| 风险 | 验证结果 |
|---|---|
| UI 人工被模型 Auto 覆盖 | 真实 TCP 请求故意给相反模式，新任务仍执行 UI 模式；Auto 反向情况也通过 |
| 缺少草稿与评审结果确认 | Human 四次明确确认；草稿未确认不调用评审；最终未确认不结束任务 |
| 只测接口按钮，未测聊天 | 23 次普通文本 `/turns` 请求串联一条真实任务；请求未指定命令或 Intent |
| 只读操作意外修改或推进 | 比较完整成果内容、版本、等待节点及主模型调用数 |
| 澄清采用被误当项目事实 | 采用仅编辑草稿；提交、共享后同项目另一对话才复用 |
| 选中条目被旧焦点覆盖 | 模型省略目标时，UI 选中的 SC1 优先；只修改其关联场景和用例 |
| 新资料影响、更新与联动 | 精确校验新 source ID、原文、版本、引用；无关条目保持不变 |
| 覆盖数据显示 0 | 前端使用真实嵌套返回结构校验数量、关系和过期提示 |
| 最终确认覆盖人工评审说明 | 修改后的说明及显式空列表均保留；确认不增加成果 revision、不重复评审 |
| 旧预览带着过时的继续动作 | 新的“停在用例”指令取消旧继续；应用修改仍保存成功 |
| 模型 JSON 正确但输入过期 | 实际来源变化时仍拒绝保存，提示具体依赖和恢复方式；重试不能混用旧批次 |
| 内部标记引起误报 | 展示/调度标记不改变业务摘要；真实来源、Profile、成果、报告和范围变化仍拒绝 |
| 旧检查点升级 | 新草稿等待可重启恢复；另用原 v2.7.0 图实际生成的检查点验证旧场景确认、旧用例停止点恢复，无重复前置阶段 |
| 导出混用历史与最新版本 | 当前场景和用例的 Excel 单元格校验；先前导出文件字节保持不变 |

另对完整生成执行分组 × 分页 × 模板字段补全的 8 个组合，全部完成。没有发现普通分页、报告发布或历史多版本祖先自身必然触发依赖冲突。

## 适用边界

这些测试验证给定模型判断下的系统执行行为，不证明任意真实模型能识别所有自然语言。尚未连接用户内网模型。浏览器尝试访问本地服务时被 URL 策略拒绝，页面未加载，因此未完成真实浏览器连续操作和视觉验收。

用户提供的旧日志缺少依赖版本与具体差异，无法断定它由哪项变化触发；本次修复的是已复现的误报，并补齐未来诊断。真正改变输入后，普通重试仍保留原输入保护；需要明确从最新上游版本重新生成受影响阶段。

旧任务保留原确认顺序。四步人工演示请新建任务，并按 [DEMO-v2.7.1.md](DEMO-v2.7.1.md) 连续执行。
