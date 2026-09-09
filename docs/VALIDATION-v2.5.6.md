# v2.5.6 定向验证

日期：2026-09-09。

## 结果

- 后端 12 项通过：新增通用模板的学习建议 → 应用 Profile → 需求理解 → 场景 → 用例 → 评审 → 实际 XLSX 单元格检查；换模板后只补所选行的缺项；错误补全响应的定向修复；缺业务依据时记录原因而非失败循环；对话修改保护人工数据并检查自定义字段。
- 兼容验证覆盖 description 历史字段别名、Auto 阶段展示、HITP 确认与澄清不重新理解需求、JSON 定向修复和评审覆盖校验。
- 前端 5 项组件检查通过，包括动态字段导出检查、具体待澄清原因、人工字段提示、直接填写缺失字段、补全操作、聊天结果与 Profile 定义编辑。
- TypeScript 检查和生产构建成功，完整 ZIP 内置 dist。

后端调用命令：

```sh
python3 -m pytest -q tests/test_template_fields_v256.py tests/test_description_v255.py tests/test_feedback_v254.py tests/test_incident_http_v251.py --tb=short --show-capture=no
```

前端调用命令（frontend 目录）：

```sh
node --import tsx --test tests/workspace-smoke.test.tsx
npm run build
```

模型响应使用可控测试替身，其中 HTTP 主流程通过本地 HTTP 服务传输。已检查程序状态、实际 API、保存结果及 XLSX 内容；未连接用户实际使用的模型服务，未完成真实浏览器视觉验证。没有运行全量旧版测试。已知非阻断提示：pytest 的既有 asyncio_mode 配置、Starlette 弃用提示和 Mermaid 大分块构建提示。
