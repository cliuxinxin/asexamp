import {createRemoteJWKSet,jwtVerify,type JWTVerifyGetKey} from 'jose';
import {DomainError,type Json} from './domain';

const certificates=new Map<string,JWTVerifyGetKey>();
export function accessConfiguration(env:Json){
 const team=String(env.TCG_ACCESS_TEAM_DOMAIN??'').replace(/\/$/,'');
 const audience=String(env.TCG_ACCESS_AUD??'');
 if(!/^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/.test(team)||!audience||audience.length>200){
  throw new DomainError('请先配置此 Worker 的 Cloudflare Access 登录保护',503);
 }
 return {team,audience};
}

export async function accessIdentity(request:Request,env:Json,verificationKeys?:JWTVerifyGetKey){
 const {team,audience}=accessConfiguration(env);
 const token=request.headers.get('cf-access-jwt-assertion');
 if(!token||token.length>16384)throw new DomainError('请通过 Cloudflare Access 登录后访问',401);
 let keys=verificationKeys??certificates.get(team);
 if(!keys){
  keys=createRemoteJWKSet(new URL(team+'/cdn-cgi/access/certs'));
  if(certificates.size>=4)certificates.delete(certificates.keys().next().value!);
  certificates.set(team,keys);
 }
 try{
  const {payload}=await jwtVerify(token,keys,{issuer:team,audience,algorithms:['RS256'],requiredClaims:['sub','exp','iat'],clockTolerance:5});
  if(typeof payload.sub!=='string'||!payload.sub||payload.sub.length>200)throw new Error('Invalid subject');
  return `cloudflare:${new URL(team).hostname}:${encodeURIComponent(payload.sub)}`;
 }catch{throw new DomainError('Cloudflare 登录已失效或不属于此应用，请重新登录',401);}
}
