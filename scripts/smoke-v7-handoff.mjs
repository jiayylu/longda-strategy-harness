/** Finite integration tests. Fixture model responses; actual CP search and audits. */
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {resolve,join} from 'node:path';
import assert from 'node:assert/strict';
const root=resolve(import.meta.dirname,'..'), out=join(root,'runs/smoke-v7-handoff');mkdirSync(out,{recursive:true});
const sc=JSON.parse(readFileSync(join(root,'configs/orders20.json'),'utf8'));sc.transport.vehicle_count=1;
sc.orders=[{id:'A',route:'R',release_minutes:0,due_minutes:5760}];
sc.routes=[{id:'R',operations:[1,2].map(fire=>({fire,temp:1150,hold_min:60,hold_max:90,forge:10}))}];
writeFileSync(join(out,'scenario.json'),JSON.stringify(sc));
const source='def construct():\n state=root()\n for step in range(2):\n  choices=candidates(state)\n  if not choices:return None\n  state=advance(state,choices[0]["id"])\n return None\n';
const strategy={name:'partial-test',hypothesis:'Stop after first forge to exercise mutable-hint completion',groups:[[{route:'R',count:1}]],loading:'grouped',policies:[0],seeds:[0],direction:'broad',fraction:1,engine:'calendar',algorithm_source:source};
writeFileSync(join(out,'strategy.json'),JSON.stringify({summary:'partial handoff regression',candidates:[strategy]}));
const args=['--orders',join(out,'scenario.json'),'--vehicles','1','--bootstrap-limit','0','--aux-model','off','--memory','off','--rounds','0','--cp-workers','1','--threads','2','--construct-workers','1','--construct-seconds','3','--completion-seconds','5','--wall-seconds','20','--target','1','--repair-attempts','0'];
const results=[];
for(const mode of ['off','on','tool']){
 const dir=join(out,mode);let pre=[],cmd=[...args,'--out',dir];
 if(mode==='tool'){
  const mock=join(out,'mock.mjs');writeFileSync(mock,`let count=0;globalThis.fetch=async()=>new Response(JSON.stringify({content:[{type:'text',text:JSON.stringify(count++===0?{summary:'run partial',candidates:[],tool_calls:[{name:'run_algorithm',arguments:{source:${JSON.stringify(source)},seconds:3}}]}:{summary:'completion is queued',candidates:[],tool_calls:[]})}],stop_reason:'end_turn'}),{headers:{'content-type':'application/json'}});`);
  pre=['--import',mock];cmd[cmd.indexOf('--rounds')+1]='1';cmd.push('--model','fixture');
 }else cmd.push('--model','off','--partial-completion',mode,'--initial-strategy-file',join(out,'strategy.json'));
 const started=performance.now();const run=spawnSync(process.execPath,[...pre,'dist/src/cli.js',...cmd],{cwd:root,encoding:'utf8',timeout:30000,env:{...process.env,ANTHROPIC_AUTH_TOKEN:'fixture'}});
 writeFileSync(join(out,`${mode}.log`),run.stdout+'\n'+run.stderr);assert.equal(run.status,0,run.stdout+run.stderr);
 const r=JSON.parse(readFileSync(join(dir,'report.json'),'utf8'));
 if(mode==='off'){assert.equal(r.best,null);assert.equal(r.cp_solver_calls,0);}
 else{assert.equal(r.best?.audit_valid,true,JSON.stringify(r.errors));assert.equal(r.completion_attempts,1);assert.equal(r.completion_valid,1);assert.equal(r.completion_history[0].hint_source,'current_partial');assert.equal(r.optimal,false);}
 results.push({mode,wall_s:(performance.now()-started)/1000,first_valid_s:r.first_valid_s,day_minutes:r.best?.day_minutes??null,completion_attempts:r.completion_attempts,completion_valid:r.completion_valid,cp_calls:r.cp_solver_calls,reason:r.stop_reason,model:'fixture/off; no live inference'});
}
writeFileSync(join(out,'summary.json'),JSON.stringify(results,null,2));console.log(JSON.stringify(results));
