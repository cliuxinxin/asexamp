export const TASK_PROMPTS:Record<string,string>={
 review_requirement:'请分析当前需求，提取业务规则、范围、歧义并绘制业务图。',
 generate_scenario:'请基于当前需求理解生成测试场景，并保留需求追溯关系。',
 generate_case:'请基于当前测试场景生成可追溯的测试用例。',
 review_case:'请评审当前测试用例，只做有依据的局部优化。',
 query:'请根据当前需求资料回答：',
 learn_template:'请学习已上传的 Excel 模板，自动判断是场景模板、用例模板或同时包含两者，提取工作表、列顺序、字段定义、填写方式和文件名规则。',
};

export function nextTaskDraft(current:string,previousIntent:string,nextIntent:string){
 const previous=TASK_PROMPTS[previousIntent];
 return !current.trim()||(previous!==undefined&&current===previous)?(TASK_PROMPTS[nextIntent]??current):current;
}
