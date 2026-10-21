/** Simulated model interruption then explicit resume; real 20-order single-car audit. */
import {readFileSync,writeFileSync,mkdirSync,mkdtempSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {resolve,join} from 'node:path';
import assert from 'node:assert/strict';
import {bootstrap} from '../dist/src/learning.js';
const root=resolve(import.meta.dirname,'..');
const out=process.argv[2]?resolve(process.argv[2]):mkdtempSync('/tmp/longda-repair-resume-');mkdirSync(out,{recursive:true});
const sc=JSON.parse(readFileSync(join(root,'configs/orders20.json'),'utf8'));
const candidate={...bootstrap(sc)[0],engine:'extended',policies:[0],seeds:[0],program_source:'def rank(candidates, state):\n    return [c["start"] if c["kind"] is not None else 0 for c in candidates]'};
const initial=join(out,'initial.json');writeFileSync(initial,JSON.stringify({summary:'Interruption test',candidates:[candidate]}));
const mock=join(out,'mock.mjs');writeFileSync(mock,`
globalThis.fetch=async(_url,init)=>{
 if(process.env.REPAIR_RESUME_PHASE==='timeout')return new Promise((_,reject)=>init.signal.addEventListener('abort',()=>reject(init.signal.reason),{once:true}));
 const p=JSON.parse(JSON.parse(init.body).messages[0].content);
 const c={...p.failed_strategy,engine:'legacy',program_source:null,policies:[3],seeds:[0],name:'resumed-repair',hypothesis:'Explicit builtin fallback after syntax failure',evidence_ids:[p.evidence_id]};
 return new Response('data: '+JSON.stringify({type:'content_block_delta',delta:{type:'text_delta',text:JSON.stringify({summary:'Resumed correction',candidates:[c]})}})+'\\n\\ndata: '+JSON.stringify({type:'message_delta',delta:{stop_reason:'end_turn'}})+'\\n\\n',{headers:{'content-type':'text/event-stream'}});
};`);
const run=join(out,'run');
function invoke(phase,extra){
 const r=spawnSync(process.execPath,['--import',mock,'dist/src/cli.js','--orders','configs/orders20.json','--vehicles','1','--out',run,'--initial-strategy-file',initial,'--bootstrap-limit','0','--model','fixture','--aux-model','off','--memory','off','--rounds','0','--cp-workers','1','--threads','1','--construct-workers','1','--api-timeout','1','--target','1',...extra],{cwd:root,timeout:20000,encoding:'utf8',env:{...process.env,ANTHROPIC_AUTH_TOKEN:'fixture',REPAIR_RESUME_PHASE:phase}});
 writeFileSync(join(out,phase+'.log'),r.stdout+'\n'+r.stderr);assert.equal(r.status,0,r.stdout+r.stderr);
 return JSON.parse(readFileSync(join(run,'report.json'),'utf8'));
}
const first=invoke('timeout',['--repair-attempts','1','--wall-seconds','3']);
assert.equal(first.best,null);assert.equal(first.cp_solver_calls,0);assert.equal(first.repair_calls,1);
const second=invoke('resume',['--resume','--repair-attempts','2','--wall-seconds','10']);
assert.equal(second.best.audit_valid,true);assert.equal(second.cp_solver_calls,0);assert.equal(second.repair_calls,2);
assert.equal(second.repairs[0].calls,2);assert.equal(second.repairs[0].state,'answered');
const summary={first_status:first.stop_reason,second_status:second.stop_reason,day_minutes:second.best.day_minutes,repair_calls:second.repair_calls,elapsed_s:second.elapsed_s,cp_search_calls:second.cp_solver_calls,verified:'Explicit resume reopens exhausted repair only after quota increase; raw failure and lineage retained; complete real audits pass; mocked API only'};
writeFileSync(join(out,'summary.json'),JSON.stringify(summary,null,2));console.log(JSON.stringify(summary,null,2));
