import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {AnalysisReport} from '../src/AnalysisReport';

Object.assign(globalThis,{React});

test('report renders extensible clarification notes and keeps new followups visibly unconfirmed',()=>{
 const html=renderToStaticMarkup(<AnalysisReport report={{summary:'理解已更新',questions:[],
  clarification_note:{summary:{detail:'用户答案已应用'}},clarification_followups:['是否需要审计？']}}/>);
 assert.match(html,/用户答案已应用/);
 assert.match(html,/补充问题（待核实，尚未确认为业务规则）/);
 assert.match(html,/是否需要审计？/);
 assert.doesNotMatch(html,/<strong>需要确认<\/strong>/);
});
