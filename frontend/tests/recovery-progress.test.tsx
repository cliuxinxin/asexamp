import {test} from 'node:test';
import assert from 'node:assert/strict';
import {emptyFeed,reduceEvent} from '../src/runProgress';

test('applied repair stays distinguishable from a later remaining validation error',()=>{
 let feed=emptyFeed();let id=0;
 const event=(data:Record<string,unknown>)=>{feed=reduceEvent(feed,'progress',++id,{node:'agent_work',at:'2026-09-09T00:00:00Z',...data});};
 event({event:'model.start',call_id:'fix',task:'agent_repair_batch'});
 event({event:'model.complete',call_id:'fix'});
 event({event:'agent.repair_applied',field_count:3});
 event({event:'agent.validation_failed',validation_error:{path:'nodes[0].refs[0]',expected:'provided_evidence_id',actual:'string'}});
 assert.equal(feed.calls.fix.status,'applied');
 assert.ok(feed.entries.some(e=>e.text.includes('引用')&&e.text.includes('当前')));
 event({event:'agent.repair_complete'});
 assert.ok(feed.entries.some(e=>e.text.includes('继续')));
});

test('rejected repair is visible and is not presented as applied',()=>{
 let feed=reduceEvent(emptyFeed(),'progress',1,{event:'model.start',node:'agent_work',call_id:'bad',task:'agent_repair_batch'});
 feed=reduceEvent(feed,'progress',2,{event:'agent.repair_rejected',node:'agent_work'});
 assert.equal(feed.calls.bad.status,'invalid');
 assert.ok(feed.entries.some(e=>e.text.includes('未应用')));
});
