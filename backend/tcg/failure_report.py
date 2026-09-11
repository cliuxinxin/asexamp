"""Small, readable failure reports. Full request/response archives remain separate."""
import hashlib
import json
import re
from .json_output import parse_model_object, parse_issue
from .schemas import DomainError

MAX_REPORT_BYTES=64*1024


def clip(text,limit):
    raw=str(text).encode('utf-8')
    return str(text) if len(raw)<=limit else raw[:limit].decode('utf-8','ignore')+'\n[超出本节长度，已省略]'


def code(text):
    fence='~'*max(3,1+max((len(x) for x in re.findall(r'~+',str(text))),default=0))
    return fence+'text\n'+str(text)+'\n'+fence


def text_content(message):
    content=message.get('content','')
    if isinstance(content,str):return content
    return ''.join(v.get('text','') for v in content if isinstance(v,dict) and isinstance(v.get('text'),str)) if isinstance(content,list) else ''


def response_excerpt(raw,issue):
    if len(raw.encode('utf-8'))<=6500:return code(raw)
    parts=['原始返回较长，下列仅为日志节选；是否返回完整需结合语法错误和结束原因判断。','返回开头：',code(clip(raw,1200))]
    position=issue.get('position')
    if isinstance(position,int):
        start=max(0,position-650);end=min(len(raw),position+650)
        first_line=raw.count('\n',0,start)+1
        lines=raw[start:end].split('\n')
        numbered='\n'.join(f'{first_line+i:>6} | {line}' for i,line in enumerate(lines))
        parts+=['错误附近（行号对应原始返回）：',code(clip(numbered,4400))]
    parts+=['返回结尾：',code(clip(raw[-450:],1400))]
    return '\n\n'.join(parts)


def belongs_to_call(event, call, calls):
    if event.get('call_id'):
        return event['call_id'] == call['call_id']
    if event.get('node') != call.get('node'):
        return False
    later = next((c.get('at') for c in calls if c.get('node') == call.get('node')
                  and c.get('at', '') > call.get('at', '')), None)
    if not (call.get('at', '') <= event.get('at', '') and (not later or event.get('at', '') < later)):
        return False
    return not event.get('call_key') or event['call_key'] == call.get('call_key')


