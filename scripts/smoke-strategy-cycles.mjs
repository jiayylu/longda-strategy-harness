/** Mock model transport; real constructor, CP-SAT, and immutable full audits. <= 40s. */
import {readFileSync,writeFileSync,mkdirSync,mkdtempSync,readdirSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {resolve,join} from 'node:path';
import assert from 'node:assert/strict';
import {bootstrap} from '../dist/src/learning.js';
const root=resolve(import.meta.dirname,'..');
const out=process.argv[2]?resolve(process.argv[2]):mkdtempSync('/tmp/longda-cycles-');mkdirSync(out,{recursive:true});
const base=JSON.parse(readFileSync(join(root,'configs/orders20.json'),'utf8'));
const strategy={...bootstrap(base)[0],engine:'legacy',policies:[3],seeds:[0],direction:'vehicle_link',fraction:.5,name:'model-cycle-fixture'};
writeFileSync(join(out,'strategy.json'),JSON.stringify(strategy));
const mock=join(out,'mock-model.mjs');
writeFileSync(mock,`
import assert from 'node:assert/strict';
import {readFileSync,appendFileSync} from 'node:fs';
globalThis.fetch=async(_url,init)=>{
 const body=JSON.parse(init.body);
 const payload=JSON.parse(body.messages[0].content);
 const previous=payload.previous_strategy_evaluation;
 if(previous){
   assert.equal(previous.status,'batch_evaluated');
   assert.ok(previous.strategies.every(s=>s.completed_trials>=3 && s.cp_trials>=2));
   assert.ok(previous.strategies.every(s=>s.evidence_ids.length===s.completed_trials));
 }
 appendFileSync(process.env.CYCLE_PAYLOADS,JSON.stringify({at:Date.now(),payload})+'\\n');
 const strategy=JSON.parse(readFileSync(process.env.CYCLE_STRATEGY,'utf8'));
 strategy.seeds=[0];strategy.name=previous?'model-cycle-revised':'model-cycle-first';
 strategy.fraction=previous?.75:.5;
 strategy.hypothesis=previous?'Revise after the evaluated batch; UNKNOWN remains unresolved':'Fixture initial strategy';
 strategy.evidence_ids=previous?previous.strategies.flatMap(s=>s.evidence_ids).slice(0,2):[];
 const counts={};for(const o of payload.scenario.orders)counts[o.route]=(counts[o.route]||0)+1;
 // Keep fixture strategy groups valid if a smaller scenario is passed.
 if(payload.scenario.orders.length!==20)strategy.groups=[Object.entries(counts).map(([route,count])=>({route,count}))];
 return new Response('data: '+JSON.stringify({type:'content_block_delta',delta:{type:'text_delta',text:JSON.stringify({summary:'Mock planner for execution-loop test',candidates:[strategy]})}})+'\\n\\ndata: '+JSON.stringify({type:'message_delta',delta:{stop_reason:'end_turn'}})+'\\n\\n',{headers:{'content-type':'text/event-stream'}});
};
`);
const run=join(out,'two-rounds');
const r=spawnSync(process.execPath,['--import',mock,join(root,'dist/src/cli.js'),
 '--orders','configs/orders20.json','--out',run,'--vehicles','1',
 '--model','fixture','--aux-model','off','--memory','off',
 '--cp-workers','2','--threads','1','--construct-workers','1',
 '--solve-seconds','.2','--strategy-solve-max-seconds','.4',
 '--construct-seconds','3','--strategy-trials','3','--rounds','2',
 '--wall-seconds','25','--api-timeout','5','--target','1440',
 '--bootstrap-limit','0','--require-planner-strategy'],
 {cwd:root,timeout:40000,encoding:'utf8',env:{...process.env,ANTHROPIC_AUTH_TOKEN:'fixture-not-a-real-credential',CYCLE_STRATEGY:join(out,'strategy.json'),CYCLE_PAYLOADS:join(out,'payloads.jsonl')}});
writeFileSync(join(out,'run.log'),r.stdout+'\n'+r.stderr);
assert.equal(r.status,0,r.stdout+r.stderr);
const report=JSON.parse(readFileSync(join(run,'report.json'),'utf8'));
assert.equal(report.planner_delivery.completed_calls,2,JSON.stringify(report.errors));
assert.equal(report.planner_delivery.accepted_candidates,2,JSON.stringify(report.errors));
const first=JSON.parse(readFileSync(join(run,'calls/1/evaluation.json'),'utf8'));
const second=JSON.parse(readFileSync(join(run,'calls/2/request.json'),'utf8'));
assert.deepEqual(second.payload.previous_strategy_evaluation,first);
assert.equal(first.strategies[0].completed_trials,3);
assert.equal(first.strategies[0].cp_trials,2);
assert.ok(report.planner_delivery.evaluated_trials>=6);
assert.equal(report.planner_delivery.evaluated_trials,report.strategy_campaigns.reduce((n,s)=>n+s.completed_trials,0));
const events=readFileSync(join(run,'events.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
const feedback=events.find(e=>e.kind==='strategy_batch_evaluated'&&e.data.round===1);
const secondStart=events.find(e=>e.kind==='model_started'&&e.data.call===2);
assert.ok(feedback.seq<secondStart.seq);
assert.equal(report.repair_calls,0,'Audited baseline must enter improvement, not repair');
const results=report.history.filter(h=>h.strategy_id==='round-1/strategy-1');
assert.ok(results.length>=3);
assert.deepEqual(results.slice(0,3).map(h=>h.id),first.strategies[0].evidence_ids);
assert.ok(results.filter(h=>h.source==='solve').every(h=>h.parent_task_id));
const reqs=readdirSync(join(run,'tasks')).flatMap(t=>readdirSync(join(run,'tasks',t)).map(a=>JSON.parse(readFileSync(join(run,'tasks',t,a,'request.json'),'utf8'))));
assert.ok(reqs.filter(q=>q.action==='solve').length>=4);
assert.ok(reqs.filter(q=>q.action==='solve').every(q=>q.direction==='vehicle_link'&&[.5,.75].includes(q.fraction)),'No silent widening');
assert.ok(report.best?.audit_valid,'Real construction baseline should pass full audits');
const summary={elapsed_s:report.elapsed_s,stop_reason:report.stop_reason,day_minutes:report.best.day_minutes,planner:report.planner_delivery,first_batch:first.strategies.map(s=>({id:s.strategy_id,total:s.completed_trials,cp:s.cp_trials,statuses:s.statuses})),verified:'Two real search batches; second model received first batch feedback; descendants attributed; original full audits; mocked model API only'};
writeFileSync(join(out,'summary.json'),JSON.stringify(summary,null,2));console.log(JSON.stringify(summary,null,2));
