export async function api<T=any>(path:string,body?:unknown,method?:string):Promise<T>{
 const response=await fetch('/api'+path,{method:method??(body===undefined?'GET':'POST'),headers:body instanceof FormData?{}:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:body instanceof FormData?body:JSON.stringify(body)});
 if(!response.ok){let text=`请求失败（${response.status}）`;try{const data=await response.json();text=typeof data.detail==='string'?data.detail:JSON.stringify(data.detail??data);}catch{}throw new Error(text);}
 if(response.status===204)return undefined as T;
 return response.json();
}
export async function downloadArtifact(id:string,layout:string,selected:string[]){
 const params=new URLSearchParams({layout});if(selected.length)params.set('ids',selected.join(','));
 const response=await fetch(`/api/artifacts/${encodeURIComponent(id)}/export?${params}`);
 if(!response.ok){const data=await response.json();throw new Error(data.detail??'导出失败');}
 const blob=await response.blob();const href=URL.createObjectURL(blob);const link=document.createElement('a');link.href=href;link.download='测试用例.xlsx';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(href),1000);
}
export const errText=(error:unknown)=>error instanceof Error?error.message:String(error);
