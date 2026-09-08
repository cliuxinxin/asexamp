import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const modulePath='../deploy.mjs';
const {deploymentInput,cloudflareApi,provision}=await import(modulePath);
const configured={CLOUDFLARE_API_TOKEN:'FAKE-TOKEN',CLOUDFLARE_ACCOUNT_ID:'a'.repeat(32),TCG_ACCESS_TEAM_DOMAIN:'https://example.cloudflareaccess.com',TCG_ACCESS_AUD:'b'.repeat(64)};
const base=JSON.parse(readFileSync('wrangler.jsonc','utf8'));

test('deployment rejects missing configuration before any remote operation',()=>{
 assert.throws(()=>deploymentInput({}),/缺少 CLOUDFLARE_API_TOKEN/);
 assert.throws(()=>deploymentInput({...configured,TCG_ACCESS_AUD:''}),/缺少 TCG_ACCESS_AUD/);
 assert.throws(()=>deploymentInput({...configured,TCG_ACCESS_TEAM_DOMAIN:'http://localhost'}),/Access/);
 assert.equal(deploymentInput(configured).name,'asexamp');
});

test('provisioning reuses existing app resources and writes a credential-free Wrangler config',async()=>{
 const writes:any[]=[];
 const api=async(path:string,method='GET',body?:any)=>{
  if(method!=='GET'){writes.push({path,method,body});throw new Error('Must reuse existing resources');}
  if(path.endsWith('/settings'))return {result:{bindings:[{name:'TCG_APP_ID',text:'cliuxinxin/asexamp'},{name:'DB',type:'d1',id:'db-id'},{name:'BUCKET',type:'r2_bucket',bucket_name:'asexamp-uploads'}]}};
  if(path.startsWith('/d1/database?'))return {result:[{name:'asexamp-db',uuid:'db-id'}]};
  if(path==='/r2/buckets/asexamp-uploads')return {result:{name:'asexamp-uploads'}};
  throw new Error('Unknown route '+path);
 };
 const {config}=await provision(deploymentInput(configured),api,base);
 assert.equal(writes.length,0);assert.equal(config.d1_databases[0].database_id,'db-id');assert.equal(config.assets.run_worker_first,true);assert.match(config.build.command,/build:cloudflare/);assert.ok(!JSON.stringify(config).includes('FAKE-TOKEN'));
});

test('provisioning creates missing bindings but refuses to overwrite an unrelated Worker',async()=>{
 const writes:any[]=[];const input=deploymentInput(configured);
 const api=async(path:string,method='GET',body?:any)=>{
  if(method==='POST'){writes.push({path,body});return {result:path==='/d1/database'?{name:body.name,uuid:'created-db'}:{name:body.name}};}
  if(path.endsWith('/settings'))return null;
  if(path.startsWith('/d1/database?'))return {result:[]};
  if(path.startsWith('/r2/buckets/'))return null;
  throw new Error('Unknown route');
 };
 const {config}=await provision(input,api,base);assert.equal(config.d1_databases[0].database_id,'created-db');assert.equal(writes.length,2);
 let count=0;await assert.rejects(()=>provision(input,async()=>{count++;return {result:{bindings:[]}};},base),/不属于此项目/);assert.equal(count,1);
});

test('Cloudflare errors omit tokens and provider response bodies',async()=>{
 const api=cloudflareApi(deploymentInput(configured),async()=>Response.json({success:false,errors:[{code:10000,message:'FAKE-TOKEN private response'}]},{status:403}));
 await assert.rejects(()=>api('/workers/scripts/test/settings'),(e:Error)=>e.message.includes('HTTP 403')&&!e.message.includes('FAKE-TOKEN')&&!e.message.includes('private response'));
});

test('unverified D1 or R2 name collisions are rejected before any writes',async()=>{
 for(const collision of ['database','bucket']){
  let writes=0;
  const api=async(path:string,method='GET')=>{
   if(method!=='GET'){writes++;throw new Error('Unexpected write');}
   if(path.endsWith('/settings'))return null;
   if(path.startsWith('/d1/database?'))return {result:collision==='database'?[{uuid:'unrelated-db',name:'asexamp-db'}]:[]};
   if(path.startsWith('/r2/buckets/'))return {result:{name:'asexamp-uploads'}};
   throw new Error('Unknown route');
  };
  await assert.rejects(()=>provision(deploymentInput(configured),api,base),/归属未确认/);
  assert.equal(writes,0);
 }
});

test('explicit resource selection supports recovery after interrupted provisioning',async()=>{
 const uuid='12345678-1234-1234-1234-123456789abc';
 const input=deploymentInput({...configured,TCG_D1_DATABASE_ID:uuid,TCG_R2_BUCKET_NAME:'existing-uploads'});
 const api=async(path:string,method='GET')=>{
  assert.equal(method,'GET');
  if(path.endsWith('/settings'))return null;
  if(path.startsWith('/d1/database?'))return {result:[{uuid,name:'original-database'}]};
  if(path==='/r2/buckets/existing-uploads')return {result:{name:'existing-uploads'}};
  throw new Error('Unknown route');
 };
 const {config}=await provision(input,api,base);
 assert.equal(config.d1_databases[0].database_id,uuid);
 assert.equal(config.d1_databases[0].database_name,'original-database');
 assert.equal(config.r2_buckets[0].bucket_name,'existing-uploads');
});
