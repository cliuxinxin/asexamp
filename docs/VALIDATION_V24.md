# v2.4.0 主流程验证

## 已执行

- `python3 -m pytest -q tests/test_flow_v24.py`：3 passed。
  - HITP：关键澄清 → 理解确认 → 场景确认 → 使用人工修改后的场景生成 → 单轮审核。
  - Auto：四个核心阶段，无人工暂停，只发布最终结果，支持导出。
  - 临时 Profile 的自定义 Sheet 和 Excel 列顺序生效，不改写项目 Profile。
- `cd frontend && node --import tsx --test tests/workspace-smoke.test.tsx`：1 passed。
  - 对话内展示结果，默认不打开侧面工作区；显式选中结果后对话修改，打开导出窗口。
- `cd frontend && npm run build`：TypeScript 检查及 Vite 生产构建通过。

## 范围

后端使用确定性模拟模型，前端交互使用 jsdom。未使用真实模型服务，不能证明实际业务覆盖质量或响应速度。浏览器连接受阻且本地浏览器二进制不可用，未完成真实浏览器截图及视觉验收。未运行全量回归、企业集成和大型文档压力测试。

已随 ZIP 提供前端构建产物；建议在新目录、新会话中验证人工确认流程。
