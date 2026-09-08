import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {spawn} from 'node:child_process';
import {randomBytes} from 'node:crypto';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const appId='cliuxinxin/asexamp';
function required(env,name){const value=env[name]?.trim();if(!value)throw new Error(`缺少 ${name}，尚未执行任何部署。请在运行环境或 GitHub 部署配置中设置。`);return value;}
export function deploymentInput(env){
 const token=required(env,'CLOUDFLARE_API_TOKEN'),account=required(env,'CLOUDFLARE_ACCOUNT_ID');
 const team=required(env,'TCG_ACCESS_TEAM_DOMAIN').replace(/\/$/,''),audience=required(env,'TCG_ACCESS_AUD');
 const name=env.TCG_WORKER_NAME||'asexamp';
 const databaseId=env.TCG_D1_DATABASE_ID?.trim(),bucketName=env.TCG_R2_BUCKET_NAME?.trim();
 if(!/^[a-f0-9]{32}$/i.test(account))throw new Error('Cloudflare Account ID 格式无效');
 if(!/^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/.test(team)||!/^[a-f0-9]{64}$/i.test(audience))throw new Error('Cloudflare Access 团队地址或 AUD 格式无效');
 if(!/^[a-z0-9](?:[a-z0-9-]{0,50}[a-z0-9])?$/.test(name))throw new Error('Worker 名称格式无效');
 if(databaseId&&!/^[a-f0-9-]{36}$/i.test(databaseId))throw new Error('D1 数据库 ID 格式无效');
 if(bucketName&&!/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/.test(bucketName))throw new Error('R2 存储桶名称格式无效');
 return {token,account,team,audience,name,databaseId,bucketName};
}

export function cloudflareApi(input,transport=fetch){
 return async function api(path,method='GET',body,allow404=false){
  const response=await transport(`https://api.cloudflare.com/client/v4/accounts/${input.account}${path}`,{method,headers:{Authorization:'Bearer '+input.token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(60000),redirect:'error'});
  if(allow404&&response.status===404)return null;
  const value=await response.json().catch(()=>null);
  if(!response.ok||value?.success!==true){
   const codes=(value?.errors??[]).map(e=>Number(e.code)).filter(Number.isFinite).join(',');
   throw new Error(`Cloudflare ${method} ${path.split('?')[0]} 失败：HTTP ${response.status}${codes?'，错误码 '+codes:''}。请检查账号权限、R2 是否已启用及资源状态。`);
  }
  return value;
 };
}

export async function runWrangler(args){
 await new Promise((resolveRun,reject)=>{
  const child=spawn(process.execPath,[resolve(root,'node_modules/wrangler/bin/wrangler.js'),...args],{cwd:root,env:{...process.env,CI:'true',WRANGLER_SEND_METRICS:'false'},stdio:'inherit',shell:false});
  child.on('error',reject);child.on('exit',code=>code===0?resolveRun():reject(new Error(`Wrangler 执行失败（${code}），未报告部署成功`)));
 });
}

export async function provision(input,api,base){
 const workerPath='/workers/scripts/'+encodeURIComponent(input.name);
 const existing=await api(workerPath+'/settings','GET',undefined,true);
 if(existing&&!existing.result.bindings?.some(b=>b.name==='TCG_APP_ID'&&b.text===appId))throw new Error('同名 Worker 已存在且不属于此项目，未覆盖。请设置 TCG_WORKER_NAME 使用新名称。');
 const bindings=existing?.result.bindings??[];
 const boundDatabase=bindings.find(b=>b.name==='DB'&&b.type==='d1')?.id;
 const boundBucket=bindings.find(b=>b.name==='BUCKET'&&b.type==='r2_bucket')?.bucket_name;
 const selectedDatabase=input.databaseId||boundDatabase;
 const selectedBucket=input.bucketName||boundBucket;
 const databases=[];
 for(let page=1;;page++){
  const result=await api('/d1/database?per_page=100&page='+page);databases.push(...result.result);
  if(result.result.length<100||page>=result.result_info?.total_pages)break;
 }
 const databaseName=input.name+'-db';
 let database=databases.find(d=>selectedDatabase?d.uuid===selectedDatabase:d.name===databaseName);
 if(selectedDatabase&&!database)throw new Error('指定或原绑定的 D1 数据库不存在，未创建替代数据库');
 if(database&&!selectedDatabase)throw new Error('同名 D1 数据库归属未确认，未修改。请换 Worker 名称，或确认资源后设置 TCG_D1_DATABASE_ID。');
 const bucketName=selectedBucket||input.name+'-uploads';
 const bucket=await api('/r2/buckets/'+encodeURIComponent(bucketName),'GET',undefined,true);
 if(selectedBucket&&!bucket)throw new Error('指定或原绑定的 R2 存储桶不存在，未创建替代存储桶');
 if(bucket&&!selectedBucket)throw new Error('同名 R2 存储桶归属未确认，未修改。请换 Worker 名称，或确认资源后设置 TCG_R2_BUCKET_NAME。');
 // Check both collisions before creating or migrating either resource.
 if(!database)database=(await api('/d1/database','POST',{name:databaseName})).result;
 if(!database?.uuid)throw new Error('Cloudflare 未返回数据库 ID，部署中止');
 if(!bucket)await api('/r2/buckets','POST',{name:bucketName});
 const config={...base,name:input.name,account_id:input.account,vars:{TCG_APP_ID:appId,TCG_ACCESS_TEAM_DOMAIN:input.team,TCG_ACCESS_AUD:input.audience},
  main:resolve(root,base.main),build:{command:'npm run build:cloudflare',cwd:root},assets:{...base.assets,directory:resolve(root,base.assets.directory)},
  d1_databases:[{binding:'DB',database_name:database.name,database_id:database.uuid,migrations_dir:resolve(root,'drizzle')}],r2_buckets:[{binding:'BUCKET',bucket_name:bucketName}]};
 return {config,workerPath};
}

export async function deploy(){
 const input=deploymentInput(process.env),api=cloudflareApi(input);
 console.log('检查 Cloudflare 账号资源并准备独立部署…');
 const base=JSON.parse(await readFile(resolve(root,'wrangler.jsonc'),'utf8'));
 const {config,workerPath}=await provision(input,api,base);
 const directory=resolve(root,'.cloudflare'),configPath=resolve(directory,'wrangler.json');
 await mkdir(directory,{recursive:true});await writeFile(configPath,JSON.stringify(config,null,2)+'\n');
 console.log('应用数据库迁移…');await runWrangler(['d1','migrations','apply','DB','--remote','--config',configPath]);
 console.log('发布到你的 Cloudflare 账号…');await runWrangler(['deploy','--config',configPath]);
 const secrets=(await api(workerPath+'/secrets')).result;
 if(!secrets.some(s=>s.name==='TCG_SECRET_KEY')){
  // Preserve an existing encryption key across every later deployment.
  await api(workerPath+'/secrets','PUT',{name:'TCG_SECRET_KEY',type:'secret_text',text:randomBytes(32).toString('base64')});
 }
 const domain=(await api('/workers/subdomain')).result?.subdomain;
 if(!domain||!/^[a-z0-9-]+$/.test(domain))throw new Error('Worker 已发布，但尚未确认 workers.dev 地址，请检查 Cloudflare 控制台');
 console.log(`Cloudflare 发布完成：https://${input.name}.${domain}.workers.dev`);
 console.log('请确认此域名已由对应的 Cloudflare Access 应用保护，再登录并测试模型。');
}

if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
 deploy().catch(error=>{console.error(error.message);process.exitCode=1;});
}