def failed_step_report(store,diagnostics,run_id,call_id=None):
    run=store.run(run_id)
    with store.lock:
        records=store.db.execute('SELECT call_id,payload FROM model_requests WHERE run_id=? ORDER BY rowid',(run_id,)).fetchall()
    calls=[json.loads(row['payload']) for row in records]
    events=diagnostics.rows(run_id,500)
    if call_id:
        chosen=[c for c in calls if c['call_id']==call_id]
        if not chosen:raise DomainError('当前任务没有这次调用的发送记录',404)
        node=chosen[0].get('node')
    else:
        node=run.get('failed_node') or next((e.get('node') for e in reversed(events) if e.get('event')=='node.error'),None)
        if not node and calls:node=calls[-1].get('node')
        chosen=[c for c in calls if c.get('node')==node]
    omitted=max(0,len(chosen)-6);chosen=chosen[-6:]
    parts=['# TCG 失败步骤日志',f'任务：{run_id}',f'当前状态：{run["status"]}；步骤：{node or run.get("failed_stage") or run["stage"]}',
        '任务级错误：'+clip(run.get('error') or '当前没有终止错误；所选调用是否通过，请看下方调用结论与校验记录。',1800),
        f'本文件只包含选中步骤最近 {len(chosen)} 次调用；不含完整需求、历史产物或检查点。文件上限 64 KiB。']
    if omitted:parts.append(f'该步骤更早的 {omitted} 次调用未收入本文件。')
    dependency_events = [{k: event[k] for k in ('event', 'node', 'task', 'call_id', 'call_key',
        'dependency_changes', 'dependency_phase', 'dependency_task', 'dependency_kind') if k in event}
        for event in events if event.get('dependency_changes') and
        event.get('node') == node and (not call_id or belongs_to_call(event, chosen[0], calls))]
    if dependency_events:
        parts += ['## 工作流依赖校验失败',
            '模型 JSON 通过后，结果仍须通过输入版本与保存校验。以下记录区分来源、成果、配置或运行范围；不据此推断是谁修改了输入。',
            code(clip(json.dumps(dependency_events[-3:], ensure_ascii=False, indent=2), 9000))]
    if not chosen:parts.append('没有可用的调用快照。旧版本记录缺失时无法补回。')
    for i,call in enumerate(chosen,1):
        cid=call['call_id']
        with store.lock:
            row=store.db.execute('SELECT content FROM model_outputs WHERE run_id=? AND call_id=?',(run_id,cid)).fetchone()
        raw=row['content'] if row else ''
        own=[e for e in events if belongs_to_call(e,call,calls)]
        transport=next((e for e in reversed(own) if e.get('event')=='model.transport_response'),{})
        end=next((e for e in reversed(own) if e.get('event') in ('model.error','model.complete')), {})
        messages=call.get('messages',[])
        user_text=next((text_content(m) for m in messages if m.get('role')=='user'),'')
        try:context=json.loads(user_text)
        except ValueError:context={}
        if not isinstance(context,dict):context={}
        mode='JSON 格式修复' if context.get('json_repair') else '字段校验修复' if context.get('validation_repair') else '首次请求或当前步骤请求'
        parts += [f'## 调用 {i} · {mode}',f'调用 ID：{cid}\n时间：{call.get("at","未知")}\n任务：{call.get("task","未知")}\n模型：{call.get("model","未知")}\n耗时：{end.get("elapsed_ms","未记录")} ms\n结束原因：{transport.get("finish_reason","未记录")}',
            f'输入字符数：{len(user_text)}；原始输出字符数：{len(raw)}；原始输出字节数：{len(raw.encode("utf-8"))}；SHA-256：{hashlib.sha256(raw.encode()).hexdigest()}']
        rejected=[e for e in own if e.get('event')=='batch.validation_failed']
        if rejected:
            parts+=['### 本次调用结论：字段或业务校验未通过',code(clip(json.dumps([e.get('errors',[]) for e in rejected],ensure_ascii=False,indent=2),5000)),
                    '以上是这次返回的校验结果；任务仍在 running 时，可能正在自动修复，不代表整个任务已终止。']
        local=[e for e in own if e.get('event')=='model.json_local_repair']
        if local:parts+=['本次原文缺少容器结束符，系统已仅补齐 '+str(local[-1].get('appended_closers'))+'，随后继续字段校验。原文未改写。']
        issue={}
        if raw:
            try:
                parse_model_object(raw)
                parts.append('JSON 语法：通过本版解析器。若任务仍失败，请看下面的字段校验或接口错误。')
            except (ValueError,json.JSONDecodeError) as exc:
                issue=parse_issue(exc)
                parts.append('JSON 语法错误：'+code(json.dumps(issue,ensure_ascii=False,indent=2)))
            parts+=['### 原始返回或错误片段',response_excerpt(raw,issue)]
        else:parts.append('未保存模型文本返回；可能在网络或接口协议层失败。')
        specific=[{k:e[k] for k in ('event','parse_error','validation_error','validation_errors','errors','error_types','http_status','stack','appended_closers',
            'dependency_changes','dependency_phase','dependency_task','dependency_kind') if k in e} for e in own if e.get('event') in ('model.invalid_json','model.error','node.error','batch.validation_failed','model.transport_error','model.json_local_repair')]
        if specific:parts+=['### 本次调用的错误记录',code(clip(json.dumps(specific,ensure_ascii=False,indent=2),3500))]
        if i==1:
            system=next((text_content(m) for m in messages if m.get('role')=='system'),'')
            parts+=['### 实际系统提示词（本步骤只列一次）',code(clip(system,6000))]
        contract={k:context[k] for k in ('current_stage','stage_instruction','output_contract') if k in context}
        for field in ('json_repair','validation_repair'):
            if isinstance(context.get(field),dict):contract[field]={k:v for k,v in context[field].items() if k not in ('previous_response','previous_response_text')}
        if contract:parts+=['### 本次输出或修复要求',code(clip(json.dumps(contract,ensure_ascii=False,indent=2),2400))]
    if run.get('validation_errors'):parts+=['## 当前失败的字段校验',code(clip(json.dumps(run['validation_errors'],ensure_ascii=False,indent=2),3000))]
    report='\n\n'.join(parts)+'\n'
    if len(report.encode('utf-8'))>MAX_REPORT_BYTES:
        report=clip(report,MAX_REPORT_BYTES-180)+'\n\n[文件已达 64 KiB 上限。可点击单次调用旁的“下载本次错误”单独查看。]\n'
    return report.encode('utf-8')
