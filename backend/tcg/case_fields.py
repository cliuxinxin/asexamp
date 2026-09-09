"""Template field policy shared by authoring, completion and Excel export."""
import copy
import hashlib
import json
import re

def is_description(field):
    return isinstance(field,str) and re.sub(r'[\s_-]','',field).lower() in {
        'description','casedescription','testcasedescription','casedesc','testcasedesc','用例描述','测试用例描述'}

def description_value(row):
    value=row.get('description')
    if isinstance(value,str) and value.strip():return value
    return next((v for k,v in row.items() if is_description(k) and isinstance(v,str) and v.strip()),'')

def description_column(profile):
    return next((c for c in (profile.get('excel_columns') or []) if isinstance(c,dict) and is_description(c.get('field'))),None)


MANUAL_FIELDS = {'actualresult','actualresults','executionresult','executionstatus','testresult',
    'teststatus','tester','executedby','executiondate','executedat','defectid','bugid',
    '实际结果','实际执行结果','执行结果','执行状态','测试结果','测试状态','执行人','测试人员','测试人','执行日期','测试日期','缺陷编号'}

def filled(value):
    # Zero and False are valid values, not missing fields.
    return value is not None and (bool(value.strip()) if isinstance(value,str) else bool(value) if isinstance(value,(list,dict)) else True)

def template_columns(profile):
    columns=[]
    for column in profile.get('excel_columns') or []:
        field=column['field']
        if is_description(field):field='description'
        inferred='derived' if field in ('steps','expected') else 'manual' if any(re.sub(r'[\s_-]','',label).lower() in MANUAL_FIELDS for label in (field,column.get('header',''))) else 'ai'
        source=column.get('value_source') or inferred
        required=column.get('required',source=='ai' and field not in ('preconditions','scenario_id'))
        columns.append({**column,'field':field,'value_source':source,'required':required})
    return columns

def field_value(row,column):
    field=column['field']
    if field in ('steps','expected'):
        key='action' if field=='steps' else 'expected'
        return [s.get(key,'') for s in row.get('steps',[]) if isinstance(s,dict) and filled(s.get(key))]
    value=description_value(row) if is_description(field) else row.get(field)
    if not filled(value) and column.get('value_source')=='default':return column.get('default_value')
    return value

def template_check(profile,rows):
    columns=template_columns(profile);missing=[]
    for row in rows:
        seen=set()
        for column in columns:
            field=column['field']
            if field in seen or column['value_source']!='ai' or not column['required'] or filled(field_value(row,column)):continue
            seen.add(field)
            note=row.get('_template_field_notes',{}).get(field,{})
            reason=note.get('reason','') if isinstance(note,dict) and note.get('contract')==column_signature(column) else ''
            missing.append({'id':row['id'],'field':field,'header':column['header'],'reason':reason})
    return {'columns':columns,'missing':missing,'manual_columns':[c['header'] for c in columns if c['value_source']=='manual'],
            'optional_columns':[c['header'] for c in columns if c['value_source']=='ai' and not c['required']]}

def column_signature(column):
    return hashlib.sha256(json.dumps(column,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def materialize_fields(rows,profile):
    result=copy.deepcopy(rows)
    for row in result:
        if not isinstance(row.get('_template_field_notes',{}),dict):row.pop('_template_field_notes',None)
        for column in template_columns(profile):
            field=column['field']
            if column['value_source']=='default' and not filled(row.get(field)):
                row[field]=copy.deepcopy(column['default_value'])
            elif field=='description' and description_value(row):row['description']=description_value(row)
            if filled(field_value(row,column)):
                row.get('_template_field_notes',{}).pop(field,None)
        if not row.get('_template_field_notes'):row.pop('_template_field_notes',None)
    return result

def protect_non_ai_fields(rows,profile,original=()):
    """Model output cannot invent or overwrite manual execution data or fixed defaults."""
    by_id={r['id']:r for r in original};result=copy.deepcopy(rows)
    for row in result:
        previous=by_id.get(row.get('id'),{}) if isinstance(row.get('id'),str) else {}
        row.pop('_template_field_notes',None)
        if isinstance(previous.get('_template_field_notes'),dict):row['_template_field_notes']=copy.deepcopy(previous['_template_field_notes'])
        for column in template_columns(profile):
            field=column['field']
            if column['value_source'] not in ('manual','default'):continue
            if field in previous:row[field]=copy.deepcopy(previous[field])
            else:row.pop(field,None)
    return materialize_fields(result,profile)
