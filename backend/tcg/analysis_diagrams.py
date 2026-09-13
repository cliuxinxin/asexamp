"""Three complementary requirement views, with truthful local omission handling."""
import copy
import re


ANALYSIS_DIAGRAM_INSTRUCTION = (
    'Include exactly three complementary current requirement diagrams in report.diagrams: '
    '业务流程图 using flowchart TD, 领域思维导图 using mindmap, and 状态转换图 using stateDiagram-v2. '
    'Show business actions/branches, domain concepts/rules, and entity states/transitions respectively. '
    'Use the current requirement facts and evidence only, never a test-generation workflow. '
    'Do not infer execution order from row order or invent states, transitions, actors or conditions. '
    'If transition or ordering facts are unavailable, explicitly mark the missing information in that view. '
    'Each diagram has a title and one plain Mermaid string; no fences, HTML, directives or style commands. '
    'Keep each view concise, use safe node IDs and quote text labels. '
    'When understanding changes, refresh all three views; do not copy outdated diagram text. '
)

_VIEWS = (('flowchart', '业务流程图'), ('mindmap', '领域思维导图'), ('stateDiagram-v2', '状态转换图'))


def _kind(source):
    if not isinstance(source, str) or len(source) > 30000:
        return None
    source = source.strip()
    match = re.match(r'^(flowchart|graph|mindmap|stateDiagram-v2)\b', source)
    if not match or re.search(r'%%\s*\{|^\s*(click|style|classDef|linkStyle)\b|<\/?[a-z][^>]*>|javascript\s*:|@\{', source, re.I | re.M):
        return None
    return 'flowchart' if match[1] == 'graph' else match[1]


def _label(value, limit=110):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    text = text[:limit] + ('…' if len(text) > limit else '')
    # Mermaid interprets quotes, markup and delimiters inside several label forms.
    return text.translate(str.maketrans({'"': '＂', '<': '＜', '>': '＞', '[': '［', ']': '］',
        '{': '｛', '}': '｝', '(': '（', ')': '）', '`': '｀', '\\': '＼', '&': '＆', '#': '＃'}))


def _fallbacks(rows):
    visible = rows[:16]
    remaining = max(0, len(rows) - len(visible))
    # A requirement list establishes membership, not control flow or state edges.
    flow = ['flowchart TD', '  subgraph requirements["本次输出未提供流程连线；当前需求索引"]']
    mindmap = ['mindmap', '  root((需求领域))']
    state = ['stateDiagram-v2', '  state "状态转换图待补全" as Unspecified',
        '  note right of Unspecified',
        '    本次输出未提供可核实的状态转换图，需根据当前需求补全。',
        f'    已核对 {len(rows)} 条当前需求；条目顺序不代表状态顺序。',
        '    图中暂不推断业务对象、状态、触发事件及转换条件。']
    for index, row in enumerate(visible):
        label = _label(str(row.get('id', '')) + ' ' + str(row.get('title') or ''))
        flow.append(f'    R{index}["{label}"]')
        mindmap.append(f'    R{index}["{label}"]')
        description = _label(row.get('description'), 150)
        if description:
            mindmap.append(f'      D{index}["{description}"]')
    if remaining:
        note = f'另有 {remaining} 条需求，详见需求列表'
        flow.append(f'    More["{note}"]')
        mindmap.append(f'    More["{note}"]')
    flow.append('  end')
    state.append('  end note')
    return dict(zip((view[0] for view in _VIEWS), ('\n'.join(flow), '\n'.join(mindmap), '\n'.join(state))))


def complete_analysis_diagrams(report, rows, *, previous=None, whole_response=True):
    """Retain current complete views; never mistake partition or old views for current ones.

    Missing diagrams must not reject valid requirements or create another model
    retry loop. Local views explicitly mark any relationships unavailable in the
    typed requirements. Models normally provide the business semantics in the
    original understand/revise call through ANALYSIS_DIAGRAM_INSTRUCTION.
    """
    supplied = report.get('diagrams', [])
    supplied = supplied if isinstance(supplied, list) else []
    previous = previous or {}
    previous_report = previous.get('report', {})
    changed = ((previous.get('items') is not None and previous['items'] != rows)
               or bool(previous) and any((previous_report.get(key) or []) != (report.get(key) or [])
                   for key in ('in_scope', 'out_of_scope', 'assumptions')))
    stale_sources = {d.get('mermaid') for d in previous.get('report', {}).get('diagrams', [])
                     if isinstance(d, dict) and isinstance(d.get('mermaid'), str)} if changed else set()
    by_kind = {}
    if whole_response:
        for diagram in supplied:
            if not isinstance(diagram, dict):
                continue
            source = diagram.get('mermaid')
            kind = _kind(source)
            if kind and source not in stale_sources and kind not in by_kind:
                by_kind[kind] = copy.deepcopy(diagram)
    fallbacks = _fallbacks(rows)
    report['diagrams'] = [by_kind.get(kind) or {'title': title, 'mermaid': fallbacks[kind]}
                          for kind, title in _VIEWS]
    return report
