// Operates only on the explicit v11 copy, never the source database.
import {Memory} from '../dist/src/memory.js';
import {DatabaseSync} from 'node:sqlite';
import {writeFileSync,readFileSync,existsSync} from 'node:fs';
const file=process.argv[2]??'data/experience-v11.sqlite';
if(!file.includes('v11')) throw Error('Explicit v11 database path required');
const migrated=new Memory(file);migrated.close();
const db=new DatabaseSync(file);
const reports=[];
function restore(id,stage,recommendation,predicate){
 const old=db.prepare('SELECT * FROM cards WHERE id=?').get(id);if(!old)throw Error('Missing '+id);
 const prior=JSON.parse(old.body);
 const ids=prior.evidence_ids.filter(eid=>{
  const r=db.prepare('SELECT * FROM experiences WHERE id=?').get(eid);return r && r.stage===stage && predicate(JSON.parse(r.outcome));
 });
 if(!ids.length)throw Error('No matching audited evidence '+id);
 const exceptions=["Observed only on the cited order and equipment state; no causal or cross-scenario guarantee.", "Further successes or failures must match this execution stage. UNKNOWN is not proof of infeasibility."];
 const conditions=stage==='construct' ? prior.conditions.filter(x=>!/(cp_direction|fraction)/.test(x)) : prior.conditions;
 if(old.retired===0 && old.review_state==='verified_scope' && prior.migration_v11 && JSON.stringify(conditions)===JSON.stringify(prior.conditions) && JSON.stringify(exceptions)===JSON.stringify(prior.exceptions)) return;
 const evidence=ids.map(eid=>JSON.parse(db.prepare('SELECT outcome FROM experiences WHERE id=?').get(eid).outcome));
 const body={...prior,stage,conditions,exceptions,recommendation,evidence_ids:ids,retirement:null,
  migration_v11:{reason:'Evidence review: restore/qualify only the executed stage supported by original audited results.',previous_version:old.version,previous_retired:!!old.retired,source_card:id},
  attribution:{status:'observational',note:'Not proof of causality or cross-scenario benefit.'},
  measured:{trials:evidence.length,valid:evidence.filter(x=>x.valid&&x.audit_valid).length,gain_minutes:evidence.reduce((a,x)=>a+(x.gain_minutes??0),0),cost:evidence.reduce((a,x)=>a+(x.cost??0),0)},updated_at:new Date().toISOString()};
 const version=old.version+1;
 db.prepare("UPDATE cards SET retired=0,version=?,stage=?,review_state='verified_scope',body=? WHERE id=?").run(version,stage,JSON.stringify(body),id);
 db.prepare('INSERT INTO card_history VALUES(?,?,?)').run(id,version,JSON.stringify({...body,op:'restore_reviewed',expected_version:old.version}));
 reports.push({id,previous_version:old.version,version,was_retired:!!old.retired,stage,evidence_ids:ids,measured:body.measured});
}
db.exec('BEGIN IMMEDIATE');
try{
 restore('review-v9-full-search','cp_improve','Keep a limited full-model CP improvement lane. An audited 854 to 858 minute gain used furnace_batch with all 20 orders released. This is one observed improvement from that incumbent, not evidence that a constructor is better.',x=>x.valid&&x.audit_valid&&x.day_minutes===858&&x.baseline_before_minutes===854);
 restore('review-v9-press-local','cp_improve','Consider press-gap CP neighborhoods releasing about 8 of 20 orders. Eight audited CP improvements in the referenced run totaled 325 minutes. Preserve baseline and actual released set; this is observational, not constructor or universal speedup evidence.',x=>x.valid&&x.audit_valid&&x.gain_minutes>0);
 restore('card-legacy-broad-same-cold-start-robust-fallback-v3','construct','Observed audited complete constructions with legacy mixed batches, WIP 4/5, same vehicle policy on this input. Consider as first-feasibility fallback; cited day scores are not CP improvements and do not establish all policies/seeds work.',x=>x.valid&&x.audit_valid);
 db.exec('COMMIT');
}catch(e){db.exec('ROLLBACK');throw e;}
const previous=existsSync("verification/memory-migration-v11.json")?JSON.parse(readFileSync("verification/memory-migration-v11.json","utf8")):{changes:[]};
const result={schema:4,changes:[...previous.changes,...reports],counts:db.prepare('SELECT stage,count(*) AS n FROM experiences GROUP BY stage').all(),cards:db.prepare('SELECT retired,review_state,count(*) AS n FROM cards GROUP BY retired,review_state').all(),legacy_policy:'Unreviewed legacy summaries are retained but not auto-supplied; raw evidence remains available by stage.'};
writeFileSync('verification/memory-migration-v11.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));db.close();
