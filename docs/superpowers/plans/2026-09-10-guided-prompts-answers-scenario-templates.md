# TCG v2.5.8 Guided Prompts, Answers, and Scenario Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add editable task prompts, evidence-grounded clarification suggestions, and independently configurable scenario-template learning and XLSX export without changing existing case export behavior.

**Architecture:** Keep task prompt defaults in a small frontend module, carry optional clarification suggestions through the v7 LangGraph interrupt, and extend Profile with a separate scenario export schema. Reuse one safe workbook writer for scenarios and cases while keeping case-only missing-field completion isolated.

**Tech Stack:** React 19, TypeScript, FastAPI, Python 3.11+, LangGraph, Pydantic, openpyxl, Node test runner, pytest.

**Spec:** `docs/superpowers/specs/2026-09-10-guided-prompts-answers-scenario-templates-design.md`

## Global Constraints

- Never overwrite text the user edited in the composer.
- Suggestions are optional and are never auto-submitted.
- Only exact, non-example evidence IDs may ground a suggested answer.
- Scenario and case Profile fields are independent and backward compatible.
- Scenario export is deterministic and does not call the model.
- Existing case template validation, completion, and export remain unchanged.
- Run only targeted tests plus one complete mocked main-flow test before packaging.

---

### Task 1: Editable task prompt defaults

