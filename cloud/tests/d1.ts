import {DatabaseSync} from 'node:sqlite';
import {readFileSync,readdirSync} from 'node:fs';
import {join} from 'node:path';
export class TestD1 {
  db = new DatabaseSync(':memory:');
  constructor(){for(const file of readdirSync('drizzle').filter(f=>f.endsWith('.sql')).sort())this.db.exec(readFileSync(join('drizzle',file),'utf8'));}
  prepare(sql:string){return new Statement(this.db,sql,[]);}
  async batch(statements:Statement[]){this.db.exec('BEGIN');try{const values=statements.map(s=>s.sync());this.db.exec('COMMIT');return values;}catch(e){this.db.exec('ROLLBACK');throw e;}}
}
class Statement {
 constructor(public db:DatabaseSync,public sql:string,public args:any[]){}
 bind(...args:any[]){return new Statement(this.db,this.sql,args);}
 async first(){return this.db.prepare(this.sql).get(...this.args)??null;}
 async all(){return {results:this.db.prepare(this.sql).all(...this.args)};}
 sync(){const r=this.db.prepare(this.sql).run(...this.args);return {success:true,meta:{changes:Number(r.changes),last_row_id:Number(r.lastInsertRowid)}};}
 async run(){return this.sync();}
}
export class TestR2 {
 values=new Map<string,Uint8Array>();
 async put(key:string,value:any){this.values.set(key,new Uint8Array(value instanceof ReadableStream?await new Response(value).arrayBuffer():value));}
 async get(key:string){const data=this.values.get(key);return data?{arrayBuffer:async()=>data.buffer,body:new Response(new Uint8Array(data).buffer).body}:null;}
 async delete(key:string){this.values.delete(key);}
}
