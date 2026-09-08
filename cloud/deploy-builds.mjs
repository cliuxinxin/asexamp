import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {randomBytes} from 'node:crypto';
import {cloudflareApi,runWrangler} from './deploy.mjs';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');

export function buildsInput(env){
 const token=(env.CLOUDFLARE_API_TOKEN||env.CF_API_TOKEN)?.trim();
 const account=(env.CLOUDFLARE_ACCOUNT_ID||env.CF_ACCOUNT_ID)?.trim();
 if(!token)throw new Error('未找到 CLOUDFLARE_API_TOKEN。请在 Cloudflare Builds 的 API token 设置中选择发布 Token。');
 if(!account||!/^[a-f0-9]{32}$/i.test(account))throw new Error('请在 Cloudflare 构建变量中设置有效的 CLOUDFLARE_ACCOUNT_ID（32 位 Account ID）。');
 return {token,account,workerName:env.WRANGLER_CI_OVERRIDE_NAME};
}

export async function deployBuilds({input=buildsInput(process.env),api=cloudflareApi(input),run=runWrangler,directory=resolve(root,'.cloudflare'),log=console.log}={}){
 const base=JSON.parse(await readFile(resolve(root,'wrangler.jsonc'),'utf8'));
 if(!/^[a-z0-9][a-z0-9-]*$/.test(base.name))throw new Error('Worker 名称无效');
 if(input.workerName&&input.workerName!==base.name)throw new Error('Cloudflare 项目名称与 wrangler.jsonc 名称不一致；请使用 '+base.name+'。尚未发布或修改资源。');
 // Wrangler provisions resources and checks the target selected by Workers Builds.
 // Access runtime variables can be added after first publication; the Worker fails closed.
 log('发布 Worker，并由 Wrangler 配置 D1/R2 绑定…');
 await run(['deploy','--config',resolve(root,'wrangler.jsonc')]);
 const workerPath='/workers/scripts/'+base.name;
 const bindings=(await api(workerPath+'/settings')).result?.bindings??[];
 const database=bindings.find(b=>b.name==='DB'&&b.type==='d1');
 const bucket=bindings.find(b=>b.name==='BUCKET'&&b.type==='r2_bucket');
 if(!database?.id||!/^[a-f0-9-]{36}$/i.test(database.id))throw new Error('Worker 已发布，但未获得有效的 DB 绑定；未执行数据库迁移。请检查 D1 权限和绑定。');
 if(!bucket?.bucket_name)throw new Error('Worker 已发布，但 BUCKET 绑定缺失；请检查 R2 是否已启用。');
 if(bindings.some(b=>b.name==='TCG_SECRET_KEY'&&b.type!=='secret_text'))throw new Error('TCG_SECRET_KEY 必须保存为 Worker Secret；请保留原值并修正类型，未生成替代密钥。');
 // Use the actual deployed UUID, never a guessed resource name from a fresh checkout.
 const migrationConfig={name:base.name,account_id:input.account,compatibility_date:base.compatibility_date,send_metrics:false,
  d1_databases:[{binding:'DB',database_id:database.id,migrations_dir:resolve(root,'drizzle')}]};
 await mkdir(directory,{recursive:true});
 const configPath=resolve(directory,'builds-migrations.json');
 await writeFile(configPath,JSON.stringify(migrationConfig,null,2)+'\n');
 log('初始化或更新已绑定的数据库…');
 await run(['d1','migrations','apply','DB','--remote','--config',configPath]);
 const secrets=(await api(workerPath+'/secrets')).result;
 if(!Array.isArray(secrets))throw new Error('无法确认现有 Worker Secrets，未生成替代密钥');
 if(!secrets.some(s=>s.name==='TCG_SECRET_KEY')){
  await api(workerPath+'/secrets','PUT',{name:'TCG_SECRET_KEY',type:'secret_text',text:randomBytes(32).toString('base64')});
 }
 const setupRequired=['TCG_ACCESS_TEAM_DOMAIN','TCG_ACCESS_AUD'].filter(name=>!bindings.some(b=>b.name===name&&(b.type==='secret_text'||typeof b.text==='string'&&b.text.length>0)));
 log('Worker 发布、数据库迁移和加密密钥初始化完成。');
 if(setupRequired.length)log('首次登录前，请在 Worker 的 Settings → Variables and Secrets 中添加：'+setupRequired.join('、')+'。配置前应用会拒绝访问。');
 log('请通过已启用 Cloudflare Access 的应用地址登录并测试模型。');
 return {setupRequired};
}

if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
 deployBuilds().catch(error=>{console.error(error.message);process.exitCode=1;});
}
