import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const modulePath='../deploy-builds.mjs';
const {buildsInput,deployBuilds}=await import(modulePath);
const env={CLOUDFLARE_API_TOKEN:'FAKE-DEPLOY-TOKEN',CLOUDFLARE_ACCOUNT_ID:'a'.repeat(32)};
const databaseId='12345678-1234-1234-1234-123456789abc';

test('native Builds needs deployment credentials but lets runtime Access configuration follow publication',()=>{
 assert.equal(buildsInput(env).account,'a'.repeat(32));
 assert.throws(()=>buildsInput({}),/CLOUDFLARE_API_TOKEN/);
 assert.throws(()=>buildsInput({...env,CLOUDFLARE_ACCOUNT_ID:'invalid'}),/Account ID/);
});

test('native Builds rejects a CI target name mismatch before publishing or changing resources',async()=>{
 let calls=0;
 await assert.rejects(()=>deployBuilds({input:buildsInput({...env,WRANGLER_CI_OVERRIDE_NAME:'another-worker'}),run:async()=>{calls++;},api:async()=>{calls++;},log:()=>{}}),/名称不一致/);
 assert.equal(calls,0);
});

test('native Builds migrates the actual Worker binding and creates an encryption key only once',async()=>{
 const directory=await mkdtemp(join(tmpdir(),'tcg-builds-'));
 const events:string[]=[];let savedSecret:any;
 const api=async(path:string,method='GET',body?:any)=>{
  if(path.endsWith('/settings'))return {result:{bindings:[{name:'DB',type:'d1',id:databaseId},{name:'BUCKET',type:'r2_bucket',bucket_name:'custom-existing-bucket'}]}};
  if(path.endsWith('/secrets')){
   if(method==='PUT'){assert.equal(events.at(-1),'migrate');savedSecret=body;return {result:{name:body.name,type:body.type}};}
   return {result:savedSecret?[{name:savedSecret.name,type:'secret_text'}]:[]};
  }
  throw new Error('Unexpected route '+path);
 };
 const run=async(args:string[])=>{
  if(args[0]==='deploy'){events.push('deploy');return;}
  assert.deepEqual(args.slice(0,5),['d1','migrations','apply','DB','--remote']);
  const config=JSON.parse(await readFile(args[args.indexOf('--config')+1],'utf8'));
  assert.equal(config.d1_databases[0].database_id,databaseId);
  assert.equal(config.d1_databases[0].database_name,undefined);
  assert.ok(!JSON.stringify(config).includes('FAKE-DEPLOY-TOKEN'));
  events.push('migrate');
 };
 try{
  const args={input:buildsInput(env),api,run,directory,log:()=>{}};
  const result=await deployBuilds(args);
  assert.deepEqual(events,['deploy','migrate']);
  assert.equal(savedSecret.name,'TCG_SECRET_KEY');assert.equal(Buffer.from(savedSecret.text,'base64').length,32);
  assert.deepEqual(result.setupRequired,['TCG_ACCESS_TEAM_DOMAIN','TCG_ACCESS_AUD']);
  const originalKey=savedSecret.text;
  await deployBuilds(args);assert.equal(savedSecret.text,originalKey);
 }finally{await rm(directory,{recursive:true,force:true});}
});

test('native Builds stops before migration when publication fails or DB binding is absent',async()=>{
 const directory=await mkdtemp(join(tmpdir(),'tcg-builds-failure-'));
 try{
  let reads=0;
  await assert.rejects(()=>deployBuilds({input:buildsInput(env),directory,log:()=>{},run:async()=>{throw new Error('Publish failed');},api:async()=>{reads++;}}),/Publish failed/);
  assert.equal(reads,0);
  const commands:string[][]=[];
  await assert.rejects(()=>deployBuilds({input:buildsInput(env),directory,log:()=>{},run:async(args:string[])=>{commands.push(args);},api:async()=>({result:{bindings:[]}})}),/DB/);
  assert.equal(commands.length,1);
 }finally{await rm(directory,{recursive:true,force:true});}
});

test('native Builds does not create a replacement key after a migration error',async()=>{
 const directory=await mkdtemp(join(tmpdir(),'tcg-builds-migration-'));let secretAccess=0;
 try{
  await assert.rejects(()=>deployBuilds({input:buildsInput(env),directory,log:()=>{},run:async(args:string[])=>{if(args[0]==='d1')throw new Error('Migration failed');},api:async(path:string)=>{
   if(path.endsWith('/settings'))return {result:{bindings:[{name:'DB',type:'d1',id:databaseId},{name:'BUCKET',type:'r2_bucket',bucket_name:'uploads'}]}};
   secretAccess++;return {result:[]};
  }}),/Migration failed/);
  assert.equal(secretAccess,0);
 }finally{await rm(directory,{recursive:true,force:true});}
});
