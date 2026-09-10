# v2.5.8 定向验证

本轮沿用已确认方案，只检查三项新需求及相关主流程，没有重跑全部历史测试。

## 验证路径

- 选择任务时填入具体提示；仅替换空白或上一任务默认文本，保留用户草稿。
- 可采用单个或全部建议答案，保留手动编辑，点击提交前不调用继续接口。
- 后端筛选当前问题的有效建议；答案和依据须非空，引用须来自当前批次的非示例证据。无效、重复或格式错误的建议被忽略，不阻断人工回答。
- 实际上传 TXT 需求与 XLSX 场景模板，学习后生成 proposal，经版本校验保存到 Profile；原用例列、工作表和文件名保持不变。
- HITP 依次完成澄清、需求理解确认、场景确认、生成、评审。已采用的答案进入后续 global_requirement_map，需求理解只执行一次。
- 场景默认导出和按所选 Profile 导出使用独立场景列；选中条目、列表换行、工作表名清理、公式文本防护均有定向覆盖。
- 实际读取导出的 XLSX 单元格、列名与工作表名，确认场景及用例均可导出；导出过程中没有模型调用。
- 保留 v2.5.7 修改期间禁止确认、修改后继续使用最新场景的并发保护。

## 验证边界

业务模型调用使用确定性替身，检查程序状态、上下文和导出行为；未连接真实内网模型，也未验证超大文档的语义质量。建议答案的引用有效不代表内容已经由用户确认，仍需在提交前核对。

## 本地结果

- 后端新功能、完整 HTTP 主流程及相关 Auto/HITP/并发兼容检查：18 passed。
- 既有用例模板字段与描述导出兼容检查：9 passed。
- 最终前端引导交互与工作区检查：21 passed。另行验证旧 proposal 兼容场景通过；TypeScript 与生产构建通过。
- 实际浏览器预览访问被运行环境拦截（ERR_BLOCKED_BY_CLIENT），本轮采用组件 DOM 交互测试检查按钮、输入、对话框及下载行为，未完成浏览器视觉验收。
- 模板应用修复：聊天发布 config + template_kinds；在学习来源与目标 Profile 不同时，仅合并本次识别模板类型的设置，目标 Profile 的另一种模板与用户已编辑的对应字段保留。
- Python 测试出现第三方 anyio BlockingPortal 弃用提醒，无测试失败。

运行命令：

```bash
PYTHONPATH=backend /tmp/tcg-v257-venv/bin/python -m pytest -q tests/test_guided_v258.py tests/test_guided_mainflow_v258.py tests/test_scenario_templates_v258.py tests/test_edit_race_v257.py tests/test_workflow_v25.py::test_hitp_gates_and_latest_scenario tests/test_workflow_v25.py::test_auto_keeps_stages_and_no_pauses
PYTHONPATH=backend /tmp/tcg-v257-venv/bin/python -m pytest -q tests/test_template_fields_v256.py tests/test_description_v255.py
cd frontend
node --import tsx --test tests/guided-v258.test.tsx tests/workspace-smoke.test.tsx
node --import tsx --test --test-name-pattern='scoped proposal|App carries learned|template proposal starts' tests/guided-v258.test.tsx tests/interactions.test.tsx
npm run build
```


- 最终代码复核：模板类型发布与跨 Profile 应用两项修复均通过复核，未发现新的正确性问题。
