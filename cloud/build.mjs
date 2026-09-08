import {build as bundle} from 'esbuild';
import {build as vite} from 'vite';
import react from '@vitejs/plugin-react';
import {mkdir,copyFile,rm} from 'node:fs/promises';
import {resolve} from 'node:path';

const target=process.argv.includes('--cloudflare')?'cloudflare':'sites';
await rm('dist',{recursive:true,force:true});
await mkdir('dist/server',{recursive:true});
await bundle({entryPoints:[target==='cloudflare'?'cloud/worker.ts':'cloud/server.ts'],outfile:'dist/server/index.js',bundle:true,format:'esm',platform:'neutral',target:'es2022',conditions:['workerd','worker','browser','import','default'],mainFields:['browser','module','main'],external:['node:*','cloudflare:*'],define:{'process.env.NODE_ENV':'"production"'},minify:true,legalComments:'eof'});
await vite({root:resolve('frontend'),configFile:false,plugins:[react()],define:{'import.meta.env.VITE_RUNTIME':'"cloud"'},build:{outDir:resolve('dist/client'),emptyOutDir:true}});
if(target==='sites'){
 await mkdir('dist/.openai',{recursive:true});
 await copyFile('.openai/hosting.json','dist/.openai/hosting.json');
}
