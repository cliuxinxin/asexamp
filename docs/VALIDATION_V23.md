# v2.3 主流程验证

验证日期：2026-09-09。按用户要求，仅验证主流程；未执行全量回归。

通过：
- API 来源上传 → 普通需求一次 direct_cases 调用 → 保存用例 → 局部修改 → XLSX 导出。
- 关键歧义暂停 → 用户回答 → 继续完成。
- 单条步骤格式错误 → 仅修复该行 → 保存。
- 超容量文档才分组；所有原始证据均被传入生成请求。
- React 组件交互：生成默认意图与需求保存、结果自动进入中间表格、后续对话定位已有结果、导出对话框打开。
- TypeScript 类型检查和 Vite 生产构建。

命令：
```sh
PYTHONPATH=backend python3 -m pytest tests/test_direct_smoke.py -q
cd frontend
node --import tsx --test tests/workspace-smoke.test.tsx
npm run build
```

API 测试使用注入的模拟模型，组件测试使用模拟 API；生产启动不包含模拟模型兜底。尚未连接用户的内网模型，不能推断真实模型速度和业务语义质量。

浏览器视觉检查未完成：云浏览器对本地地址返回 ERR_BLOCKED_BY_CLIENT；本地 Playwright 缺少浏览器文件，下载超时后停止。未声称像素级还原或移动端实际截图通过。构建有 Mermaid 大块提示，无构建错误。

未验证：Docker、OCR、所有旧版测试、各模型兼容性与大规模跨章节业务组合。
