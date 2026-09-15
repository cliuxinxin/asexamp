import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','CustomEvent','File','FormData'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};

const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {App}=await import('../src/App');
const {ConversationPrompt,clarificationReplyText}=await import('../src/ConversationPrompt');
const {ConversationSuggestions}=await import('../src/ConversationSuggestions');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});

const optionAnswer='发生登录错误后保留用户已输入的账号。';
const questionWithOptions={id:'Q1',question:'错误后保留输入吗？',suggestion:'建议保留输入。',options:[
 {id:'keep',label:'是，保留',answer:optionAnswer},
 {id:'clear',label:'否，清空',answer:'发生登录错误后清空用户已输入的账号。'},
]};
const suggestedQuestion={id:'Q2',question:'锁定多久？',suggestion:'锁定 15 分钟后自动解锁。'};
const clarification={id:'clarify:fill',kind:'clarification',title:'请补充登录规则',message:'选择仅回答当前问题。',run_id:'run',questions:[questionWithOptions,suggestedQuestion]};

test('question option and suggestion fill actions provide prompt-scoped text without adopting the answer',async()=>{
 const answers:any[]=[];const fills:string[]=[];
 render(<ConversationPrompt prompt={clarification} onAnswer={(...args)=>answers.push(args)} onFill={text=>fills.push(text)}/>);

 const optionGroup=screen.getByRole('group',{name:'Q1 错误后保留输入吗？'});
 fireEvent.click(within(optionGroup).getByRole('button',{name:'填入输入框：是，保留'}));
 assert.deepEqual(fills,[clarificationReplyText('Q1','错误后保留输入吗？',optionAnswer)]);
 assert.deepEqual(answers,[]);

 const suggestionGroup=screen.getByRole('group',{name:'Q2 锁定多久？'});
 fireEvent.click(within(suggestionGroup).getByRole('button',{name:'填入输入框：锁定 15 分钟后自动解锁。'}));
 assert.deepEqual(fills,[
  clarificationReplyText('Q1','错误后保留输入吗？',optionAnswer),
  clarificationReplyText('Q2','锁定多久？','锁定 15 分钟后自动解锁。'),
 ]);
 assert.deepEqual(answers,[]);

 fireEvent.click(within(optionGroup).getByRole('button',{name:'是，保留'}));
 const adopt=within(suggestionGroup).getByRole('button',{name:'采用 Q2 建议：锁定 15 分钟后自动解锁。'}) as HTMLButtonElement;
 await waitFor(()=>{assert.equal(answers.length,1);assert.equal(adopt.disabled,false);});
 fireEvent.click(adopt);
 await waitFor(()=>assert.equal(answers.length,2));
 assert.deepEqual(answers,[
  ['Q1','错误后保留输入吗？',optionAnswer],
  ['Q2','锁定多久？','锁定 15 分钟后自动解锁。'],
 ]);
});

test('question fill actions cannot run while waiting, inactive, or answered',()=>{
 const fills:string[]=[];
 const view=render(<ConversationPrompt prompt={clarification} waiting onAnswer={()=>{}} onFill={text=>fills.push(text)}/>);
 const waitingFill=screen.getByRole('button',{name:'填入输入框：是，保留'}) as HTMLButtonElement;
 assert.equal(waitingFill.disabled,true);fireEvent.click(waitingFill);assert.deepEqual(fills,[]);

 view.rerender(<ConversationPrompt prompt={clarification} active={false} onAnswer={()=>{}} onFill={text=>fills.push(text)}/>);
 assert.equal(screen.queryByRole('button',{name:/^填入输入框：/}),null);

 view.rerender(<ConversationPrompt prompt={{...clarification,questions:[{...questionWithOptions,answer:optionAnswer}]}} onAnswer={()=>{}} onFill={text=>fills.push(text)}/>);
 assert.equal(screen.queryByRole('button',{name:/^填入输入框：/}),null);
});

