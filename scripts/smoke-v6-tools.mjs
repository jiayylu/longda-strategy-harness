/** Fixed API responses, real tools/constructor/audits. Not an LLM reasoning benchmark. */
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {resolve,join} from 'node:path';
import assert from 'node:assert/strict';
const root=resolve(import.meta.dirname,'..'), out=join(root,'runs/smoke-v6-tools-final');mkdirSync(out,{recursive:true});
const mock=join(out,'mock.mjs');
writeFileSync(mock,`
import {readFileSync} from 'node:fs';
let n=0;
globalThis.fetch=async(_u,init)=>{
 const body=JSON.parse(init.body),p=JSON.parse(body.messages[0].content);
 const source=readFileSync('python/default_algorithm.py','utf8');
 const text=n++<2 ? {summary:'execute and verify duplicate receipt',candidates:[],tool_calls:[{name:'run_algorithm',arguments:{source,seconds:8}}]} : {summary:'saved verified tool result',candidates:[],tool_calls:[]};
 return new Response(JSON.stringify({content:[{type:'text',text:JSON.stringify(text)}],stop_reason:'end_turn'}),{headers:{'content-type':'application/json'}});
};`);
const run=join(out,'run');
const r=spawnSync(process.execPath,['--import',mock,'dist/src/cli.js','--orders','configs/orders20.json','--vehicles','1','--out',run,'--bootstrap-limit','0','--model','fixture','--aux-model','off','--memory','off','--rounds','1','--cp-workers','0','--threads','1','--construct-workers','1','--wall-seconds','20','--target','1440','--api-timeout','15','--repair-attempts','0'],{cwd:root,encoding:'utf8',timeout:30000,env:{...process.env,ANTHROPIC_AUTH_TOKEN:'fixture'}});
writeFileSync(join(out,'run.log'),r.stdout+'\n'+r.stderr);assert.equal(r.status,0,r.stdout+r.stderr);
const report=JSON.parse(readFileSync(join(run,'report.json'),'utf8'));
assert.equal(report.best?.audit_valid,true);
assert.equal(report.errors.length,0,JSON.stringify(report.errors));
assert.equal(report.algorithm_tool_trials.length,1,'receipt replay must not execute or publish twice');
assert.equal(report.strategy_campaigns.filter(c=>c.strategy_id.startsWith('tool-round-')).length,1);
const trials=report.strategy_campaigns.filter(c=>c.strategy_id.startsWith('tool-round-'))[0];assert.equal(trials.completed_trials,1);
const events=readFileSync(join(run,'events.jsonl'),'utf8').trim().split('\n').map(JSON.parse);assert.ok(events.some(e=>e.kind==='duplicate_skipped'));
const summary={valid:true,day_minutes:report.best.day_minutes,total_s:report.elapsed_s,actual_executions:report.algorithm_tool_trials.length,receipt_replayed:true,attributed_trials:trials.completed_trials,model:'fixture, not real inference'};
writeFileSync(join(out,'summary.json'),JSON.stringify(summary,null,2));console.log(JSON.stringify(summary));
