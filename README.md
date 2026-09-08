# TCG Case Agent · 本地 + 云端聊天版

前端是聊天界面，后端使用 **Python + FastAPI + LangGraph + LangChain**。项目、会话、需求、用例、版本和检查点保存在本机 SQLite，上传原件保存在本地目录。默认接本机 Ollama，也支持自行配置 OpenAI 兼容接口。

本包包含完整源码和已编译的前端。正常使用只需要 Python，无需安装 Node、Cloudflare、Redis、外部数据库或云存储。

## 云端部署

现已增加独立的 Cloudflare Workers 适配，使用 LangGraph.js / LangChain.js、D1 和 R2，共用现有聊天界面。本地 Python 部署继续按下方方式运行。云端关闭页面后可能暂停，重新打开会从检查点继续；模型配置在站点中保存一次即可。

详细架构、Graph、配置和开发命令见 [云端部署说明](docs/CLOUD.md)。

## 从 v2.0 升级与排查等待

v2.0.7 修复首次创建虚拟环境中断后无法继续启动的问题。启动器检查 Python、pyvenv.cfg、activate 和 pip，补全缺失部分；先创建激活脚本，再以可见日志初始化 pip。中断后重新运行 `python3 start.py` 即可继续检查环境。只需要修复启动时，可从本包取出 `start.py` 替换旧版同名文件；正常使用无需手动 source activate。

v2.0.6 按本次要求把模型请求总时限、连接及读写等待统一为 **60 分钟（3600 秒）**。旧 `.env`、系统变量及已保存配置中的短超时自动按 3600 秒执行，连接信息和 Key 保持原有读取规则。每次任务调用增加“查看发送内容”，可核查系统提示词、完整任务上下文及参数，并下载当时的请求记录。

v2.0.5 在聊天中显示完整执行时间线：阶段、批次、耗时、模型输出、校验和重试。模型正文通过真实流式调用逐步返回；SSE 事件持久化并支持断线续接，完成后也能展开回看。暂停场景编辑同样有成功、失败和停止记录。现有 `.env`、历史聊天和检查点继续使用。

v2.0.4 支持 `.env` 一次配置模型连接，数据目录内的配置可随原有聊天一起保留；新增用例生成/导入阶段的字段校验反馈修正。v2.0.4 当时默认超时为 120 秒；v2.0.6 已统一改为 3600 秒。

v2.0.3 修复 AI 评审字段格式错误后的重复失败：补齐字段契约，记录具体字段路径/期望类型/实际类型，校验不通过时自动带反馈修正一次；修正失败保留原始有效用例和检查点。已保存的评审结果及版本原子提交，重启重试不会重复应用。

v2.0.2 修复自动路由把完整需求送给模型的问题：路由仅使用有预算上限的指令、近期对话及来源/产物概况；实际需求分析仍读取完整证据。此前版本的节点日志、等待进度和诊断下载继续保留。停止旧服务、备份整个数据目录后，可在新版目录运行 `python3 start.py --data-dir 原数据目录完整路径`，继续使用已有配置和聊天。详细命令、日志含义及多轮对话边界见 [诊断与升级说明](docs/TROUBLESHOOTING.md)。

## 启动

安装 Python 3.11 或更新版本，推荐 Python 3.12。解压进入项目目录。

macOS / Linux：

```bash
python3 start.py
```

Windows：

```powershell
py -3 start.py
```

启动器会在项目内创建 `.venv`、安装锁定依赖，然后打开 **http://127.0.0.1:8000**。首次安装依赖需要联网。前端文件已经包含在 `frontend/dist/`，日常启动不需要 Node.js。

保留终端运行。关闭浏览器不会停止任务；关闭 Python 服务后任务暂不推进，再次启动会从已保存的检查点恢复。按 Ctrl+C 可停止服务。

可选参数：

```bash
python3 start.py --port 8080
python3 start.py --data-dir /your/local/path/tcg-data
python3 start.py --no-browser
python3 start.py --check
python3 start.py --log-level debug
```

