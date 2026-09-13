# Task 1 — 指定资料、局部采用与精确关联

已实现并通过定向验收：

- `project.update_from_sources` 保留指定的需求/场景/用例目标、`selected_ids` 和明确 `source_ids`。场景/用例直接复用 `artifact_modify`；预览不采用资料，直接保存复用原子 apply，均不批准当前阶段。联动仍使用已有 sync 实现。
- `workspace.reconcile` 接受所选条目、资料与 `preview:false`。未指定的新附件不自动被采用，也不替代当前确认操作。资料已经被当前分支某一成果采用后，不再称为“未采用”。
- 修订报告保存实际资料版本、采用范围、精确 refs 及上游待同步详情。已明确关联的父项采用相同依据后，可记录/显示差异已解决。不会为导入的无关联行虚构父项。
- 继续守卫仅检查将使用的父项，保留真实过期父项保护，返回受影响行/上游行及版本。manifest、资料变更、晚到结果、原子提交与人工字段保护保持有效。
- 同一场景的多个用例只同步其中一条时，分别保存其父项版本；未选用例保持原内容和旧父项。依赖关系、工作区、后续模型上下文均读取相应版本。
- 工作区支持 `?revision=N`，新增 `parents`、`lineage_rows`（逐行父项原文、依据、版本、stale）、`source_adoptions`、`upstream_discrepancies`、`revision_diff`、`review`。评审后改动由实际评审基线计算，忽略人工执行字段。
- 资料采用的同一有预算模型调用会检查提供的已确认项目事实；经校验的 `source_conflicts` 返回窄问题且不创建修改预览。该语义检查依赖模型识别，程序验证冲突必须指向提供的事实、所选条目和补充依据。

验证先记录了四项缺失行为的失败，再实现；另先记录并修复混合父级版本、评审差异、事实冲突及后续上下文读取的失败。连续领域链路验证了：新增资料 → 仅 C1 修改 → 仍等待 → 对应需求与场景同步 → 仅受影响用例同步 → 可继续；无关行与人工结果保持原样。独立的连续 HTTP 验收由总任务的旅程测试负责。

最后实际运行：

```bash
PYTHONPATH=backend /workspace/scratch/34c288a69784/tcg-test-env/bin/python -m pytest -q tests/test_targeted_sources_v290.py tests/test_workspace_actions_v2512.py tests/test_framework_impact_v270.py tests/test_workspace_coverage_v2512.py tests/test_framework_context_v270.py tests/test_dependency_diagnostics_v271.py tests/test_workspace_changes_v280.py -k 'not test_source_preview_returns_shared_diff_and_adopts_only_on_apply and not test_unadopted_business_source_blocks_result_gate_but_not_clarification and not test_source_reconciliation_from_case_focus_uses_current_analysis_revision and not test_row_scoped_reconciliation_is_rejected_before_model and not test_explicit_run_source_exclusion_survives_generation_but_later_upload_is_pending'
```

结果：`85 passed, 6 deselected`。排除的六个旧用例要求自动采用未指定附件、用例目标改回需求理解、新附件阻止继续或拒绝所选范围，与已批准的 v2.9.0 行为相反；对应新语义由 `test_targeted_sources_v290.py` 覆盖。未运行全部历史套件。

`git diff --check` 对本任务修改文件通过。总任务另外负责来源 supersession 生命周期的 manifest 兼容，不由本子任务重复修改。
