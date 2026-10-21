"""One finite pair. Reuses algorithm code, not a historical schedule; no API inference."""
import json,subprocess,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
old=Path('/home/huanzz/longda-strategy-harness-v6-20261019')
out=root/'runs/compare-v7-partial20';out.mkdir(parents=True,exist_ok=False)
p=json.loads((old/'runs/live-comparison/v6/calls/1/plan.json').read_text())
if 'candidates' not in p:raise ValueError('Expected saved strategy plan')
(out/'strategy.json').write_text(json.dumps(p))
results=[]
for label,base in [('v6',old),('v7',root)]:
    run=out/label
    args=['node','dist/src/cli.js','--orders','configs/orders20.json','--vehicles','1',
          '--out',str(run),'--initial-strategy-file',str(out/'strategy.json'),
          '--model','off','--aux-model','off','--memory','off','--rounds','0',
          '--bootstrap-limit','0','--cp-workers','1','--threads','8','--construct-workers','1',
          '--construct-seconds','8','--wall-seconds','30','--target','1440',
          '--strategy-trials','1','--repair-attempts','0','--stop-after-repair']
    if label=='v7':args+=['--completion-seconds','15']
    start=time.monotonic()
    with (out/(label+'.log')).open('w') as f:
        r=subprocess.run(args,cwd=base,stdout=f,stderr=subprocess.STDOUT,timeout=45)
    d=json.loads((run/'report.json').read_text())
    actual=[]
    for path in (run/'tasks').glob('*/attempt-*/result.json'):
        a=json.loads(path.read_text());actual.append(dict(status=a.get('status'),valid=a.get('valid'),
             partial_handoff=a.get('partial_handoff'),cp_called=a.get('cp_solver_called'),
             hint_source=a.get('hint_source'),hint_values=a.get('hint_values'),
             scheduled=max([x.get('completed_operations',0) for x in a.get('rows',[])]+[0]),
             solver_s=a.get('solver_wall_s')))
    results.append(dict(version=label,wall_s=time.monotonic()-start,first_valid_s=d.get('first_valid_s'),
        day_minutes=(d.get('best') or {}).get('day_minutes'),cp_calls=d.get('cp_solver_calls'),
        reason=d.get('stop_reason'),completion_history=d.get('completion_history'),trials=actual,
        model_calls=d.get('actual_main_model_requests'),exit_code=r.returncode))
(out/'summary.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results))