同一个数据目录只允许一个服务进程。应用面向个人本机使用，默认仅监听回环地址；不包含多人账号或公网部署的认证系统。

## 配置模型

### 用 .env 配置一次（推荐）

将包内 `.env.example` 复制为**原数据目录中的 `.env`**，填写自己的 Key。Mac 从新版项目目录执行一次（`cp -n` 不覆盖已经存在的配置）：

```bash
cp -n .env.example /Users/liuxinxin/Documents/GitHub/tcg-case-agent-local/data/.env
open -e /Users/liuxinxin/Documents/GitHub/tcg-case-agent-local/data/.env
```

文件内容如下。示例沿用本次诊断中的 LongCat 模型名；若服务端模型不同，请填写自己账号实际使用的 ID。Base URL 根据 [SiliconFlow 官方接口文档](https://docs.siliconflow.cn/api-reference/chat-completions/chat-completions)。

```dotenv
TCG_MODEL_PROVIDER=openai
TCG_MODEL_BASE_URL=https://api.siliconflow.cn/v1
TCG_MODEL_NAME=meituan-longcat/LongCat-2.0
TCG_API_KEY="填写你的 API Key"
TCG_MODEL_TIMEOUT_SECONDS=3600
```

之后每次在所用版本的项目目录启动，继续指定原数据目录：

```bash
python3 start.py --data-dir /Users/liuxinxin/Documents/GitHub/tcg-case-agent-local/data
```

也可把 `.env` 放在项目根目录，或用 `python3 start.py --env-file /固定路径/tcg.env --data-dir /原数据目录` 指定位置。`--env-file` 的相对路径以启动命令所在目录为准，传给后端时转为绝对路径。直接启动 Uvicorn 时可使用 `TCG_ENV_FILE`。

配置读取规则：系统环境变量优先于文件；文件选择顺序为显式 `--env-file` / `TCG_ENV_FILE`，否则选数据目录 `.env`，不存在才选项目根目录 `.env`。只读一个文件，不合并多个文件。已选文件中的字段覆盖 `settings.json`；未提供字段沿用已有配置或默认值。不存在的显式文件、格式错误、错误 provider/URL/超时会明确报错。文件按 UTF-8 读取，支持 `export NAME=value`、单/双引号和注释；值含空格或 `#` 时加引号，只支持单行，不执行命令或展开变量。只读取上面 5 个 `TCG_` 配置项。

检测到任意模型环境配置后，页面显示当前连接并提供“测试连接”；修改在文件或系统变量中完成，重启后生效。API Key 不返回到页面/日志，也不从 `.env` 复制到数据库。显式 `TCG_API_KEY=""` 表示清空；更高优先级配置改变服务地址或 provider 时，不沿用较低层的密钥，需在同一层明确配置相应 Key。

`.env` 是本机明文配置文件，已被 Git 和镜像构建排除，下载包只包含无密钥的 `.env.example`。继续使用原数据目录即可保留配置与聊天，不需要每次在页面重新填写。


### 本地 Ollama

1. 安装并启动 [Ollama](https://ollama.com/)，准备一个本机已安装、支持中文指令和 JSON 输出的模型。模型权重需要另行下载，本包不包含模型。
2. 在终端运行 `ollama list` 查看已安装模型的准确名称。若没有模型，先在 Ollama 中安装适合本机内存的模型。
3. 打开左下角“模型与设置”，选择“Ollama · 本地模型”。
4. 服务地址使用 `http://127.0.0.1:11434`，模型名称填写 `ollama list` 中的名称。
5. 点击“保存并测试连接”。

依赖和模型安装完成后，选择本机 Ollama 的工作流程可以完全在本地运行。模型速度和上下文容量取决于本机硬件和所选模型。

### 兼容模型 API

选择“OpenAI 兼容接口”，填写自己的 Base URL、模型 ID，以及服务需要的 API Key。可连接本地兼容服务，也可连接远程服务；远程服务会接收当前任务的上下文。

接口需要支持 Chat Completions 和 `response_format={"type":"json_object"}`。模型密钥在本机加密保存，设置接口不返回明文。更换服务地址后，原密钥不会自动用于新服务。普通本地无密钥服务可留空。

本项目不会自动创建付费账户、获取密钥或下载大型模型。未配置模型时会明确提示，不会用预置结果冒充生成结果。

## 使用流程

1. 选择项目并开始新对话。项目会自动生成 Default Profile。
2. 点击附件按钮上传 DOCX、文字型 PDF、XLSX、XLS、CSV、TXT 或 MD；也可在“需求来源”粘贴文本；在聊天中粘贴需求时，勾选“这条消息是需求正文”。普通生成指令不会被当作业务证据。
3. 选择来源角色、任务目标、Auto / HITP 和 Profile。默认自动识别任务目标。
4. 在聊天中查看逐步更新的执行时间线和 AI 输出，回答澄清问题、确认场景，获得最终用例。
5. 结果卡片提供表单编辑、JSON 全字段编辑、需求依据、历史版本和 Excel 下载。勾选条目后可以让 AI 仅修改所选内容。

六个显式任务为：分析需求、生成场景、生成用例、评审用例、证据问答、学习模板。未显式选择时，由路由节点结合本次消息和当前产物判断目标。

Auto 不在业务澄清处暂停，并只发布当前任务的目标结果。HITP 在需求需要澄清时暂停；完整用例生成还会在场景阶段等待确认。可以直接编辑或通过聊天修改待确认场景，然后继续生成。

生成用例后执行一次 AI 评审，使用 ADD / UPDATE / DELETE 定向修改。编辑后的产物产生不可变新版本；版本冲突会拒绝覆盖，恢复历史也会创建新版本。

## 技术结构

```text
backend/tcg/main.py       FastAPI 接口、应用生命周期、前端静态文件
backend/tcg/graph.py      LangGraph 节点、路由、interrupt/resume、后台执行
backend/tcg/model.py      LangChain 模型适配、任务契约、模型连接设置
backend/tcg/storage.py    SQLite、版本、审计、并发、单进程锁
backend/tcg/schemas.py    Pydantic 输入及证据/产物业务校验
backend/tcg/documents.py  本地文档解析和 XLSX 导出
backend/tcg/diagnostics.py  结构化日志、安全异常元数据及诊断下载
backend/tcg/environment.py  .env 读取、配置文件选择和环境变量映射
frontend/src/            React 聊天界面
frontend/dist/           可直接由 Python 提供的前端构建结果
tests/                   后端、恢复、模型传输、启动器检查
frontend/tests/          前端 DOM 交互回归测试
start.py                 跨平台 Python 启动器
requirements.txt         已验证 Python 依赖锁定版本
docs/API_CONTRACT.md     接口与响应结构
```

LangGraph 实际使用 `StateGraph`、`AsyncSqliteSaver`、`interrupt()` 和 `Command(resume=...)`。Graph State 主要保存 ID 和游标，正文与节点结果按引用从 SQLite 读取。任务由 Python 服务推进。LangChain 的 `astream()` 读取模型响应片段，后端将进度和正文片段写入 SQLite 事件表，再由 SSE 推送到聊天界面。浏览器按事件 ID 去重和补齐断线期间的记录；每 7 秒查询聊天状态作为补偿。

任务状态独立持久化，启动时恢复 queued / running 任务；waiting 任务继续等待人工操作。对同一聊天的新任务使用数据库唯一约束。每次产物写入重新检查任务状态和版本，以阻止取消后的迟到结果、重复提交和旧版本覆盖。

源文本按段落、表格行、PDF 页及 Excel 坐标解析为证据段。证据 ID 形如 `src_xxx#P1`。移除来源只使其对后续任务失效，历史引用仍可查阅。模型生成结构化 JSON，服务端验证字段和证据归属后才写入。

实现参考：[LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[人工暂停与恢复](https://docs.langchain.com/oss/python/langgraph/interrupts)、[LangChain ChatOllama](https://docs.langchain.com/oss/python/integrations/chat/ollama)。

## 实时执行过程

时间线包含阶段开始/完成、需求批次、请求耗时、校验失败字段、格式修正、重试、人工等待、停止和恢复。每次模型调用有独立输出卡片；自动重试和格式修正不会覆盖上次输出。已完成助手消息下方可展开“查看完整执行过程”。

实际模型输出是按任务契约生成的 JSON，生成途中可能不完整，卡片会标明“尚未校验”。最终用例仍需通过字段、证据及版本检查后才能保存和发布。时间线展示模型响应正文及执行事件，不读取独立的 reasoning/thinking 字段。系统提示词和任务输入可按需点开核查，认证信息不进入请求记录。

SSE 连接中断会自动续接；若连接彻底关闭，点击“重新连接”。刷新页面从持久化事件重放，关闭页面不取消后台生成。关闭 Python 服务则需要重启恢复：未完成的模型调用会重新执行，不支持从某个 token 续写。已经收到的片段保留，下一次调用显示在独立卡片中。旧版本没有记录的片段无法补回。

OpenAI 兼容服务须支持 Chat Completions 的流式 JSON 响应，Ollama 使用流式消息接口。连接测试仍是一次小型非流式 JSON 请求，测试通过不代表服务商支持流式输出；不兼容时请结合时间线及诊断检查提供方。v2.0.6 的单次请求时限统一为 60 分钟；有限重试规则不变，每次重试也最多等待 60 分钟。

参考：[LangChain 流式输出](https://docs.langchain.com/oss/python/langchain/streaming)、[消息片段聚合](https://docs.langchain.com/oss/python/langchain/messages)、[浏览器 SSE 与重连](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)。

## 核查发送给 AI 的内容

在“执行过程”中找到某次 AI 调用，点击 **查看发送内容**。弹窗提供：

- 系统提示词：本应用的完整系统提示和当前任务契约。
- 任务上下文：当次实际传入的指令、历史消息、需求证据、Profile、已有结果；自动修正时还包括被拒绝的回答和校验反馈。具体内容以该调用真实输入为准。
- 请求记录：上述消息、模型、服务地址、超时及显式生成参数，并可下载 JSON 留存核查。

请求在发出前保存成不可变快照，按 run_id/call_id 关联；重试、澄清、暂停编辑和格式修正分别记录。弹窗打开时才读取正文，不把整份输入放进 SSE 或普通诊断日志。任务失败、页面刷新及服务重启后仍能查看。记录代表传给 LangChain 的消息和本应用显式参数，不是底层 HTTP 抓包，也不证明提供方已经收到。连接测试没有聊天任务 ID，因此不记录在聊天时间线中。旧版本未记录的输入不凭当前配置重新拼造，会显示“尚无发送记录”。

本次 60 分钟策略包括应用总时限和模型 HTTP 客户端的连接、读、写、连接池等待；连接测试、普通生成、重试和暂停编辑均采用同一实际值。前端 fetch 没有另设较短的请求截止时间，SSE 维持原来的长连接和重连方式。旧 timeout_seconds / TCG_MODEL_TIMEOUT_SECONDS 的有效短值保持兼容但归一为 3600，设置页显示为只读；原文件不被改写，新保存的页面设置写入 3600。无效环境配置仍会明确报错。

这是本应用的等待策略，无法延长模型供应商、代理或网络设备自己的连接期限。单次调用满 60 分钟仍会终止，即使它仍在流式输出；整个任务包含多次模型调用，时长可以超过 60 分钟。任务运行中仍可以点击“停止”。

## 本地数据与备份

默认数据位置为项目下的 `data/`，可用 `--data-dir` 更改：

- `tcg.sqlite3`：项目、会话、来源、用例、版本、任务、审计及 SSE 执行记录（包括模型输出正文）、每次调用的输入快照。
- `checkpoints.sqlite3`：LangGraph 执行检查点。
- `uploads/`：上传原件。
- `logs/tcg.log`：本地业务日志，默认 5 MiB 轮转并保留 3 个备份。
- `settings.json`：模型连接设置和加密后的密钥。
- 本地加密密钥文件：启动配置保存 API Key 时生成。

执行事件及输入快照目前保留完整历史，没有自动清理策略，长任务会增加数据库体积。模型正文只保存在本地执行事件中，不写入诊断日志或诊断下载；已有业务产物及模型缓存仍按原机制保存。

**备份时先停止服务，再复制整个数据目录**，包括加密密钥和 SQLite 辅助文件。只复制单个数据库不足以完整恢复。加密保护不能阻止已经拥有该本地目录读取权限的人访问数据，因此仍应使用正常的本机账户和磁盘保护。

这份源码包不含业务数据、用户文件、API Key、模型权重或 Python / Node 依赖目录。旧 Cloudflare 版本的数据不会自动迁入，可手动导出文件后作为输入上传到本地版。

## 开发与测试

Python：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
python start.py --no-install --no-browser
```

Windows 激活命令为 `.venv\Scripts\Activate.ps1`；也可以直接使用 `.venv\Scripts\python.exe`。

修改前端时需要 Node.js（本次使用 Node.js 24）：

```bash
cd frontend
npm ci
npm test
npm run build
```

开发热更新可运行 `npm run dev`，通过 Vite 代理访问已启动的 Python API。前端和 API 各自使用同一个回环主机名。构建后生成 `frontend/dist`，重启或刷新 Python 提供的页面即可使用新版界面。

验证记录见 `docs/VALIDATION.md`。测试用模型只通过测试依赖注入提供，不在应用设置中提供“假模型”选项。

## 当前边界

这是按聊天交互和本地 Python 架构重写的核心版本，未宣称原 PRD 企业验收全部完成。

- 不内置 OCR；扫描页、加密 PDF 或无法提取文本的文件明确报错。单文件上限 15 MB，提取文本上限 200 万字符。
- 没有企业 OKTA、外部 Repository / KB 连接器、向量数据库。知识文件可以作为本地来源使用；Profile 级跨会话 KB 管理尚未实现。
- 长文档分析会分批遍历，场景和用例支持显式分页；单个模型上下文超过 50 万字符会明确失败，不静默截断。尚未实现完整 Token 预算规划、跨批语义去重与确定性的覆盖率门禁。
- 复杂需求的关系图由模型按契约生成并保存在报告中；当前界面展示结构化报告 / 图源，没有完整的可视化图形编辑器。
- 校验基础用例与场景字段，并保留扩展字段；没有完整的任意自定义 Schema 校验。Excel 支持全部 / 选中、一用例一行 / 一步骤一行、Sheet 名；列为固定用例字段，暂不支持自定义列映射和文件名规则。
- 模板学习支持确认后更新当前 Profile 或创建新 Profile；没有单次运行的临时 Profile 覆盖确认流程。
- 无自动测试执行或脚本录制。证据引用校验不等同于证明模型结论正确，最终结果仍需业务复核。
- 已在 Linux + Python 3.12 / Node.js 24 验证；macOS、Windows 和 Docker 的完整运行尚未分别实测。启动器按跨平台方式实现。

## 可选 Docker

可直接 Python 启动，也附带单服务 Docker 配置：

```bash
docker compose up --build
```

访问 http://127.0.0.1:8000。SQLite 和上传文件保存于 `tcg-data` 卷。容器只向宿主机回环地址映射端口。

容器中的 `127.0.0.1` 是容器自身。若模型在宿主机上运行，需要使用模型服务可达的宿主地址，例如 `http://host.docker.internal:11434`，并按自己的网络环境配置模型监听地址。此 Docker 路径尚未实测。


Docker Compose 使用项目根目录 `.env` 作为可选 `env_file`，需 Compose 2.24.0+，参见 [Docker 官方说明](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/)。这是 Compose 自己的环境文件解析规则；含 `$` 的字面值请按 Compose 规则使用单引号。宿主机数据目录 `.env` 不会自动映射进默认命名卷。Python 启动方式无需 Docker。本次未运行 Docker 验证。
