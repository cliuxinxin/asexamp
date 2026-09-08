import {createHandler} from './server';
import {accessIdentity} from './access';
import {DomainError,type Json} from './domain';

export function createCloudflareWorker(identify=accessIdentity,handle=createHandler()){
 return {async fetch(request:Request,env:Json,ctx:any){
  try{
   const owner=await identify(request,env);
   // Replace the client-supplied header only after verifying the Access JWT.
   const headers=new Headers(request.headers);
   headers.set('oai-authenticated-user-id',owner);
   return await handle(new Request(request,{headers}),env,ctx);
  }catch(e){
   return Response.json({detail:e instanceof DomainError?e.message:'登录验证暂时不可用，请稍后重试'},{status:e instanceof DomainError?e.status:503,headers:{'Cache-Control':'no-store'}});
  }
 }};
}

export default createCloudflareWorker();