test('text quick replies keep direct send and add a disabled-aware fill action, while navigation stays action-only',()=>{
 const sends:any[]=[];const fills:string[]=[];
 const selection={id:'select:1',kind:'selection',title:'选择资料用途',message:'请选择。',choices:[{id:'supplement',title:'作为补充需求'}]};
 const view=render(<ConversationSuggestions prompt={selection} onChoose={(...args)=>sends.push(args)} onFill={text=>fills.push(text)}/>);
 fireEvent.click(screen.getByRole('button',{name:'填入输入框：作为补充需求'}));
 assert.deepEqual(fills,['作为补充需求']);assert.deepEqual(sends,[]);
 fireEvent.click(screen.getByRole('button',{name:'作为补充需求'}));
 assert.deepEqual(sends,[['作为补充需求',undefined]]);

 view.rerender(<ConversationSuggestions prompt={selection} disabled onChoose={(...args)=>sends.push(args)} onFill={text=>fills.push(text)}/>);
 const disabledFill=screen.getByRole('button',{name:'填入输入框：作为补充需求'}) as HTMLButtonElement;
 assert.equal(disabledFill.disabled,true);fireEvent.click(disabledFill);assert.deepEqual(fills,['作为补充需求']);

 view.rerender(<ConversationSuggestions prompt={{id:'profile',kind:'profile',title:'模板建议',message:'查看更改'}} onChoose={()=>{}} onFill={text=>fills.push(text)} onViewProfileChange={()=>{}}/>);
 assert.equal(screen.queryByRole('button',{name:/^填入输入框：/}),null);
 assert.ok(screen.getByRole('button',{name:'查看 Profile 更改'}));
});

const json=(value:unknown)=>new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
function appFixture(){
 const turns:any[]=[];const posts:string[]=[];
 const state:any={chat:{id:'chat',project_id:'project',title:'登录测试'},sources:[],messages:[{id:'assistant',role:'assistant',content:'请补充登录规则。',metadata:{}}],runs:[{id:'run',chat_id:'chat',intent:'generate_case',mode:'hitp',stage:'clarification',status:'waiting',artifact_ids:[],updated_at:'2026-09-15T00:00:00Z'}],conversation_prompt:structuredClone(clarification)};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');
  if(init.method==='POST')posts.push(path);
  if(path==='/projects')return json([{id:'project',name:'登录项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat')return json(state);
  if(path==='/chats/chat/turns'){turns.push(JSON.parse(init.body));return json({id:'turn',status:'succeeded',message:'已处理',parts:[],pending:[],actions:[]});}
  if(path.endsWith('/workspace-state'))return json({});
  return json([]);
 }) as typeof fetch;
 return {turns,posts};
}

test('App appends prompt and quick-reply text into the focused draft without a POST',async()=>{
 const requests=appFixture();render(<App/>);
 const optionGroup=await screen.findByRole('group',{name:'Q1 错误后保留输入吗？'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'保留这段草稿'}});

 fireEvent.click(within(optionGroup).getByRole('button',{name:'填入输入框：是，保留'}));
 assert.equal(input.value,'保留这段草稿\n'+clarificationReplyText('Q1','错误后保留输入吗？',optionAnswer));
 assert.equal(document.activeElement,input);assert.equal(requests.turns.length,0);assert.deepEqual(requests.posts,[]);

 fireEvent.click(screen.getByRole('button',{name:'填入输入框：先解释澄清问题'}));
 await waitFor(()=>assert.equal(input.value,'保留这段草稿\n'+clarificationReplyText('Q1','错误后保留输入吗？',optionAnswer)+'\n解释这些澄清问题的依据，不采用答案也不继续流程。'));
 assert.equal(document.activeElement,input);assert.equal(requests.turns.length,0);assert.deepEqual(requests.posts,[]);
 assert.ok(within(optionGroup).getByRole('button',{name:'是，保留'}));
});
