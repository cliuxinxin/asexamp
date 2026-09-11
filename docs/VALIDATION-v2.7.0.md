# TCG v2.7.0 定向验证

验证日期：2026-09-11。运行环境使用真实 FastAPI/HTTP、LangGraph 1.2.11、SQLite 检查点和固定项目依赖；业务模型响应由测试控制。没有调用用户内网模型。

## 最终结果

- 后端定向测试：101 passed，0 failed。现有 Starlette/AnyIO 弃用提示 1 条。
- 前端定向交互：5 passed，0 failed。
- TypeScript 检查及 Vite 生产构建：成功。保留已有 Mermaid 分块大小提示。
- git diff --check：通过。

## 后端命令

```bash
python -m pytest -q --show-capture=no --tb=short tests/test_framework_*_v270.py tests/test_workflow_v25.py tests/test_workspace_actions_v2512.py tests/test_backend_model.py tests/test_model_request_inspection.py
```

覆盖：真实主流程 Auto/HITP 与确认节点、仅用自然语言提交的连续 HTTP 对话、估算/解释/只读评审、场景和用例定向联动、补资料影响及更新、覆盖表和实际 Excel 单元格、不可变版本/历史证据、项目/版本范围保护、完整必要上下文分组、输出预算和既有自定义网关兼容、跨章节规则归并及缓存复用、输入变化时拒绝旧结果、上下文回执、旧等待编辑后的确认版本。

持久对话专项验证：应用回执提交后中断、后续动作部分完成后中断、后续动作完成但未返回时中断、回答待答事项后中断、已处理待答事项恢复、同条消息修改后继续、预览应用后继续、外部暂停或控制版本变更仍阻止旧继续。检查已保存修改不会重复应用，未完成动作可以恢复。

## 前端命令

```bash
cd frontend
node --import tsx --test tests/settings-capacity.test.tsx tests/source-impact.test.tsx
npm run build
```

覆盖模型容量设置、服务端限制声明、确认携带版本绑定，以及资料影响卡片的范围、关联结果、不确定性和内部信息隐藏。

## 实际使用边界

- 这些验证证明程序如何处理给定模型结果；不能证明实际模型能理解所有自然语言说法。请按演示文档连接实际模型再走一遍。
- 来源分组会记录输入覆盖；跨章节归并使用候选关系检查，明确报告未解决冲突和检查范围，不声称业务关系绝无遗漏。
- 模型窗口未知时采用 32768 tokens、输出预留 8192；设置应与实际服务一致。必要依赖过大时明确报容量不足，不丢弃证据换取表面成功。
- 历史版本若从未保存对应证据，不能追溯恢复；系统提示历史证据缺失。新生成和修改保存精确来源版本。
- 本轮未运行全部历史测试。历史测试中直接改写版本表或用 Store.put 伪造新版本的旧构造方式不再符合不可变提交契约。
- 升级先停止旧服务并备份数据。保留数据及模型配置；演示使用新会话。项目共享使用同一服务，未增加成员账号与权限系统。

连续演示见 [DEMO-v2.7.0.md](DEMO-v2.7.0.md)。
