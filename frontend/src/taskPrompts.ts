export const TASK_PROMPTS:Record<string,string>={
 review_requirement:'请分析当前需求，提取业务规则、范围、歧义并绘制业务图。',
 generate_scenario:'请基于当前需求理解生成测试场景，并保留需求追溯关系。',
 generate_case:'请基于当前测试场景生成可追溯的测试用例。',
 review_case:'请评审当前测试用例，只做有依据的局部优化。',
 query:'请根据当前需求资料回答：',
 learn_template:'请学习附件中的模板，识别它是场景模板还是用例模板，并给出字段定义和应用建议。',
};

export function nextTaskDraft(current:string,previousIntent:string,nextIntent:string,selectionStart?:number,selectionEnd?:number){
 const previous=TASK_PROMPTS[previousIntent];
 const prompt=TASK_PROMPTS[nextIntent];
 if(!prompt)return current;
 if(!current.trim()||current===previous)return prompt;
 if(selectionStart!==undefined&&selectionEnd!==undefined&&selectionEnd>selectionStart)return current.slice(0,selectionStart)+prompt+current.slice(selectionEnd);
 return current.endsWith(prompt)?current:current+'\n\n'+prompt;
}
