# TCG v2.9.0 验证记录

验证日期：2026-09-12。使用真实 SQLite、HTTP、LangGraph 和受控模型响应；没有连接用户实际模型服务。

## 统一验证结果

- 后端定向验证：**103 passed**，14.83 秒。包含连续人工/自动主流程、四个确认点、即时澄清、项目事实、指定资料局部采用、精确父级版本、模板学习/样例/Excel、上下文预算与并发边界。
- 前端定向 DOM 交互：**48 passed**，7.27 秒。包含宽对话单份成果、侧栏/紧凑输入、选择范围与筛选顺序、历史快照、编辑期间禁止确认、行内关联、完整步骤、评审信息及澄清采用。
- TypeScript 检查和 Vite 生产构建：**通过**。构建保留既有 Mermaid 单个解析包超过 500 kB 的提示；测试依赖有一条 Starlette/AnyIO 废弃提示。
- `git diff --check`：通过。
- 原版 UI 参考：读取 `ly061/execution-agent-UI` 的原 CSS，采用其红色强调 `#d31145`、白色侧栏、浅灰背景与原文字色；没有增加外部字体网络依赖。
- 实际浏览器验证：本次尝试访问本地运行页面，Browser 返回 `net::ERR_BLOCKED_BY_CLIENT`。未绕过限制，**未完成浏览器视觉与真实点击验收**。

## 验证命令

后端（Python 3.12，项目根目录；先安装 `requirements.txt` 和 pytest）：

```bash
PYTHONPATH=backend python -m pytest -q tests/test_conversation_inputs_v290.py tests/test_conversation_journey_v290.py tests/test_project_facts_v290.py tests/test_project_fact_isolation_v290.py tests/test_clarification_flow_v290.py tests/test_targeted_sources_v290.py tests/test_manual_gates_v271.py tests/test_framework_generation_v270.py tests/test_dependency_diagnostics_v271.py tests/test_framework_context_v270.py tests/test_framework_context_receipts_v270.py tests/test_conversation_project_v260.py tests/test_scenario_templates_v258.py tests/test_template_fields_v256.py tests/test_conversation_boundary_v260.py tests/test_manual_journey_v271.py::test_auto_composer_completes_the_same_real_stages_without_manual_gates
```

前端（`frontend` 目录，`npm ci` 后）：

```bash
node --import tsx --test tests/conversation-layout-v290.test.tsx tests/artifact-lineage-v290.test.tsx tests/clarification-v290.test.tsx tests/workspace-v280.test.tsx tests/phase-navigation-v280.test.tsx tests/human-gates-v271.test.tsx
npm run build
```

## 本轮交叉检查修复的具体问题

1. 当前澄清采用后只存草稿、未刷新理解；修为即时提交/共享/刷新，仍停在理解确认，部分未答问题保留。
2. 默认输入与普通问答会复用旧任务假设；修为当前任务范围隔离，并在模型上下文标明 provisional。
3. 新附件使无关确认被阻塞、用例更新强制返回理解；修为明确资料/目标/所选范围，仅实际依赖过期时拦截。
4. 同场景两个用例只同步一个时，父级版本被一起标新；补逐用例版本，工作区、依赖和后续模型读取保持一致。
5. 发布消息没有固定成果版本；现在保存该消息发布时的 revision，历史回看不再随当前成果变化。
6. 另一对话替代项目规则误伤正在生成的任务；只对有真实替代记录且证据内容完全未变的规则退休状态兼容。正文、文本块、范围、角色和普通停用变化仍被阻止。
7. 同对话保存无关项目事实会暂停 Auto；能力自身声明是否影响任务边界，确认共享事实不隐式采用到运行任务，任务假设仍需要安全边界。
8. 提示词的 `provisional:true` 与事实能力的 `status` 不一致，可能把假设共享；两种写法统一归一化，任何明确暂定假设均禁止共享。

## 范围与兼容性

没有运行全部历史测试。早期测试仍包含“保存默认不共享”“未指定附件一律采用”“用例更新必须先更新理解”及旧并排界面的断言，它们不代表 v2.9 的批准契约；本轮对应行为有新定向测试。

统一验证最初发现一条旧场景导出测试在生成后直接篡改 immutable artifact Profile。测试改为在生成请求中传入相同 Profile，再验证真实 Excel、默认场景列、所选范围和不调用用例补全逻辑；没有放宽成果不可原地修改的保护。

前端 lockfile 中几处旧版本号与 integrity 不匹配，按实际包的官方元数据恢复。直接依赖和包内容的 integrity 保留；`npm ci` 与构建通过。

自由文本路由、事实语义冲突识别、补全内容质量取决于接入模型。定向测试控制外部模型返回，证明执行顺序、范围、版本、引用和保存行为；不等于已经验证用户内网模型的理解准确率。项目共享依赖同一后端，未新增多账号权限系统。
