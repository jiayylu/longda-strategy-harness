import {DatabaseSync} from 'node:sqlite';
import {Memory} from '../dist/src/memory.js';
import {ModelClient} from '../dist/src/client.js';
import {Events,save} from '../dist/src/events.js';
import {updateExperience} from '../dist/src/auxiliary_roles.js';
const out=process.argv[2]??'runs/updater-v11';
const db=new Memory(out+'/memory-v11.sqlite');
const sql=new DatabaseSync(out+'/memory-v11.sqlite',{readOnly:true});
const r=sql.prepare('SELECT profile FROM experiences LIMIT 1').get();const p=JSON.parse(r.profile);sql.close();
const cards=db.cards(p.compatible,p.state);
const client=new ModelClient('https://dashscope.aliyuncs.com/apps/anthropic','qwen3.7-plus',process.env.ANTHROPIC_AUTH_TOKEN,'off',4096,undefined,'memory_updater');
const events=new Events(out+'/events.jsonl','updater-acceptance',performance.now());
const result=await updateExperience(client,db.facts(p.compatible,12).reverse(),cards,p.state,db,p.compatible,true,events,new AbortController().signal,35,out+'/update.json');
const report={status:result.status,model:result.model,changes:result.changes,rejected:result.rejected,error:result.error,wall_s:result.wall_s,
 full_search_active:db.cards(p.compatible,p.state).some(c=>c.id==='review-v9-full-search')};
save('verification/updater-live-v11.json',report);console.log(JSON.stringify(report));db.close();
