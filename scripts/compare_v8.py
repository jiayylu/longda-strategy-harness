"""Bounded v7/v8 comparison. No stored schedules or experience; optional real model calls.
Each arm is a new cold start. Concurrent unrelated server load is recorded, not concealed.
"""
import argparse,json,os,subprocess,time,signal,getpass,hashlib
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--live',action='store_true');p.add_argument('--tag',required=True);p.add_argument('--only',choices=['v7-live','v8-live']);a=p.parse_args()
root=Path(__file__).resolve().parents[1];old=root.parent/'longda-strategy-harness-v7-20261019'
out=root/'runs'/a.tag;out.mkdir(parents=True,exist_ok=False)
if a.live and not os.environ.get('ANTHROPIC_AUTH_TOKEN'):
 os.environ['ANTHROPIC_AUTH_TOKEN']=getpass.getpass('API token (not saved): ')
rows=[]
arms=[('v7-off-1',old,False),('v8-off-1',root,False),('v8-off-2',root,False),('v7-off-2',old,False)]
if a.live:arms += [('v7-live',old,True),('v8-live',root,True)]
if a.only:
 if not a.live:raise SystemExit('--only live arms require --live')
 arms=[x for x in arms if x[0]==a.only]
for label,folder,live in arms:
 dest=out/label;seconds=150 if live else 45
 cmd=[str(folder/'.venv/bin/python'),str(folder/'start.py'),'--orders','configs/orders20.json','--vehicles','2','--out',str(dest),
 '--model','qwen3.8-max' if live else 'off','--effort','low','--thinking-budget','1024','--max-tokens','8192',
 '--summary-model','off','--memory-model','off','--memory','off',
 '--cp-workers','2','--threads','4','--construct-workers','1','--rounds','1' if live else '0',
 '--strategy-trials','3','--repair-attempts','1','--construct-seconds','5','--solve-seconds','10',
 '--completion-seconds','15','--api-timeout','130','--wall-seconds',str(seconds),'--target','1140']
 start=time.monotonic();load=os.getloadavg();print('START',label,flush=True)
 with (out/(label+'.log')).open('w') as log:
  proc=subprocess.Popen(cmd,cwd=folder,stdout=log,stderr=subprocess.STDOUT,env=os.environ.copy(),start_new_session=True)
  try:proc.wait(timeout=seconds+30)
  except subprocess.TimeoutExpired:
   os.killpg(proc.pid,signal.SIGINT)
   try:proc.wait(timeout=15)
   except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
 r=json.loads((dest/'report.json').read_text()) if (dest/'report.json').exists() else {}
 history=r.get('history',[]);cs=[t for t in history if t.get('purpose')=='partial_complete']
 row=dict(arm=label,exit_code=proc.returncode,wall_limit_s=seconds,process_wall_s=time.monotonic()-start,load_start=load,load_end=os.getloadavg(),
  elapsed_s=r.get('elapsed_s'),first_valid_s=r.get('first_valid_s'),day_minutes=(r.get('best')or{}).get('day_minutes'),audit_valid=(r.get('best')or{}).get('audit_valid'),
  model_calls=r.get('api_calls'),model_requests=r.get('actual_main_model_requests'),planner_delivery=r.get('planner_delivery'),timing=r.get('timing'),
  completed_trials=r.get('completed_trials'),completion_attempts=r.get('completion_attempts'),completion_valid=r.get('completion_valid'),
  completion_allocated_s=sum(t.get('allocated_seconds',0) for t in cs),quality_feedback=len(r.get('quality_feedback',[])),
  deferrals=len(r.get('completion_deferrals',[])),errors=r.get('errors'),reason=r.get('stop_reason'),campaigns=r.get('strategy_campaigns'))
 rows.append(row);(out/'summary.json').write_text(json.dumps({'protocol':{'orders':20,'vehicles':2,'cp_workers':2,'threads':4,'construct_workers':1,'memory':'off','summary':'off','target':1140,'known_answers':False,'qualification':'short interleaved diagnostic; unrelated server work continues'},'results':rows},ensure_ascii=False,indent=2))
 print(json.dumps({k:row[k] for k in ['arm','elapsed_s','first_valid_s','day_minutes','audit_valid','model_calls','completion_attempts','quality_feedback','reason','exit_code']},ensure_ascii=False),flush=True)
