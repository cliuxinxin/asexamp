import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
import {diffColumns,diffText,equalConfig} from '../src/profile-diff';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {render,screen,cleanup,fireEvent,waitFor,within}=await import('@testing-library/react');
const {ProfileConfigDiff}=await import('../src/ProfileConfigDiff');
afterEach(cleanup);

test('Chinese text changes highlight only edits and preserve both exact values including emoji',()=>{
 const before='步骤使用简体中文，每条写 2 步。😀\n保留完整规范。';
 const after='步骤使用简体中文，每条写 3 步。😃\n保留完整规范。';
 const parts=diffText(before,after);
 assert.equal(parts.filter(part=>part.kind!=='add').map(part=>part.text).join(''),before);
 assert.equal(parts.filter(part=>part.kind!=='remove').map(part=>part.text).join(''),after);
 const {container}=render(<ProfileConfigDiff before={before} after={after} field="template_rules"/>);
 assert.deepEqual([...container.querySelectorAll('del')].map(node=>node.textContent),['2','😀']);
 assert.deepEqual([...container.querySelectorAll('ins')].map(node=>node.textContent),['3','😃']);
 assert.ok(container.textContent?.includes('保留完整规范。'));
 assert.equal(container.querySelector('textarea'),null);
});

test('long dissimilar writing rules keep full content with bounded diff work',()=>{
 const before='共同开始'+('甲乙😀'.repeat(12000))+'共同结尾';
 const after='共同开始'+('丙丁😃'.repeat(12000))+'共同结尾';
 const parts=diffText(before,after);
 assert.equal(parts.filter(part=>part.kind!=='add').map(part=>part.text).join(''),before);
 assert.equal(parts.filter(part=>part.kind!=='remove').map(part=>part.text).join(''),after);
 assert.ok(parts.length<=4);
});

test('column comparison aligns by field and exposes additions, removals, configuration changes, and order',async()=>{
 const before=[{field:'id',header:'编号'},{field:'title',header:'用例标题',definition:'必须验证正确结果',default_value:0},{field:'notes',header:'人工备注'}];
 const after=[{field:'title',header:'测试标题',definition:'必须验证可观察结果'},{field:'id',header:'编号'},{field:'status',header:'执行状态',value_source:'default',default_value:false,required:false}];
 const {container}=render(<ProfileConfigDiff before={before} after={after} field="excel_columns"/>);
 assert.ok(screen.getByText('+ 新增列：执行状态'));
 assert.ok(screen.getByText('- 删除列：人工备注'));
 const title=screen.getByRole('region',{name:'列更改 title'});
 assert.ok(within(title).getByText('默认值'));
 assert.ok(within(title).getByText('未设置'));
 assert.ok(within(title).getByText('0').closest('del'));
 assert.ok(within(title).getByText('可观察').closest('ins'));
 assert.ok(within(title).getByText('正确').closest('del'));
 assert.ok(screen.getByText('false').closest('ins'));
 assert.ok(screen.getByText('↕ 调整顺序：编号'));
 assert.equal(container.querySelector('.profile-full-config .profile-diff-pair'),null);
 const details=screen.getByText('查看完整前后配置及列顺序').closest('details')!;
 details.open=true;fireEvent(details,new dom.window.Event('toggle'));
 await waitFor(()=>assert.ok(details.textContent?.includes('必须验证正确结果')));
 assert.ok(details.textContent?.includes('必须验证可观察结果'));
});

test('inserting a column does not mislabel unchanged columns as moved, and unknown metadata stays readable',()=>{
 const before=[{field:'title',header:'标题',custom:{alpha:1,beta:2}},{field:'steps',header:'步骤'}];
 const after=[{field:'status',header:'状态',default_value:''},{field:'title',custom:{beta:2,alpha:1},header:'标题'},{field:'steps',header:'步骤'}];
 const result=diffColumns(before,after);
 assert.equal(result.unchanged,2);assert.deepEqual(result.changes.map(change=>change.kind),['add']);
 render(<ProfileConfigDiff before={before} after={after} field="scenario_excel_columns"/>);
 assert.ok(screen.getByText('空字符串').closest('ins'));
 assert.ok(screen.getByText('其余 2 列配置未变。'));
 assert.equal(screen.queryByText(/调整顺序/),null);
 assert.equal(equalConfig({field:'a',default_value:0},{field:'a'}),false);
});

test('removed, false, zero, null and empty values remain distinguishable',()=>{
 const props=[{before:undefined,after:false,beforePresent:false},{before:0,after:null},{before:'',after:undefined,afterPresent:false}];
 const {container}=render(<>{props.map((props,index)=><ProfileConfigDiff key={index} {...props}/>)}</>);
 assert.ok(screen.getByText('false').closest('ins'));
 assert.ok(screen.getByText('0').closest('del'));
 assert.ok(screen.getByText('空值（null）').closest('ins'));
 assert.ok(screen.getByText('空字符串').closest('del'));
 assert.equal(screen.getAllByText('未设置').length,2);
 assert.equal(container.querySelector('script'),null);
});