**Files:**
- Create: `frontend/src/taskPrompts.ts`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/tests/guided-v258.test.tsx`

**Interfaces:**
- Produces: `TASK_PROMPTS: Record<string,string>` and `nextTaskDraft(current, previousIntent, nextIntent): string`.
- Consumes: existing `intent`, `content`, and `setContent` composer state.

- [x] **Step 1: Write failing component and unit coverage**

```ts
assert.equal(nextTaskDraft('', 'auto', 'learn_template').includes('自动判断是场景模板'), true);
assert.equal(nextTaskDraft('我的自定义要求', 'auto', 'learn_template'), '我的自定义要求');
fireEvent.click(screen.getByRole('button', {name:'学习模板'}));
assert.match((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value, /列顺序/);
```

- [x] **Step 2: Run the narrow test and confirm it fails**

Run: `npm test -- --test-name-pattern='task prompt' frontend/tests/guided-v258.test.tsx`

Expected: FAIL because `taskPrompts.ts` and the fill behavior do not exist.

- [x] **Step 3: Add prompt mapping and draft-preservation logic**

```ts
export const TASK_PROMPTS = {
  review_requirement: '请分析当前需求，提取业务规则、范围、歧义并绘制业务图。',
  generate_scenario: '请基于当前需求理解生成测试场景，并保留需求追溯关系。',
  generate_case: '请基于当前测试场景生成可追溯的测试用例。',
  review_case: '请评审当前测试用例，只做有依据的局部优化。',
  query: '请根据当前需求资料回答：',
  learn_template: '请学习已上传的 Excel 模板，自动判断是场景模板、用例模板或同时包含两者，提取工作表、列顺序、字段定义、填写方式和文件名规则。',
} as const;
```

On task selection, replace the composer only when it is blank or exactly equals the previous task's default; keep every other draft byte-for-byte.

- [x] **Step 4: Run the narrow test and confirm it passes**

Run: `npm test -- frontend/tests/guided-v258.test.tsx`

Expected: task prompt tests PASS.

### Task 2: Evidence-grounded clarification suggestions

**Files:**
- Modify: `backend/tcg/flow.py`
- Modify: `backend/tcg/workflow.py`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/RunCard.tsx`
- Modify: `frontend/src/styles.css`
- Test: `tests/test_guided_v258.py`
- Test: `frontend/tests/guided-v258.test.tsx`

**Interfaces:**
- Produces backend helper `valid_question_suggestions(report, evidence) -> list[dict]`.
- Produces interrupt property `question_suggestions?: QuestionSuggestion[]` where `QuestionSuggestion={question,answer,basis,refs,confidence}`.
- Consumes `report.questions` and the exact evidence list used by analysis.

- [x] **Step 1: Write failing backend tests**

```python
def test_suggestions_keep_only_current_questions_and_non_example_refs():
    report = {'questions':['是否锁定？'], 'question_suggestions':[
        {'question':'是否锁定？','answer':'是','basis':'需求明确','refs':['src#P1'],'confidence':'supported'},
        {'question':'其他？','answer':'否','basis':'猜测','refs':['example#P1'],'confidence':'supported'}]}
    assert valid_question_suggestions(report, evidence) == [report['question_suggestions'][0]]
```

Also assert the v7 clarification interrupt contains the filtered list and still works when only the legacy `questions: string[]` exists.

- [x] **Step 2: Run backend tests and confirm they fail**

Run: `PYTHONPATH=backend /tmp/tcg-v257-venv/bin/pytest -q tests/test_guided_v258.py -k suggestion`

Expected: FAIL because suggestions are not validated or forwarded.

- [x] **Step 3: Extend the analysis output contract and validate suggestions**

The contract asks for optional `report.question_suggestions`. The helper retains an item only when its question is in `report.questions`, answer and basis are non-empty strings, confidence is `supported` or `assumption`, and every ref exists in the current non-example evidence map. Invalid suggestions are dropped without failing the analysis.

Pass the filtered suggestions in `node_clarification_gate()`:

```python
interrupt({
    'type':'clarification',
    'artifact_id':artifact['id'],
    'questions':questions,
    'question_suggestions':valid_question_suggestions(artifact['report'], evidence),
})
```

- [x] **Step 4: Write failing frontend tests for adopting answers**

Render `RunCard` with one supported suggestion and assert “采用此答案” fills a draft that includes the question and answer. Assert “采用全部建议” combines multiple answers and does not call `/resume` until the user clicks the existing continue button.

- [x] **Step 5: Implement suggestion cards and draft actions**

Add the typed interrupt field, show answer/basis/confidence under the matching question, and preserve the existing free-text textarea. Buttons only call `setAnswer`; submission continues through the existing `/resume` action.

- [x] **Step 6: Run backend and frontend narrow tests**

Run: `PYTHONPATH=backend /tmp/tcg-v257-venv/bin/pytest -q tests/test_guided_v258.py -k suggestion`

Run: `npm test -- frontend/tests/guided-v258.test.tsx`

Expected: both PASS.

### Task 3: Separate scenario Profile schema and template learning

**Files:**
- Modify: `backend/tcg/schemas.py`
- Modify: `backend/tcg/model.py`
- Modify: `backend/tcg/workflow.py`
- Modify: `frontend/src/ProfileEditor.tsx`
- Test: `tests/test_guided_v258.py`
- Test: `frontend/tests/guided-v258.test.tsx`

**Interfaces:**
- Produces Profile keys `scenario_excel_columns`, `scenario_sheet_name`, `scenario_filename_pattern`.
- Produces model response `{template_kinds: ('scenarios'|'cases')[], config: object, summary: string}`; `template_kinds` is report metadata and is not persisted in Profile.
- Consumes existing `profile_config()` and `WorkflowEngine.template_config()`.

- [x] **Step 1: Write failing Profile and merge tests**

```python
profile = profile_config({'scenario_excel_columns':[{'field':'title','header':'场景名称'}]})
assert profile['scenario_sheet_name'] == 'Test Scenarios'
assert profile['excel_columns'] == DEFAULT_PROFILE['excel_columns']

merged, _ = engine.template_config(current, {
    'scenario_excel_columns':[{'field':'description','header':'场景描述'}],
    'excel_columns':[],
})
assert merged['excel_columns'] == current['excel_columns']
```

Assert scenario columns allow `requirement_ids` and `refs`, while case `excel_columns` still rejects both.

- [x] **Step 2: Run Profile tests and confirm they fail**

Run: `PYTHONPATH=backend /tmp/tcg-v257-venv/bin/pytest -q tests/test_guided_v258.py -k 'profile or template'`

Expected: FAIL because the scenario schema is absent.

- [x] **Step 3: Add independent scenario defaults and validation**

```python
'scenario_sheet_name': 'Test Scenarios',
'scenario_filename_pattern': '{project}_scenarios_{date}.xlsx',
'scenario_excel_columns': [
  {'field':'id','header':'Scenario ID'},
  {'field':'title','header':'Title'},
  {'field':'description','header':'Description'},
  {'field':'priority','header':'Priority'},
  {'field':'requirement_ids','header':'Requirement IDs'},
  {'field':'refs','header':'Evidence Refs'},
],
```

Validate the scenario fields separately. Only `_`-prefixed and server-internal fields are forbidden in the scenario schema; `requirement_ids` and `refs` are explicitly allowed traceability fields.

- [x] **Step 4: Update template-learning contract and merge behavior**

Tell the model to identify scenarios, cases, or both from sheet names, headers, definitions, and example rows. Validate `template_kinds` against the two allowed values. Normalize only configuration keys for detected kinds; an absent or empty detected value preserves the matching current configuration. Store `template_kinds` in the proposal report beside `config`, never inside `config`.

- [x] **Step 5: Add scenario settings UI**

Extend `ProfileEditor` with a separate “场景 Excel 模板” section for sheet name, filename pattern, and ordered columns. Keep the current “用例 Excel 模板” section behavior and accessibility labels intact.

- [x] **Step 6: Run Profile, workflow, and editor tests**

Run: `PYTHONPATH=backend /tmp/tcg-v257-venv/bin/pytest -q tests/test_guided_v258.py tests/test_template_fields_v256.py`

Run: `npm test -- frontend/tests/guided-v258.test.tsx frontend/tests/workspace-smoke.test.tsx`

Expected: PASS, including old case-template tests.

### Task 4: Scenario XLSX export

**Files:**
- Modify: `backend/tcg/documents.py`
- Modify: `backend/tcg/main.py`
- Modify: `frontend/src/ArtifactCard.tsx`
- Modify: `frontend/src/api.ts`
- Test: `tests/test_guided_v258.py`
- Test: `frontend/tests/guided-v258.test.tsx`

**Interfaces:**
- Produces `export_artifact(artifact, layout='case', selected=None) -> bytes`, dispatching by `artifact['type']`.
- Extends `GET /api/artifacts/{id}/export-options` and existing export endpoint to accept scenarios.
- Consumes scenario Profile fields from Task 3.

- [x] **Step 1: Write failing export tests**

Create a scenario artifact with list-valued traceability fields. Assert the workbook uses `Test Scenarios`, joins list values with newlines, neutralizes formula-looking content, supports selected IDs, and honors a custom scenario column order. Assert case export output still uses existing case fields and field completion checks.

- [x] **Step 2: Run export tests and confirm they fail**

Run: `PYTHONPATH=backend /tmp/tcg-v257-venv/bin/pytest -q tests/test_guided_v258.py -k export`

Expected: FAIL with “仅 Case Artifact 支持 Excel 导出”.

- [x] **Step 3: Add deterministic scenario workbook export**

Refactor common workbook styling and selected-ID validation into private helpers. For scenarios, read only `scenario_*` settings, serialize dict/list values with readable newlines or JSON, and never invoke `template_check()` or field-completion endpoints. Retain `export_cases()` as a compatibility wrapper.

- [x] **Step 4: Make API options artifact-aware**

For cases return the existing snapshot checks. For scenarios return snapshot/profile configuration without case-only missing-field checks. Pick the correct filename pattern by artifact type and use the existing safe filename normalization.

- [x] **Step 5: Enable scenario export in the result card**

Show “导出 Excel” for `scenarios` and `cases`. Use generic copy in the dialog; hide the case layout and missing-field completion controls for scenarios. Continue using selected row IDs and the existing download helper.

- [x] **Step 6: Run export/API/UI tests**

Run: `PYTHONPATH=backend /tmp/tcg-v257-venv/bin/pytest -q tests/test_guided_v258.py tests/test_description_v255.py`

Run: `npm test -- frontend/tests/guided-v258.test.tsx frontend/tests/workspace-smoke.test.tsx`

Expected: PASS.

### Task 5: Version, focused main flow, and package

**Files:**
- Modify: `backend/tcg/main.py`
- Modify: `README.md`
- Modify: `RELEASE-MANIFEST.json`
- Test: `tests/test_guided_v258.py`

**Interfaces:**
- Produces release directory and ZIP named `tcg-case-agent-local-v2.5.8`.
- Consumes all prior task interfaces.

- [x] **Step 1: Add one complete mocked v7 main-flow test**

The test creates a project/chat, uploads requirement evidence and an Excel example, starts HITP generation, receives a clarification suggestion, submits the adopted answer, confirms understanding, confirms scenarios, completes reviewed cases, exports scenarios, learns the template, and verifies the final run is completed.

- [x] **Step 2: Run focused backend and frontend verification**

Run: `PYTHONPATH=backend /tmp/tcg-v257-venv/bin/pytest -q tests/test_guided_v258.py tests/test_template_fields_v256.py tests/test_description_v255.py tests/test_edit_race_v257.py`

Run: `npm test -- frontend/tests/guided-v258.test.tsx frontend/tests/workspace-smoke.test.tsx`

Run: `npm run build`

Expected: targeted suites PASS and the production frontend build succeeds.

- [x] **Step 3: Update release metadata**

Change the FastAPI version to `2.5.8`, add the three user-facing features to README, and regenerate the release manifest checksums after the build.

- [x] **Step 4: Create and inspect the ZIP**

Run: `zip -qr ../tcg-case-agent-local-v2.5.8.zip . -x 'frontend/node_modules/*' -x '.venv/*' -x 'data/*' -x '*.pyc' -x '__pycache__/*'`

Inspect with: `unzip -t ../tcg-case-agent-local-v2.5.8.zip`

Expected: archive integrity check reports no errors and contains `frontend/dist`, backend source, tests, README, and release manifest.


## v2.5.8 execution record

- Continued the already approved isolated v2.5.8 directory; v2.5.7 source/archive retained.
- Task prompt, clarification, scenario template and export implementation completed with focused tests.
- Clarification backend review: no correctness findings; malformed optional suggestions safely discarded.
- Integration review: fixed unpublished template-kind metadata and destination-Profile rebasing for kind-specific proposals. A scenario proposal must preserve the chosen destination's case configuration, including when it was learned using another Profile.
- Scenario field editor follows the approved field/header/definition contract; case-only filling policies are not exposed for scenarios.
- Browser navigation was blocked by the environment; component DOM interaction verification and production build used for this iteration.
- Release verification and package contents recorded in docs/VALIDATION-v2.5.8.md and RELEASE-MANIFEST.json.
