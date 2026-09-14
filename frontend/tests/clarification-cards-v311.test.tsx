import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within,act}=await import('@testing-library/react');
const {ConversationPrompt}=await import('../src/ConversationPrompt');
const {ConversationSuggestions}=await import('../src/ConversationSuggestions');
afterEach(cleanup);
const prompt={id:'clarify:1',kind:'clarification',title:'请澄清登录规则',message:'只提交当前问题的答案。',questions:[
 {id:'Q1',question:'错误后保留输入吗？',suggestion:'建议保留输入。',options:[{id:'keep',label:'是，保留',answer:'发生登录错误后保留用户已输入的账号。'},{id:'clear',label:'否，清空',answer:'发生登录错误后清空用户已输入的账号。'}]},
 {id:'Q2',question:'锁定多久？',suggestion:'锁定 15 分钟后自动解锁。'},
]};

test('choices sit with their question and send the full answer without touching draft text',async()=>{
 const calls:any[]=[];
 render(<><ConversationPrompt prompt={prompt} onAnswer={(...args)=>{calls.push(args);return true;}}/><textarea aria-label="保留草稿" defaultValue="未发送的额外说明"/><ConversationSuggestions prompt={prompt} onChoose={()=>{throw new Error('A single answer should not use composer suggestions.');}}/></>);
 const question=screen.getByRole('group',{name:'Q1 错误后保留输入吗？'});
 assert.ok(within(question).getByText('发生登录错误后清空用户已输入的账号。'));
 await act(async()=>fireEvent.click(within(question).getByRole('button',{name:'否，清空'})));
 await waitFor(()=>assert.deepEqual(calls,[['Q1','错误后保留输入吗？','发生登录错误后清空用户已输入的账号。']]));
 await waitFor(()=>assert.equal(screen.queryByRole('button',{name:'是，保留'}),null));
 assert.equal((screen.getByLabelText('保留草稿') as HTMLTextAreaElement).value,'未发送的额外说明');
 assert.ok(screen.getByRole('button',{name:'采用 Q2 建议：锁定 15 分钟后自动解锁。'}));
 const composerSuggestions=screen.getByRole('group',{name:'快捷回复'});
 assert.equal(within(composerSuggestions).queryByRole('button',{name:/采用 Q[12]/}),null);
 assert.equal(within(composerSuggestions).queryByRole('button',{name:/采用全部/}),null);
});

test('unstructured questions do not acquire invented yes/no and historical questions have no actions',()=>{
 const questionOnly={...prompt,questions:[prompt.questions[1],{id:'Q3',question:'需要哪些字段？'}]};
 const view=render(<ConversationPrompt prompt={questionOnly} onAnswer={()=>true}/>);
 assert.equal(screen.getAllByRole('button').length,1);
 assert.equal(screen.queryByRole('button',{name:/^(是|否|Yes|No)$/}),null);
 view.rerender(<ConversationPrompt prompt={questionOnly} active={false} onAnswer={()=>true}/>);
 assert.equal(screen.queryByRole('button'),null);
 assert.ok(screen.getByText('锁定多久？'));
});

test('pending answers prevent duplicate sends and failures retain an explicit retry',async()=>{
 let reject!:(error:Error)=>void;let calls=0;
 const pending=new Promise<boolean>((_resolve,fail)=>{reject=fail;});
 const view=render(<ConversationPrompt prompt={prompt} onAnswer={()=>{calls++;return pending;}}/>);
 const button=screen.getByRole('button',{name:'是，保留'});
 fireEvent.click(button);fireEvent.click(button);assert.equal(calls,1);
 assert.equal((screen.getByRole('button',{name:'否，清空'}) as HTMLButtonElement).disabled,true);
 await act(async()=>reject(new Error('提交失败，请重试')));
 assert.ok(screen.getByRole('alert').textContent?.includes('提交失败，请重试'));
 await waitFor(()=>assert.equal((button as HTMLButtonElement).disabled,false));
 view.rerender(<ConversationPrompt prompt={{...prompt,id:'clarify:2',questions:[prompt.questions[1]]}} onAnswer={()=>true}/>);
 assert.equal(screen.queryByRole('button',{name:'是，保留'}),null);
 assert.equal(screen.queryByRole('alert'),null);
});
