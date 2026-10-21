"""Two bounded REAL model runs, no stored timetable, all API and audit time included.
Credential is read without echo and kept in child environment only.
"""
import getpass,json,os,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];old=ROOT.parent/'longda-strategy-harness-v5-20261016'
if not os.environ.get('ANTHROPIC_AUTH_TOKEN'):os.environ['ANTHROPIC_AUTH_TOKEN']=getpass.getpass('API token (not saved): ')
out=ROOT/'runs/live-comparison';out.mkdir(parents=True,exist_ok=True)
# Same real failing greedy code and current orders in both arms. No known schedule/hint provided.
initial=ROOT/'configs/calendar-repair-start.json'
rows=[]
for version,root in [('v5',old),('v6',ROOT)]:
 dest=out/version
 cmd=[str(root/'.venv/bin/python'),str(root/'start.py'),'--orders','configs/orders20.json','--vehicles','1','--out',str(dest),
 '--model','qwen3.8-max','--effort','low','--thinking-budget','1024','--max-tokens','8192',
 '--summary-model','off','--memory-model','off','--memory','off','--bootstrap-limit','0',
 '--cp-workers','0','--threads','1','--construct-workers','1','--rounds','1','--strategy-trials','1',
 '--repair-attempts','1','--stop-after-repair','--algorithm-development','--initial-strategy-file',str(initial),
 '--construct-seconds','8','--api-timeout','100','--wall-seconds','120','--target','600']
 begin=time.monotonic()
 with (out/f'{version}.log').open('w') as log:
  p=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,env=os.environ.copy(),start_new_session=True)
  try:p.wait(timeout=150)
  except subprocess.TimeoutExpired:
   import signal
   os.killpg(p.pid,signal.SIGINT)
   try:p.wait(timeout=15)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
 elapsed=time.monotonic()-begin
 report=json.loads((dest/'report.json').read_text()) if (dest/'report.json').exists() else {}
 row=dict(version=version,process_wall_s=elapsed,exit_code=p.returncode,total_s=report.get('elapsed_s'),
  first_valid_s=report.get('first_valid_s'),best_day_minutes=(report.get('best') or {}).get('day_minutes'),
  model_api_requests=report.get('actual_main_model_requests'),model_wall_s=report.get('timing',{}).get('planner_wall_s'),
  errors=report.get('errors'),stop_reason=report.get('stop_reason'),construction_trials=report.get('construction_trials'))
 rows.append(row);(out/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2));print(json.dumps(row,ensure_ascii=False),flush=True)
