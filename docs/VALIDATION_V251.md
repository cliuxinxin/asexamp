# v2.5.1：实际报错回放与 HTTP 主流程验证

本轮先用用户提供的 JSON 重现了三个失败，再修改实现：场景缺少 requirement_ids、非用例 Excel 返回空列与 null 文件名、澄清后重新生成需求理解。

## 验证内容

| 验证 | 实际路径与断言 |
|---|---|
| 用户场景 JSON | 回放用户的 7 条需求、20 个场景；场景原始 JSON 没有 requirement_ids，专用关联请求补齐后继续，不改写场景正文 |
| 用户模板 JSON | 回放空 excel_columns、null filename_pattern、数组 template_rules；任务完成，列与文件名沿用当前配置，项目 Profile 不自动改变 |
| 澄清 | 相同分析 Artifact ID、相同需求条目；新版本增加问题及答案记录，没有第二次需求分析调用 |
| HTTP 主流程 | FastAPI → 真正的 HTTP chat/completions 请求 → 返回解析 → LangGraph 检查点 → XLSX 导出；模型端使用本地测试服务器回放 JSON |
| 坏 JSON | HTTP 服务第一次场景返回 `{"items": [}`，随后请求携带该原文自动修复；没有重新调用需求分析 |
| 人工流程 | DOCX 上传 → 澄清 → 方案确认 → 20 个场景 → 场景确认 → 20 条用例 → 评审 → 总结 → Excel 21 行（含表头） |
| 再次生成 | 同一会话、同一组需求来源再次生成，累计需求分析调用仍为一次 |
| 诊断包 | 含坏 JSON 原文、发送记录、JSON 修复事件、最终检查点；认证 Key 不出现在压缩包文本中 |
| 故障恢复 | 场景校验故意失败后重试可进入场景确认；澄清来源仍只有一个，需求分析仍只调用一次 |
| 重启 | 人工确认节点关闭并重新启动服务后恢复；不会重算理解或再次插入答案 |
| 前端 | 不显示生成流程横条；执行过程默认展开；来源选择与显式修改仍可操作 |

## 执行结果

```bash
python3 -m pytest -q tests/test_incident_v251.py tests/test_incident_http_v251.py tests/test_workflow_v25.py
# 14 passed
cd frontend
node --import tsx --test tests/workspace-smoke.test.tsx
# 2 passed
npm run build
# TypeScript 与 Vite 生产构建通过
```

测试 fixtures 中的原始字段内容来自用户提供的返回；回放时只将来源引用 ID 替换为测试上传实际产生的 ID。关联请求的预期结果及测试用例后续输出由测试服务器提供，不将它们冒充真实模型输出。

未连接实际内网模型，也未完成真实浏览器视觉验收。HTTP 链路验证证明本次给出的失败返回可被处理，并不证明模型对任意需求的业务理解与覆盖质量。未运行全量历史回归。

## 调试

运行中、失败后及完成后的对话均可下载“完整诊断包”。其中每个 call_id 对应发送 JSON 和原始返回文本；diagnostics.json 包含失败节点、校验路径、最近 500 条事件、缓存键、Profile 快照、检查点状态及等待节点。原始回答保存在当前数据目录的 SQLite 中，普通 tcg.log 仍不记录完整文档和认证头。

这次不会要求通过反复新建会话绕开问题；需求理解复用发生在同一会话内。来源增加、删除或业务用途变化会使已有理解失效，以避免沿用过期需求。
