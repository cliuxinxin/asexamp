import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';
const dom=new JSDOM('<!doctype html><html><body></body></html>');
Object.defineProperty(globalThis,'window',{value:dom.window,configurable:true});
Object.defineProperty(globalThis,'document',{value:dom.window.document,configurable:true});
const {checkDiagram}=await import('../src/BusinessDiagram');
const {default:mermaid}=await import('mermaid');
mermaid.initialize({startOnLoad:false,securityLevel:'strict',htmlLabels:false});

test('accept and parse the actual Chinese mindmap from the user analysis',async()=>{
 const report=JSON.parse(await readFile(new URL('../../tests/fixtures/user_analysis.json',import.meta.url),'utf8')).report;
 const source=report.diagrams.find((d:{mermaid:string})=>d.mermaid.startsWith('mindmap')).mermaid;
 const parsed=await mermaid.parse(checkDiagram('```mermaid\n'+source+'\n```'));
 assert.equal(parsed&&parsed.diagramType,'mindmap');
});

test('existing graph types parse and unsafe directives remain blocked',async()=>{
 for(const source of ['flowchart TD\n A[登录] --> B[首页]','stateDiagram-v2\n [*] --> Ready'])assert.ok(await mermaid.parse(checkDiagram(source)));
 assert.throws(()=>checkDiagram('mindmap\n root((登录))\n  <script>alert(1)</script>'));
 assert.throws(()=>checkDiagram('mindmap\n %%{init: {}}%%\n root((登录))'));
});
