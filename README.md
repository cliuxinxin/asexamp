# TCG Case Agent 2.3.0

本地测试用例工作台：左侧项目与资料、中间可编辑用例、右侧持续对话。UI 参考 execution-agent-UI 的白色导航、玫红强调色与表格/助手并排布局；未复制其执行平台或后端。

## 启动

需要 Python 3.11+，推荐 3.12。ZIP 已包含编译后的前端，正常使用不需要 Node.js。

1. 解压到新目录，复制 `.env.example` 为 `.env`，填写原有模型地址、名称和 Key。
2. 运行 `python3 start.py`（Windows：`py -3.12 start.py`）。首次安装 Python 依赖需要联网。
3. 打开 http://127.0.0.1:8000 ，上传需求后点击“生成测试用例”，或直接在助手中粘贴需求。
4. 结果自动进入中间表格。展开查看步骤和原文依据，使用“编辑”改字段，或选中用例后让 AI 局部修改，最后导出 Excel。

仍使用原来的私有网关协议：`POST /api/v1/chat/completions`，Key 默认放入 `X-API-Key`。不要求 tools、embedding 或模型列表接口。默认请求仍只有 model/messages；连接设置和自定义请求头沿用上一版。

## 新的默认流程

- 新任务使用 `experience=reliable`、`graph_version=5`。
- 生成用例：本地解析 → 整份需求一次生成 → 本地格式/引用检查 → 保存。
- 不再默认运行独立的意图识别、需求分析、场景生成和 AI 审核。用户显式选择“分析需求”“生成场景”“评审用例”时，仍可调用原有专项流程。
- 模型发现阻碍生成的关键歧义时集中提出最多三个问题；补充后继续。非阻塞问题进入覆盖说明，不强制停下。
- 单条格式错误只尝试局部修复一次；无法修复的条目隔离显示，合法用例保留。结果不代表实际执行通过。
- 只有完整请求超过配置容量才分组；优先使用 DOCX 标题、Markdown 标题、编号章节和来源边界。大章节仍可能拆为相邻原文组。分组时先提取各组共同规则，再携带全局摘要生成。
- 输出过长时续写剩余用例，不重新分析需求。已验收页面和有效草稿保留，来源引用保持原始 ID。
- 全局摘要辅助理解，具体断言仍需本组原文支持；大文档跨章节语义覆盖需人工复核，未保证自动发现所有跨组组合。

## 容量配置

在 `.env` 中按实际模型能力填写，修改后重启：

```dotenv
TCG_MODEL_CONTEXT_TOKENS=32768
TCG_OUTPUT_TOKENS=8192
# 仅当私有网关支持 max_tokens 请求字段时启用：
TCG_SEND_OUTPUT_LIMIT=false
```

输入估算包括系统提示、任务约束、需求、配置和历史，并预留输出空间。当前使用保守的跨模型字符估算，不是指定模型的精确 tokenizer，不自动探测模型容量。Ollama 同时配置 num_ctx/num_predict；私有网关默认只将输出设置用作预算预留，避免改变原有请求协议。若模型支持更大的窗口，可相应提高，例如 131072，但不得超过实际限制。

`TCG_CONTEXT_CHARS` 仅继续用于旧版/专项多阶段流程，不再决定默认生成用例的分批。原文提取上限仍为 200 万字符。扫描 PDF 需要 OCR；可选 Docling 路径本次未验证。

## 数据与恢复

默认保存在 `data/`，日志位于 `data/logs/tcg.log`。旧版 graph 4 的进行中任务继续使用旧路径，新会话采用 graph 5。建议先用新目录试跑，再按需要指定已有数据目录。未修改旧目录的数据或推送 GitHub。

```bash
python3 start.py --port 8080 --no-browser
python3 start.py --data-dir ./data-v23 --env-file ./.env
# 当前环境已经安装所需依赖时
python3 start.py --no-install --no-browser
```

运行中可以查看有效草稿；最终结果与诊断详情分开。错误不会隐藏原始需求。失败可重试未完成步骤，已验收页面复用。异常中断边界可能重复外部模型请求。

服务沿用本机回环访问限制，不是公网多用户服务。Docker、内网模型真实性能、大规模语义质量及全部旧接口回归未纳入本次验证。

## 本次验证

按要求只走主流程：API 上传来源 → 一次生成 → 对话局部修改 → Excel 导出，以及关键澄清、单条修复和超容量分组。使用模拟模型，不能据此推断真实模型耗时和业务覆盖质量。前端执行 TypeScript 检查与生产构建。详见 `docs/VALIDATION_V23.md`。

```bash
PYTHONPATH=backend python3 -m pytest tests/test_direct_smoke.py -q
cd frontend
npm ci
npm run build
```

主流程实现：`backend/tcg/direct.py`；持久化与原路径保留在 `storage.py` / `reliable.py`。历史文档与旧测试保留供参考，旧 graph 4 的调用次数断言不代表 v2.3 默认行为。
