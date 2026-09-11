import type {QuestionSuggestion} from './types';

export function clarificationSuggestions(questions:string[]=[],suggestions:QuestionSuggestion[]=[]):QuestionSuggestion[]{
 return [...new Set(questions)].filter(question=>question.trim()).map(question=>
  suggestions.find(item=>item.question===question&&item.answer?.trim())??{
   question,
   answer:`建议暂按现有需求中已明确的规则设计；对于“${question}”涉及的未明确条件，先标记为待确认，不新增限制或例外。`,
   basis:'当前没有明确答案，这是暂定的测试设计假设，采用前可修改。',
   refs:[],
   confidence:'assumption',
  });
}

function answerBlocks(draft:string,question:string){
 const marker=`问题：${question}\n回答：`;
 const blocks:{answerStart:number;value:string}[]=[];
 let start=draft.indexOf(marker);
 while(start!==-1){
  if(start===0||draft[start-1]==='\n'){
   const answerStart=start+marker.length;
   const nextQuestion=draft.indexOf('\n问题：',answerStart);
   // A blank paragraph separates manual notes from the structured answer.
   const value=draft.slice(answerStart,nextQuestion===-1?draft.length:nextQuestion).split(/\n[ \t]*\n/,1)[0];
   blocks.push({answerStart,value});
  }
  start=draft.indexOf(marker,start+marker.length);
 }
 return blocks;
}

export function hasClarificationAnswer(draft:string,question:string){
 return answerBlocks(draft,question).some(block=>block.value.trim());
}

export function adoptClarificationSuggestions(draft:string,suggestions:QuestionSuggestion[]){
 return suggestions.reduce((current,suggestion)=>{
  const blocks=answerBlocks(current,suggestion.question);
  if(blocks.some(block=>block.value.trim()))return current;
  if(blocks.length){
   // Restore an emptied answer in place, keeping its question and all other draft text.
   const insertAt=blocks[0].answerStart;
   return current.slice(0,insertAt)+suggestion.answer+current.slice(insertAt);
  }
  return [current,`问题：${suggestion.question}\n回答：${suggestion.answer}`].filter(Boolean).join('\n\n');
 },draft);
}
