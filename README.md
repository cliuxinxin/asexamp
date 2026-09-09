# TCG Case Agent 2.2.0

可在本机运行的测试用例生成系统。包含 FastAPI 后端、React 编译前端、LangGraph 固定流程、SQLite 检查点、文档解析、人工确认、项目记忆、版本编辑和 Excel 导出。新版界面默认使用 `experience=reliable`（graph_version=4）。

## 三步启动

需要 Python 3.11+（推荐 3.12）；首次安装依赖需要联网。已包含前端构建产物，正常运行不需要 Node.js。

1. 解压到一个**新目录**。复制 `.env.example` 为 `.env`，填入你的真实 Key：

```dotenv
TCG_MODEL_PROVIDER=openai
TCG_MODEL_BASE_URL=http://10.206.3.151:8000/api/v1
TCG_MODEL_NAME=gpt-5
TCG_API_KEY="你的真实密钥"
TCG_MODEL_TIMEOUT_SECONDS=300
```

2. 在解压目录运行：

```bash
python3 start.py
```

Windows 使用 `py -3.12 start.py`。启动器自动创建虚拟环境、安装依赖、启动服务并打开浏览器；终端保持运行。

3. 打开 `http://127.0.0.1:8000`，在设置中测试连接，然后新建对话、上传需求、选择设计深度和用例类型，发送“生成测试用例”。

机器必须能够访问上述内网地址。未配置或连接失败时会明确报错，系统没有假模型兜底。演示建议先上传 `examples/login-requirements.md`（虚构示例），选择快速设计和 Business 类型确认真实模型连通，再用实际大文档测量耗时。

## 演示操作

- 上传一个或多个 PDF、DOCX、XLSX、XLS、CSV、TXT、MD 文件。单个失败不阻止其他文件上传；相同内容和角色在同一会话中去重。
- 分类默认“自动识别”，当前是保守的文件名规则，结果标为待确认；在来源面板修改为主需求、变更、补充、澄清、知识或示例。示例只参考格式，不作为业务依据。
- 生成前选快速、标准或深度，以及 Business / Negative / Boundary / Security 类型。每次运行保存配置快照。
- Auto 自动执行；HITP 在需要澄清或确认场景时等待。按界面提供的按钮继续。
- 流程展示当前阶段和已完成批次。失败后查看原因，修复连接/配置再点重试；已验收批次不会从头生成。模型持续返回不合法数据时会有限次修复后停下，不无限循环。
- 点击成果卡片打开工作区，编辑、查看引用和历史版本、导出 Excel。
- 在“项目记忆”中添加已确认偏好或规则，同项目后续会话使用；删除后不影响已有运行快照。最多50条、合计8000字符。需要作为用例引用依据的业务规则仍应上传或粘贴为需求/澄清来源。

## 为什么本版更可控

生成主流程固定为：需求分批分析 → 必要澄清 → 场景分页 → 用例分页 → 一轮分批审查 → 发布。不会自由规划后反复回到分析。

每批处理状态、验收输出、页游标和检查点写入本地 SQLite。进程中断后重启可恢复正在执行的任务；失败任务由用户手动重试。外部模型调用在崩溃边界可能重复，但验收结果与成果写入保持幂等。

引用错误集中反馈，保留已通过条目及稳定编号，最多两次结构修复。覆盖检查要求已解析的来源块有需求引用或明确排除原因、需求有关联场景、场景有关联用例；这些是结构覆盖，不等于语义完整性或测试执行成功。

请求按完整提示词和上下文字符预算分批，避免一次塞入所有大文档。超过单条预算时明确提示；不会静默删除证据。快速模式减少设计展开，实际速度仍取决于文档、模型响应和输出量。

## 模型协议与配置

OpenAI provider 直接用 HTTPX 调用 `POST /api/v1/chat/completions`：

- `TCG_API_KEY` 发送为 `X-API-Key`，不是默认 Bearer。
- 请求只含 `model` 与 `messages`；内容使用 `[{"type":"text","text":"..."}]`。
- 不假定网关支持 tools、JSON mode、stream、temperature、embedding 或模型列表接口。
- 读取 `choices[0].message.content` 的 JSON 文本；只有 `finish_reason=stop` 接受为完整响应。
- 地址可填完整 endpoint 或 `/api/v1` 基地址。默认300秒超时可配置；认证失败不会盲目自动重试。
- `.env` 选择顺序：显式 `--env-file`，否则数据目录 `.env`，否则项目目录 `.env`；进程环境变量最后覆盖。修改后重启。密钥只在后端使用，不进入前端构建产物。

可选配置：

```dotenv
TCG_CONTEXT_CHARS=32000
TCG_MAX_UPLOAD_MB=100
TCG_MAX_PAGES_PER_BATCH=30
TCG_DOCUMENT_PARSER=native
```

字符预算不是 token 数，应根据网关限制调整并预留输出空间。原生解析最多200万字符，Office 解压内容最多100MB；扫描 PDF 默认提示无可提取文本。可选安装 `requirements-docling.txt` 后设置 `TCG_DOCUMENT_PARSER=docling`，首次模型资源下载和 OCR 环境需单独准备。此可选路径未在本次环境验证。

## 运行、数据和排错

```bash
python3 start.py --port 8080 --no-browser
python3 start.py --data-dir ./data-v22 --env-file ./.env
python3 start.py --check
# 已手动安装 requirements.txt 的环境
python3 start.py --no-install --no-browser
```

默认数据位于 `data/`，日志位于 `data/logs/tcg.log`。停止服务后备份整个数据目录，恢复时使用原目录。使用单 worker；不要同时启动多个进程共享数据库。

本包基于可取得的 v2.1.0 源码新增可靠流程，不包含你部署日志中的 `incremental_agent.py`，因此不能视为 graph_version=3 数据的兼容升级。请先在新目录和新数据目录运行；原系统和数据保留。

遇到失败：先在设置中测试连接，再看运行错误和当前批次；必要时导出诊断信息。401/403 检查 Key，超时检查内网与模型服务，响应截断检查网关输出限制，结构/引用错误查看集中校验反馈。诊断请求可能包含需求原文，分享前检查内容。

Docker 为可选启动方式：`docker compose up --build`。默认仅映射本机8000端口并使用命名数据卷；本次未验证 Docker 实际构建。此版本适合单机演示，未提供账号权限或多用户生产部署。

## 开发与验证

```bash
python -m pip install -r requirements.txt
python -m pytest -q
cd frontend
npm ci
npm test
npm run build
```

验证范围见 `docs/VALIDATION_V22.md`。测试用 HTTP 模型只在 tests 中，不进入生产运行路径。大文档分批不保证自动识别所有跨批次冲突；存在冲突或高风险规则时请用 HITP 审核，并核查成果引用。没有承诺固定秒数完成或达到微软 Copilot 的全功能范围。

架构：`backend/tcg/reliable.py` 为新版工作流，`model.py` 为私有接口适配，`storage.py` 为持久化，`documents.py` 为解析，`frontend/src` 为界面。保留旧接口供兼容参考；`docs/LEGACY_README_V21.md` 等旧文档不代表新版默认行为。
