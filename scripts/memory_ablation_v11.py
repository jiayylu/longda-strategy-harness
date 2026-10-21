"""Bounded independent runs; one frozen DB snapshot, no saved timetable hints."""
import argparse,getpass,json,os,signal,sqlite3,subprocess,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--tag',required=True);p.add_argument('--live',action='store_true');a=p.parse_args()
root=Path(__file__).resolve().parents[1];out=root/'runs'/a.tag;out.mkdir(parents=True,exist_ok=False)
if a.live and not os.environ.get('ANTHROPIC_AUTH_TOKEN'):os.environ['ANTHROPIC_AUTH_TOKEN']=getpass.getpass('API token (not saved): ')
snap=out/'memory-v11.sqlite'
with sqlite3.connect(f'file:{root}/data/experience-v11.sqlite?mode=ro',uri=True) as src,sqlite3.connect(snap) as dst:src.backup(dst)
# Reverse order in repeat two to reduce time-of-run/resource-order effects.
arms=['off','raw','cards'] if a.live else ['off','raw','cards','cards','raw','off']
rows=[]
for idx,arm in enumerate(arms):
 seconds=100 if a.live else 30
 label=f'{idx+1}-{arm}';dest=out/label
 cmd=[str(root/'.venv/bin/python'),str(root/'start.py'),'--orders','configs/orders20.json','--vehicles','2','--out',str(dest),
 '--model','qwen3.8-max' if a.live else 'off','--effort','low','--thinking-budget','1024','--max-tokens','8192',
 '--summary-model','qwen3.7-plus' if a.live else 'off','--memory-model','off',
 '--memory','off' if arm=='off' else 'read-only','--memory-content','cards' if arm=='cards' else 'raw','--memory-db',str(snap),
 '--cp-workers','2','--threads','4','--construct-workers','1','--construct-seconds','5','--solve-seconds','10','--completion-seconds','10',
 '--rounds','1' if a.live else '0','--repair-attempts','0','--strategy-trials','2','--api-timeout','70','--aux-timeout','15',
 '--wall-seconds',str(seconds),'--target','1140']
 start=time.monotonic();load=os.getloadavg();print('START',label,flush=True)
 with (out/f'{label}.log').open('w') as log:
  proc=subprocess.Popen(cmd,cwd=root,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  try:proc.wait(timeout=seconds+25)
  except subprocess.TimeoutExpired:
   os.killpg(proc.pid,signal.SIGINT)
   try:proc.wait(timeout=10)
   except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
 report=json.loads((dest/'report.json').read_text()) if (dest/'report.json').exists() else {}
 stats=json.loads((dest/'search-statistics.json').read_text()) if (dest/'search-statistics.json').exists() else {}
 row={'arm':arm,'label':label,'wall_s':time.monotonic()-start,'exit_code':proc.returncode,'load_start':load,'first_valid_s':report.get('first_valid_s'),
 'best_minutes':(report.get('best') or {}).get('day_minutes'),'audit_valid':(report.get('best') or {}).get('audit_valid'),
 'trials':report.get('completed_trials'),'model_delivery':report.get('planner_delivery'),'reason':report.get('stop_reason'),
 'memory':stats.get('memory'),'errors':report.get('errors'),'model_wall_s':report.get('timing',{}).get('planner_wall_s'),
 'model_strategy_improvements':sum(x.get('gain_minutes',0)>0 for x in report.get('history',[]) if x.get('strategy_id'))}
 rows.append(row);(out/'summary.json').write_text(json.dumps({'protocol':{'orders':20,'vehicles':2,'budget_each_s':seconds,'cp_workers':2,'threads':4,'main_model':a.live,'memory_updater':'off to isolate read effect','limits':'Short samples only; offline cards cannot influence model; live single repeats include model wall time, not causal proof.'},'results':rows},ensure_ascii=False,indent=2))
 print(json.dumps({k:row[k] for k in ['label','first_valid_s','best_minutes','audit_valid','reason','model_delivery']},ensure_ascii=False),flush=True)
