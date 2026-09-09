# v2.5.5 用例描述为空的修复

## 原因

模板列映射只能决定导出时读取哪个字段，不会自动产生字段内容。v2.5.4 的用例生成示例没有 description，核心用例校验也不要求它；即使将模板传给模型，模型遗漏该字段仍然可以通过。Excel 导出又使用 item.get(field, '')，因此直接写空。历史模板使用 case_description 等名称时，与实际用例中的 description 也可能不一致。

用户未提供本次实际 Profile 和生成 JSON，不能确认其具体列名；本版修复可复现的漏生成路径，并兼容常见描述字段名。

## 修复

- 模板配置 description 或常见等价字段时，生成指令明确要求非空 description，提供完整示例；使用模板 definition 解释该字段的业务含义。
- 对结构与引用已通过基础校验、但缺少描述的用例，调用 complete_case_descriptions，只补描述。请求包含缺失用例、对应需求依据、语言和字段定义；返回只能按原 ID 合并 description，不替换步骤、预期或其他字段。正常已有描述时没有补全调用。
- 评审新增或清空描述时执行同样补全。已有描述继续保留，仍应按业务检查其准确性。
- description、case_description、test_case_description、case_desc、test_case_desc、用例描述、测试用例描述按同一字段含义处理，忽略英文大小写、空格和连字符；Excel 表头保持模板设置。
- 原结果可在导出窗口点“补全用例描述”。这是一个独立后台任务，只更新缺失描述并保存版本，不再做需求分析、场景生成、整套用例生成或额外 AI 总结。
- 下载 GET 不调用模型，也不通过复制标题来填充空白。若仍缺描述，导出前给出补全提示，避免再次静默输出空列。

## 已有结果怎么处理

保留原 data 目录和模型设置后启动新版，打开原用例 → 导出 Excel → 选择含“用例描述”的 Profile → 补全用例描述。任务完成后重新导出。也可在编辑器直接填写 description。补全时保留原有非空描述，并检查版本冲突；已有进行中任务先完成或停止。

如果字段名使用了上述列表之外的自定义名称，将该列 Case 字段设为 description，Excel 列标题仍可保留“用例描述”。此版本没有把所有任意自定义列都自动变为必填。

## 验证

- `python3 -m pytest -q tests/test_description_v255.py tests/test_feedback_v254.py tests/test_incident_http_v251.py --tb=short --show-capture=no`：8 项通过。包含描述补全→评审→实际 XLSX 单元格验证、旧用例仅补缺失描述、常见别名兼容，以及自动与人工主流程。
- `node --import tsx --test tests/workspace-smoke.test.tsx`：5 项通过。包含描述缺失时导出入口提示、补全按钮请求及现有聊天/人工确认交互。
- `npm run build`：通过，前端构建产物已包含在 ZIP。

使用本地模拟模型接口验证逻辑和文件输出，没有访问用户内网模型，也没有进行真实浏览器视觉验收。未运行全量历史回归。
