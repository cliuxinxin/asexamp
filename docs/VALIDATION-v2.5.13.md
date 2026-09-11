# v2.5.13 验证记录

日期：2026-09-11

## 本次范围

用户在普通聊天框直接请求按已有场景估算测试用例数，无需选择任务或点击估算按钮。复用 v2.5.12 的独立估算能力，保持原工作流确认状态和成果版本。

## 已执行

| 检查 | 结果 | 证据范围 |
|---|---|---|
| `python3 tests/test_chat_estimate_v2513.py -v` | 20/20 通过 | 实际 Store/SQLite、现有 estimate action、真实路由注册闭包；分类及估算响应为可控替身 |
| `node --import tsx --test tests/chat-estimate-v2513.test.tsx` | 6/6 通过 | 实际 App 的聊天提交与结果渲染，HTTP 用替身返回 |
| workspace-smoke 中 confirmation is disabled / wide conversation | 2/2 通过 | 等待确认锁、普通生成与固定目标修改、结果及导出入口 |
| interactions 中 delayed send preserves newer text / rejected message retains / only explicit requirement checkbox | 3/3 通过 | 发送中更新草稿、切会话、请求失败保留内容、显式需求正文旁路 |
| Python 编译与挂载位置检查 | 通过 | main / chat_estimate / model / 新后端测试；路由在 SPA catchall 之前，CSS 已导入 |
| `npm run build -- --emptyOutDir` | 通过 | TypeScript 检查、Vite 生产构建；本轮捕获 101 个 dist 文件 |

构建保留了 Mermaid 等大分块的既有体积提示，不影响产物生成。没有为了本次功能改动分包方式。

## 核对的行为

- 上一次任务选项为 generate_case 时，直接估算仍只走 interpret，不新建生成任务。
- 等待确认状态、interrupt、场景版本保持不变；实际估算期间复用锁，取消/版本变化后的响应不发布。
- 逐场景数量范围经服务端校验，合计由服务端计算；聊天保存来源 ID/版本、行范围、理由和假设。
- 连续估算保持来源；先选第 2、3 个场景再追问仅异常时，由服务端继承该子集。明确全部场景才扩大。
- 暂停场景优先旧显示成果；跨会话/跨项目目标拒绝；歧义、多份同名或无场景时不猜测生成。
- 元数据按模型容量缩减，保留当前来源锚点；路由无需求正文、场景正文、用例步骤或 Profile 样例。附件名称和用途统计保留，供普通消息识别使用。
- 普通问答在澄清节点进入 dialogue，不作为澄清答案提交；非估算请求返回既有任务类型，不重复走旧自动路由。
- 更新相关历史测试的固定页面标题及新增 interpret 模拟响应，使草稿测试继续检查实际行为；未修改产品标题来迁就旧测试。

## 验证边界

未连接真实内网模型，因此以上不能证明任意自然语言都能识别正确，也不校验估算数字的业务合理性。运行环境缺少 FastAPI/LangGraph 完整依赖，没有运行真实 HTTP/LangGraph 端到端主流程。前端使用 DOM 交互检查，未执行浏览器截图验收；没有运行全量历史测试。

本版仍保留显式的修改目标、正文作为需求和继续按钮入口；普通聊天估算不等于通用多动作调度器。运行中的可靠模式需等待安全确认节点或任务完成后再发送普通消息。

真实模型串测见 `demo/ALL-SCENARIOS-TEST.md`；重点估算问法见 `demo/CHAT-ESTIMATE-TEST.md`。以状态、目标、范围、引用及版本为通过标准，不固定模型必须生成的数量。
