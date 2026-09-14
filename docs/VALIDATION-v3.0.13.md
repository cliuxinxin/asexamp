# v3.0.13 定向验证

验证环境：Python 3.12、本地 SQLite、真实 FastAPI/HTTP 和 LangGraph；模型返回使用可控响应。前端使用 React 19、JSDOM、TypeScript 与 Vite 生产构建。

| 检查 | 结果 |
| --- | --- |
| 后端定向测试 | 41 通过 |
| 前端组件与 App 交互 | 23 通过 |
| TypeScript 和生产构建 | 通过 |
| 实际 Excel 单元格/列顺序、case/step 布局 | 通过 |
| 逐格取舍和人工修改通过原评审节点 | 通过 |
| Supervisor 保存后只导出一次最终版本 | 通过 |
| 旧版本、过期提示、重复提交及无效引用 | 通过 |
| 保留未保存输入、布局切换后的混合取舍 | 通过 |
| 历史资料停用后的只读导出 | 通过 |
| 实际浏览器显示/截图验证 | 未完成：浏览器访问本地演示地址返回 ERR_BLOCKED_BY_CLIENT |
| 用户实际 Azure/内网模型 | 未连接验证 |

后端测试文件：test_table_projection_v313.py、test_table_review_v313.py、test_table_review_supervisor_v313.py、test_side_review_scope_v313.py、test_review_proposals_v310.py、test_supervisor_pipeline_v312.py、test_manual_tables_v310.py。

前端测试文件：full-screen-review-v313.test.tsx、table-review-entry-v313.test.tsx、artifact-preview-v310.test.tsx、execution-plan-v312.test.tsx。测试使用 --test-timeout=30000，避免单项检查无限等待。

构建保留已有 Mermaid 解析包的大体积提示；没有新增第三方表格依赖。发行包启动校验及文件清单见 RELEASE-MANIFEST.json。此次未运行全部历史测试。
