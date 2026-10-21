"""Bounded v9/v10 comparison. No schedule answers or rule changes. Models included in live wall clock."""
import argparse, json, os, signal, subprocess, time, sqlite3, getpass
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--tag',required=True);p.add_argument('--live',action='store_true');a=p.parse_args()
root=Path(__file__).resolve().parents[1];old=root.parent/'longda-strategy-harness-v9-20261020';out=root/'runs'/a.tag;out.mkdir(parents=True,exist_ok=False)
if a.live and not os.environ.get('ANTHROPIC_AUTH_TOKEN'):os.environ['ANTHROPIC_AUTH_TOKEN']=getpass.getpass('API token (not saved): ')
if a.live:
 db=out/'test-memory.sqlite'
 with sqlite3.connect(f'file:{root}/data/experience-v10.sqlite?mode=ro',uri=True) as src,sqlite3.connect(db) as dst:src.backup(dst)
else:db=root/'data/experience-v10.sqlite'
arms=[('v10-live',root)] if a.live else [('v9-1',old),('v10-1',root),('v10-2',root),('v9-2',old)]
rows=[]
for label,folder in arms:
 seconds=180 if a.live else 45
 cmd=[str(folder/'.venv/bin/python'),str(folder/'start.py'),'--orders','configs/orders20.json','--vehicles','2','--out',str(out/label),
 '--model','qwen3.8-max' if a.live else 'off','--effort','low','--thinking-budget','1024','--max-tokens','8192',
 '--summary-model','qwen3.7-plus' if a.live else 'off','--memory-model','qwen3.7-plus' if a.live else 'off',
 '--memory','read-write' if a.live else 'read-only','--memory-db',str(db),
 '--cp-workers','6','--threads','4','--construct-workers','1','--rounds','1' if a.live else '0',
 '--strategy-trials','3','--repair-attempts','1','--construct-seconds','5','--solve-seconds','10',
 '--completion-seconds','10','--api-timeout','150','--aux-timeout','30','--wall-seconds',str(seconds),'--target','1140']
 before=time.monotonic();load=os.getloadavg();print('START',label,flush=True)
 with (out/f'{label}.log').open('w') as log:
  proc=subprocess.Popen(cmd,cwd=folder,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  try:proc.wait(timeout=seconds+30)
  except subprocess.TimeoutExpired:
   os.killpg(proc.pid,signal.SIGINT)
   try:proc.wait(timeout=10)
   except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
 dest=out/label;report=json.loads((dest/'report.json').read_text()) if (dest/'report.json').exists() else {}
 events=[json.loads(l) for l in (dest/'events.jsonl').read_text().splitlines()] if (dest/'events.jsonl').exists() else []
 active=set();area=0;last=0;peak=0;refill=0
 for e in events:
  t=e['elapsed_s'];area+=len(active)*(t-last);last=t;d=e.get('data',{})
  if e['kind']=='tool_call' and d.get('action')=='solve':active.add(d['call_id'])
  if e['kind']=='tool_result':active.discard(d.get('call_id'))
  if e['kind']=='portfolio_refilled':refill+=d['added']
  peak=max(peak,len(active))
 row=dict(arm=label,process_wall_s=time.monotonic()-before,load_start=load,load_end=os.getloadavg(),exit_code=proc.returncode,
  elapsed_s=report.get('elapsed_s'),first_valid_s=report.get('first_valid_s'),day_minutes=(report.get('best') or {}).get('day_minutes'),
  audit_valid=(report.get('best') or {}).get('audit_valid'),completed_trials=report.get('completed_trials'),
  model_calls=report.get('api_calls'),planner_delivery=report.get('planner_delivery'),timing=report.get('timing'),errors=report.get('errors'),
  completion_attempts=report.get('completion_attempts'),completion_valid=report.get('completion_valid'),reason=report.get('stop_reason'),
  peak_cp_tasks=peak,mean_cp_tasks=area/max(1,report.get('elapsed_s') or last),generated_tasks=refill)
 rows.append(row);(out/'summary.json').write_text(json.dumps({'protocol':{'orders':20,'vehicles':2,'cp_workers':6,'threads':4,'construct_workers':1,
 'memory':'identical read-only v10 snapshot for paired arms; isolated writable copy for live',
 'schedule_answers':False,'causality_limit':'two short repeats, no general speedup claim','concurrency_note':'CP worker lifetime includes build and audit; not OS CPU usage'},'results':rows},ensure_ascii=False,indent=2))
 print(json.dumps({k:row[k] for k in ['arm','elapsed_s','first_valid_s','day_minutes','audit_valid','peak_cp_tasks','mean_cp_tasks','reason']},ensure_ascii=False),flush=True)
