import {test} from 'node:test';
import assert from 'node:assert/strict';
import {generateKeyPair,exportJWK,SignJWT,createLocalJWKSet} from 'jose';
import {accessIdentity} from '../access';
import {createCloudflareWorker} from '../worker';
import {TestD1,TestR2} from './d1';
import {Store} from '../store';

const env={TCG_ACCESS_TEAM_DOMAIN:'https://example.cloudflareaccess.com',TCG_ACCESS_AUD:'a'.repeat(64)};
async function signing(){
 const {publicKey,privateKey}=await generateKeyPair('RS256');const jwk=await exportJWK(publicKey);jwk.kid='test';
 const keys=createLocalJWKSet({keys:[jwk]});
 const token=(claims:Record<string,any>={})=>new SignJWT({sub:'user-1',iss:env.TCG_ACCESS_TEAM_DOMAIN,aud:env.TCG_ACCESS_AUD,iat:Math.floor(Date.now()/1000),exp:Math.floor(Date.now()/1000)+300,...claims}).setProtectedHeader({alg:'RS256',kid:'test'}).sign(privateKey);
 return {keys,token};
}

test('Access validates signature, issuer, audience, lifetime and subject; spoofed Sites headers have no authority',async()=>{
 const s=await signing();const request=(token:string)=>new Request('https://app.example/api/projects',{headers:{'cf-access-jwt-assertion':token,'oai-authenticated-user-id':'admin'}});
 assert.equal(await accessIdentity(request(await s.token()),env,s.keys),'cloudflare:example.cloudflareaccess.com:user-1');
 for(const claims of [{aud:'another-app'},{iss:'https://other.cloudflareaccess.com'},{exp:1},{exp:undefined},{sub:''},{iat:undefined}])await assert.rejects(()=>s.token(claims).then(t=>accessIdentity(request(t),env,s.keys)));
 const other=await signing();await assert.rejects(()=>other.token().then(t=>accessIdentity(request(t),env,s.keys)));
 await assert.rejects(()=>accessIdentity(new Request('https://app.example',{headers:{'oai-authenticated-user-id':'admin'}}),env,s.keys));
 await assert.rejects(()=>accessIdentity(request('abc'),{},s.keys));
});

test('standalone Worker gates assets and API and scopes records to the verified subject',async()=>{
 const s=await signing();let assetsRead=0;
 const bindings={...env,DB:new TestD1(),BUCKET:new TestR2(),ASSETS:{fetch:async()=>{assetsRead++;return new Response('private app');}}};
 const worker=createCloudflareWorker((request,values)=>accessIdentity(request,values,s.keys));
 const req=(path:string,token?:string)=>new Request('https://app.example'+path,{headers:{'oai-authenticated-user-id':'spoof',...(token?{'cf-access-jwt-assertion':token}:{})}});
 assert.equal((await worker.fetch(req('/'),bindings,{})).status,401);assert.equal(assetsRead,0);
 assert.equal((await worker.fetch(req('/api/projects'),bindings,{})).status,401);
 const token=await s.token();assert.equal((await worker.fetch(req('/'),bindings,{})).status,401);
 assert.equal((await worker.fetch(req('/',token),bindings,{})).status,200);assert.equal(assetsRead,1);
 const response=await worker.fetch(req('/api/projects',token),bindings,{});assert.equal(response.status,200);assert.equal((await response.json() as any[]).length,1);
 assert.equal((await new Store(bindings.DB,'spoof').list('project')).length,0);
 assert.equal((await new Store(bindings.DB,'cloudflare:example.cloudflareaccess.com:user-1').list('project')).length,1);
});
