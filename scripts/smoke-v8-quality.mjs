/** Deterministic API fixture + REAL 20-order construction. This is not an LLM effectiveness measurement. */
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {resolve,join} from 'node:path';
import assert from 'node:assert/strict';
const root=resolve(import.meta.dirname,'..'),out=join(root,'runs/smoke-v8-quality-v2');mkdirSync(out,{recursive:true});
const candidate=JSON.parse(readFileSync(join(root,'configs/quality-feedback-regression.json'),'utf8'));
const runDir=join(out,'run'),mock=join(out,'mock.mjs');
writeFileSync(mock,`
import {readFileSync,existsSync,appendFileSync} from 'node:fs';
let n=0;
globalThis.fetch=async(_,init)=>{
 const body=JSON.parse(String(init.body));
 const txt=JSON.stringify(body);
 appendFileSync(${JSON.stringify(join(out,'requests.jsonl'))},JSON.stringify(body)+'\\n');

 const c=${JSON.stringify(candidate)};
 if(txt.includes('repair_construction')){
  c.name='quality-revised-grouping';c.hypothesis='Repair actual legal low-score feedback by comparing larger waves and different policies; outcome unproven.';
  c.algorithm_source=c.algorithm_source.replace('build_waves(routes, 4)','build_waves(routes, 5)').replace('policies = [1, 3, 0]','policies = [5, 4, 0]');
 }
 return new Response(JSON.stringify({content:[{type:'text',text:JSON.stringify({summary:'fixture policy, real construction',candidates:[c],tool_calls:[]})}],stop_reason:'end_turn'}),{headers:{'content-type':'application/json'}});
};`);
const args=['--orders','configs/orders20.json','--vehicles','2','--out',runDir,'--model','fixture','--aux-model','off','--memory','off','--rounds','0','--cp-workers','1','--threads','2','--construct-workers','1','--construct-seconds','8','--solve-seconds','5','--completion-seconds','5','--wall-seconds','90','--target','1140','--repair-attempts','1','--strategy-trials','1','--stop-after-repair','--bootstrap-limit','0','--initial-strategy-file','configs/quality-feedback-pair.json'];
const r=spawnSync(process.execPath,['--import',mock,'dist/src/cli.js',...args],{cwd:root,encoding:'utf8',timeout:110000,env:{...process.env,ANTHROPIC_AUTH_TOKEN:'fixture'}});
writeFileSync(join(out,'run.log'),r.stdout+'\n'+r.stderr);assert.equal(r.status,0,r.stdout+r.stderr);
const report=JSON.parse(readFileSync(join(runDir,'report.json'),'utf8'));
const quality=report.quality_feedback?.filter(t=>t.strategy_id)||[];
assert.ok(quality.length,'a real model-strategy construction must trigger quality feedback');
assert.ok(report.repair_calls>=1,'quality must call repair');
assert.ok(report.repair_evaluations?.length>=1,'repair must execute');
for(const q of quality)assert.equal(report.history.filter(h=>h.strategy_id===q.strategy_id&&h.source==='solve'&&h.purpose!=='partial_complete').length,0,'poor strategy must not enter dependent improvement CP');
const summary={fixture:true,real_orders:20,vehicles:2,quality_feedback:quality.map(x=>x.details),repair_calls:report.repair_calls,repair_evaluations:report.repair_evaluations,best_minutes:report.best?.day_minutes,elapsed_s:report.elapsed_s};
writeFileSync(join(out,'summary.json'),JSON.stringify(summary,null,2));console.log(JSON.stringify(summary));
