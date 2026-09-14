export type Json = Record<string, any>;
export type Project = {id:string;name:string;created_at:string};
export type Chat = {id:string;project_id:string;profile_id?:string;title:string;updated_at:string};
export type Profile = {id:string;project_id:string;name:string;version:number;config:Json};
export type Source = {id:string;name:string;role:string;characters:number;classification?:{label:string;reason:string;provisional:boolean};text?:string;chunks?:{id:string;text:string;location:any}[]};
export type Message = {id:string;role:'user'|'assistant';content:string;created_at:string;metadata:Json};
export type Depth = 'auto'|'quick'|'standard'|'deep';
export const depthNames:Record<Depth,string>={auto:'遵循 Profile',quick:'快速设计',standard:'标准设计',deep:'深度设计'};
export type Coverage = {requirements_total:number;requirements_covered:number;branches_total:number;branches_covered:number;gaps:Json[]};
export type AgentState = {depth?:Depth;rationale?:string;plan?:{id:string;title:string;status:string}[];insights?:{id:string;summary:string;refs:string[];kind:string}[];summary?:string;coverage?:Coverage;pending_instructions?:number};
export type Recovery = {category:string;title:string;detail:string;suggestions:string[];preserved:string[];retryable:boolean};
export type RunProgress = {phase:string;completed:number;total:number;label:string};
export type QuestionSuggestion = {question:string;answer:string;basis:string;refs:string[];confidence:'supported'|'assumption'};
export type RunInterrupt = {type:string;questions?:string[];question_suggestions?:QuestionSuggestion[];artifact_id?:string;artifact_revision?:number;sources?:Array<{id:string;name:string;role:string}>;message?:string;suggested_text?:string;recommended_depth?:string;items?:Json[];title?:string;confirm_label?:string;next_stage?:string;phase?:string;reason?:string;stop_after?:string};
export type Run = {id:string;chat_id?:string;interrupt_id?:string;control_version?:number;artifact_revision?:number;status:string;intent:string;mode:string;stage:string;stop_after?:string;pause_contract?:number;error?:string;created_at?:string;updated_at:string;diagnostic?:Json|null;artifact_ids:string[];experience?:'legacy'|'agent'|'reliable';progress?:RunProgress;graph_version?:number;draft_artifact_id?:string;edit_in_progress?:boolean;generation_plan?:{input_groups:number;reason:string;context_tokens:number;output_reserve:number;token_estimate:number};agent?:AgentState;recovery?:Recovery;interrupt?:RunInterrupt};
export type MemoryEntry = {id:string;content:string;kind:'preference'|'business';active:boolean};
export type ConversationReview = {summary:string;issues:{title:string;detail?:string;case_ids?:string[];refs?:string[]}[];scope?:{reviewed_count?:number;total_count?:number};notes?:string[]};
export type ConversationPrompt = {id:string;kind:string;title:string;message:string;run_id?:string;artifact_id?:string;artifact_revision?:number;busy?:boolean;questions?:{id:string;question:string;suggestion?:string;answer?:string}[];choices?:{id:string;title:string}[];review?:ConversationReview};
export type Snapshot = {chat:Chat;messages:Message[];sources:Source[];runs:Run[];memory?:Json;conversation_prompt?:ConversationPrompt|null};
// view_item_ids is a local presentation scope for partial read-tool results, never persisted.
export type Artifact = {id:string;project_id?:string;chat_id?:string;type:string;title:string;revision:number;items:Json[];report?:Json;view_item_ids?:string[]};
export type Settings = {provider:'ollama'|'openai'|'azure';base_url:string;model:string;api_version?:string|null;has_api_key:boolean;timeout_seconds:number;context_window:number;output_tokens:number;output_limit_mode:'request'|'server';server_output_tokens?:number|null;timeout_policy?:string;environment_managed?:boolean;env_file?:string|null;header_names?:string[];has_headers?:boolean;auth_mode?:'bearer'|'headers'};
export const intents=[['auto','自动识别'],['review_requirement','分析需求'],['generate_scenario','生成场景'],['generate_case','生成用例'],['review_case','评审用例'],['query','证据问答'],['learn_template','学习模板']];
export const roles:Record<string,string>={auto:'自动识别',primary:'主要需求',change:'变更',supplement:'补充',clarification:'澄清',example:'参考样例',knowledge:'知识来源'};
export const busy=(run?:Run)=>!!run&&['queued','running','waiting'].includes(run.status);

export type ClarificationQuestion={id:string;question:string;answer:string;suggestion:Omit<QuestionSuggestion,'question'>;adopted:boolean};
export type ClarificationDraft={id:string;run_id:string;revision:number;question_set_version:string;questions:ClarificationQuestion[];answer:string;submitted:boolean;source_id:string|null;shared?:boolean};
export type TurnCommand={name:string;arguments:Json};
export type TurnPart=
 |{type:'assistant_note';text:string}
 |{type:'answer';text:string;refs?:string[]}
 |{type:'diagnostic';reference_id:string;call_id?:string;category:string;message:string;hints:string[];log_path:string}
 |{type:'artifact';artifact_id:string;revision:number}
 |{type:'case_details';artifact_id:string;revision:number;items:Json[];title?:string}
 |{type:'diff';proposal_id:string;changes:Json[]}
 |{type:'coverage';data:Json}
 |{type:'source_impact';data:Json}
 |{type:'files';files:{name:string;url:string}[]}
 |{type:'clarification_draft';draft:ClarificationDraft}
 |{type:'estimate';data:Json};
export type TurnResponse={id:string;client_message_id:string;status:string;message:string;parts:TurnPart[];pending:Json[];actions:Json[]};
export type TurnRequest={client_message_id:string;content:string;intent_hint?:string;artifact_id?:string;artifact_revision?:number;selected_ids?:string[];view_order?:string[];profile_id?:string;mode?:string;source_ids?:string[];reply_to?:string;reply_kind?:'confirm'|'clarification'|'question';command?:TurnCommand;depth?:Depth;case_types?:string[];profile_override?:Json;as_requirement?:boolean;experience?:string};
