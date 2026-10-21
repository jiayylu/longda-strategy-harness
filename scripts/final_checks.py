"""Short offline continuation/resume checks and read-only release integrity."""
import json,subprocess,sys,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
import worker
from production_state import audit_state

out=ROOT/'runs/final-checks';out.mkdir(parents=True,exist_ok=True)
def launch(case,name,extra):
    dest=out/name
    cmd=['node','dist/src/cli.js','--orders',str(ROOT/'runs/context-cases'/f'{case}.json'),
         '--out',str(dest),'--model','off','--aux-model','off','--memory','off',
         '--cp-workers','0','--construct-workers','1','--threads','1','--bootstrap-limit','8',
         '--construct-seconds','1','--wall-seconds','12',*extra]
    with open(out/(name+('-resume' if '--resume' in extra else '')+'.log'),'w') as f:
        if not (dest/"report.json").exists() or "--resume" in extra:
            subprocess.run(cmd,cwd=ROOT,stdout=f,stderr=subprocess.STDOUT,check=True,timeout=25)
    r=json.load(open(dest/'report.json'))
    assert r['best'] and r['best']['audit_valid'] and not r['errors'],r
    b=dest/'best';sc=json.load(open(b/'scenario.json'));s=json.load(open(b/'best_schedule.json'))
    assert worker.audit(sc,s)['valid'] and worker.verify(sc,s)['valid'] and audit_state(sc,s)['valid']
    return dict(case=case,name=name,first_valid_s=r['first_valid_s'],day_minutes=r['best']['day_minutes'],
        source_hash=r['source_hash'],invocations=r['invocations'],errors=r['errors'],stop=r['stop_reason'])

rows=[]
for case in ['cold8-car2-heating','cold20-car2-day2','cold4-car1-press']:
    rows.append(launch(case,case,['--target','1','--max-trials','8']))
rows.append(launch('cold8-car2-press','resume-wip',['--target','1440','--max-trials','2']))
rows.append(launch('cold8-car2-press','resume-wip',['--target','1440','--max-trials','4','--resume']))
rules=worker.integrity()
prior=ROOT.parent/'longda-strategy-harness-v2-20261015'
old_manifest=json.load(open(prior/'reference-manifest.json'));manifest=json.load(open(ROOT/'reference-manifest.json'))
assert old_manifest==manifest
for rel,sha in manifest.items():
    assert hashlib.sha256((prior/'reference'/rel).read_bytes()).hexdigest()==sha
payload=dict(tests=rows,all_audits_valid=True,reference_files=len(manifest),reference_unchanged=True,rule_integrity=rules)
(out/'report.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
print(json.dumps(payload,ensure_ascii=False))
