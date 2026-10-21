/** Recorded model responses; real 20-order single-car constructors and both audits. */
import {readFileSync,writeFileSync,mkdirSync,mkdtempSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {resolve,join} from 'node:path';
import assert from 'node:assert/strict';
import {bootstrap} from '../dist/src/learning.js';
const root=resolve(import.meta.dirname,'..');
const out=process.argv[2]?resolve(process.argv[2]):mkdtempSync('/tmp/longda-repair-');
mkdirSync(out,{recursive:true});
const sc=JSON.parse(readFileSync(join(root,'configs/orders20.json'),'utf8'));
const strategy={...bootstrap(sc)[0],policies:[0],seeds:[0],engine:'extended',backtrack_budget:0,
 name:'unsupported-code-fixture',program_source:'def rank(candidates, state):\n    return [c["start"] if c["kind"] is not None else 0 for c in candidates]'};
const input=join(out,'initial.json');
writeFileSync(input,JSON.stringify({summary:'Explicit failure fixture; not a timetable',candidates:[strategy]}));
const mock=join(out,'mock-model.mjs');
writeFileSync(mock,`
import assert from 'node:assert/strict';
import {appendFileSync} from 'node:fs';
let calls=0;
globalThis.fetch=async(_url,init)=>{
 const body=JSON.parse(init.body),p=JSON.parse(body.messages[0].content);
 assert.equal(p.operation,'repair_construction');
 assert.equal(p.execution.cp_after_failed_constructor,false);
 calls++;
 if(calls===1){assert.equal(p.failure.kind,'program_error');assert.match(p.failure.message,/IsNot/);}
 else {assert.equal(p.failure.kind,'construction_deadend');assert.ok(p.failure.details.failure_context);}
 const c={...p.failed_strategy,name:'revision-'+calls,evidence_ids:[p.evidence_id],
    program_source:null,engine:calls===1?'extended':'legacy',policies:[calls===1?0:3],seeds:[0],
    hypothesis:calls===1?'Remove unsupported syntax and try builtin ranking':'Observed rolling/backtracking deadend: use the cold-start batch constructor as a different legal construction method'};
 appendFileSync(process.env.REPAIR_PAYLOADS,JSON.stringify({payload:p,candidate:c})+'\\n');
 return new Response('data: '+JSON.stringify({type:'content_block_delta',delta:{type:'text_delta',text:JSON.stringify({summary:'Recorded tool-error repair response',candidates:[c]})}})+'\\n\\ndata: '+JSON.stringify({type:'message_delta',delta:{stop_reason:'end_turn'}})+'\\n\\n',{headers:{'content-type':'text/event-stream'}});
};`);
const run=join(out,'run');
const r=spawnSync(process.execPath,['--import',mock,'dist/src/cli.js','--orders','configs/orders20.json',
 '--vehicles','1','--out',run,'--initial-strategy-file',input,'--bootstrap-limit','0',
 '--model','fixture','--aux-model','off','--memory','off','--rounds','0','--repair-attempts','3',
 '--construct-workers','1','--cp-workers','1','--threads','1','--construct-seconds','2',
 '--wall-seconds','30','--target','1'],
 {cwd:root,timeout:45000,encoding:'utf8',env:{...process.env,ANTHROPIC_AUTH_TOKEN:'test-fixture',REPAIR_PAYLOADS:join(out,'payloads.jsonl')}});
writeFileSync(join(out,'run.log'),r.stdout+'\n'+r.stderr);
assert.equal(r.status,0,r.stdout+r.stderr);
const report=JSON.parse(readFileSync(join(run,'report.json'),'utf8'));
assert.equal(report.repair_calls,2,JSON.stringify({errors:report.errors,repairs:report.repairs.map(r=>({state:r.state,kind:r.failure.kind}))}));
assert.equal(report.best?.audit_valid,true);
assert.equal(report.cp_solver_calls,0,'No CP search should be scheduled after either failed construction');
assert.equal(report.errors.length,0,JSON.stringify(report.errors));
assert.equal(report.repair_evaluations[0].valid,false);
assert.equal(report.repair_evaluations[1].valid,true);
const ev=readFileSync(join(run,'events.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
const results=ev.filter(e=>e.kind==='tool_result');
assert.equal(results.length,3);
assert.equal(results[0].data.status,'PROGRAM_ERROR');
assert.equal(results[1].data.status,'CONSTRUCTION_DEADEND');
assert.equal(results[2].data.status,'AUDITED');
const summary={test:'recorded-model-two-repairs',elapsed_s:report.elapsed_s,day_minutes:report.best.day_minutes,
 repair_calls:report.repair_calls,cp_solver_calls:report.cp_solver_calls,hard_audits:true,
 note:'Mocked model responses, real constructor and complete audits; not evidence of real model reasoning'};
writeFileSync(join(out,'summary.json'),JSON.stringify(summary,null,2));
console.log(JSON.stringify(summary,null,2));
