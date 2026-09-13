"""Regression for the user's three failed JSON calls and a small downloadable report."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient

from tcg.json_output import parse_model_object, parse_issue
from tcg.failure_report import MAX_REPORT_BYTES
from tcg.main import create_app
from tcg.model import LangChainGateway, SYSTEM, TASK_INSTRUCTIONS
from tcg.schemas import DomainError
from test_backend_api import setup_chat, start, until
from test_gateway_v22 import configured, response, run_with_transport


@pytest.mark.parametrize('prefix', ['', '  \n', '```json\n', 'Here is the result:\n'])
def test_error_position_points_into_original_response(prefix):
    raw=prefix+'{"mermaid":"mindmap\n  root((登录))"}'
    with pytest.raises(json.JSONDecodeError) as caught:
        parse_model_object(raw)
    issue=parse_issue(caught.value)
    assert issue['position']==raw.index('\n',raw.index('mindmap'))
    assert issue['line']==raw[:issue['position']].count('\n')+1
    assert issue['message']=='Invalid control character at'


def test_valid_mindmap_is_preserved_and_extra_objects_rejected():
    value={'mermaid':'mindmap\n  root((登录))\n    引号 " 和反斜杠 \\'}
    assert parse_model_object('```json\n'+json.dumps(value,ensure_ascii=False)+'\n```')==value
    for raw in ('[]','{} {}','{"items":[]}\n{"items":[]}'):
        with pytest.raises(ValueError):parse_model_object(raw)


def test_bad_transport_envelope_does_not_trigger_model_json_repair(tmp_path):
    with pytest.raises(DomainError) as caught:
        run_with_transport(configured(tmp_path),lambda request:httpx.Response(200,content=b'truncated{'))
    assert caught.value.parse_error['layer']=='http_response_envelope'
    assert not hasattr(caught.value,'raw_response')


def test_failed_step_download_contains_three_calls_but_not_large_inputs(tmp_path):
    seen=[]
    raw=json.dumps({'notes':'模型业务分析内容'*5000},ensure_ascii=False)[:-1]+',"mermaid":"mindmap\n  root((登录))"}'
    error_position=raw.index('\n')
    def handler(request):
        body=json.loads(request.content)
        task=next(k for k,v in TASK_INSTRUCTIONS.items() if SYSTEM+'\nTASK CONTRACT:\n'+v==body['messages'][0]['content'][0]['text'])
        context=json.loads(body['messages'][1]['content'][0]['text'])
        seen.append((task,context))
        return httpx.Response(200,json=response(json.dumps({'intent':'generate_case'}) if task=='route' else raw))
    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway=LangChainGateway(configured(tmp_path),http_client=http_client)
    app=create_app(tmp_path,gateway)
    try:
        with TestClient(app) as client:
            _,chat,_=setup_chat(client,'DO_NOT_EXPORT_FULL_REQUIREMENT '+('登录需求 '*20000))
            run=until(client,start(client,chat,intent='auto',experience='reliable'))
            assert run['status']=='failed',run
            assert run['failed_node']=='understand'
            assert len(seen)==4
            assert '第 1 行' in run['error']
            report=client.get('/api/runs/'+run['id']+'/failed-step')
            assert report.status_code==200
            assert report.headers['content-type'].startswith('text/markdown')
            assert 'attachment;' in report.headers['content-disposition']
            assert len(report.content)<=MAX_REPORT_BYTES
            text=report.text
            assert text.count('## 调用 ')==3
            assert text.count('JSON 格式修复')==2
            assert 'DO_NOT_EXPORT_FULL_REQUIREMENT' not in text
            assert 'PRIVATE-GATEWAY-KEY' not in text
            assert '"task": "route"' not in text
            assert '实际系统提示词' in text and 'Return one JSON object only' in text
            assert 'JSON syntax requirements' in text
            assert f'"position": {error_position}' in text
            assert 'mindmap' in text and 'root((登录))' in text
            assert '仅为日志节选' in text
            assert 'previous_response_text' not in text
            calls=app.state.store.db.execute('SELECT call_id,payload FROM model_requests WHERE run_id=?',(run['id'],)).fetchall()
            ids=[row['call_id'] for row in calls if json.loads(row['payload'])['node']=='understand']
            one=client.get('/api/runs/'+run['id']+'/failed-step',params={'call_id':ids[-1]})
            assert one.status_code==200 and one.text.count('## 调用 ')==1
            assert ids[0] not in one.text
            assert client.get('/api/runs/'+run['id']+'/failed-step?call_id=foreign-call').status_code==404
            assert len(seen)==4, 'Downloading must not call the model again'
    finally:
        import asyncio
        asyncio.run(http_client.aclose())
