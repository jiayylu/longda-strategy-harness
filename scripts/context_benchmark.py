"""Bounded end-to-end comparisons. No persistent/background search is started."""
import argparse,getpass,json,os,subprocess,time,sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def run(name,case,db,*,model='off',memory='read-only',seconds=25,trials=10,target=1440,bootstrap=8,aux='off',rounds=1,thinking_budget=0,cp_workers=0,solve_seconds=15):
 out=ROOT/'runs/context-benchmark-final'/name
 cmd=['node','dist/src/cli.js','--orders',str(ROOT/'runs/context-cases'/f'{case}.json'),'--out',str(out),
      '--model',model,'--aux-model',aux,'--memory',memory,'--memory-db',str(db),'--rounds',str(rounds),
      '--cp-workers',str(cp_workers),'--solve-seconds',str(solve_seconds),'--threads','2','--construct-workers','1','--construct-seconds','2',
      '--wall-seconds',str(seconds),'--max-trials',str(trials),'--bootstrap-limit',str(bootstrap),'--target',str(target),
      '--thinking-budget',str(thinking_budget),'--api-timeout','170','--aux-timeout','45','--summary-trigger-chars','2000','--memory-update-every','4','--summary-tail','3']
 out.parent.mkdir(parents=True,exist_ok=True);t=time.monotonic()
 with open(out.parent/f'{name}.log','w') as log:
  process=subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=seconds+30)
 report=json.loads((out/'report.json').read_text()) if (out/'report.json').exists() else {}
 result=dict(name=name,case=case,exit=process.returncode,wall_s=time.monotonic()-t,first_valid_s=report.get('first_valid_s'),
   day_minutes=(report.get('best') or {}).get('day_minutes'),memory_hits=report.get('memory_hits'),direct_reuse=report.get('direct_reuse_candidates'),
   references=report.get('reference_only_candidates'),adaptations=report.get('adaptation_records',[]),model_calls=report.get('api_calls'),
   summary_calls=report.get('summary_calls'),memory_update_calls=report.get('memory_update_calls'),errors=report.get('errors'),stop=report.get('stop_reason'))
 print(json.dumps(result,ensure_ascii=False),flush=True);return result

def main():
 p=argparse.ArgumentParser();p.add_argument('phase',choices=['offline','online']);a=p.parse_args()
 dest=ROOT/'runs/context-benchmark-final';dest.mkdir(parents=True,exist_ok=True);db=dest/'training.sqlite';results=[]
 if a.phase=='offline':
  for case in ['cold4-car2','cold8-car2','cold8-car2-heating','cold4-car1-press']:
   results.append(run('train-'+case,case,db,memory='read-write',seconds=25,trials=12))
  for repeat in range(2):
   for case in ['cold8-car2','cold8-car2-heating','cold4-car1-press']:
    for mode in (['off','read-only'] if repeat==0 else ['read-only','off']):
     results.append(run(f'{case}-{mode}-{repeat}',case,db,memory=mode,seconds=20,trials=8,target=1))
  for case in ['cold12-car2','cold20-car2','cold8-car2-press','cold20-car2-day2']:
   results.append(run('new-'+case,case,db,memory='read-only',seconds=25,trials=10,target=1))
 else:
  if not os.environ.get('ANTHROPIC_AUTH_TOKEN'):os.environ['ANTHROPIC_AUTH_TOKEN']=getpass.getpass('API token (not saved): ')
  # Main model and qwen3.7-plus calls are included in end-to-end wall time.
  # Bootstrap is disabled only here to isolate planner adaptation from generic seeds.
  for case in ['cold12-car2','cold8-car2-press']:
   for mode in ['off','read-write']:
    isolated=dest/f'online-{case}-{mode}.sqlite'
    if mode!='off':
     with sqlite3.connect(db) as src,sqlite3.connect(isolated) as dst:src.backup(dst)
    results.append(run(f'online-{case}-{mode}',case,isolated,model='qwen3.8-max',memory=mode,seconds=180,trials=80,target=1440,bootstrap=0,aux='qwen3.7-plus',rounds=2))
 (dest/f'{a.phase}.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
