"""Finite production-flow check plus cold/experience planner comparisons."""
import getpass,os,json,sqlite3
from pathlib import Path
from context_benchmark import ROOT,run

if not os.environ.get('ANTHROPIC_AUTH_TOKEN'):
 os.environ['ANTHROPIC_AUTH_TOKEN']=getpass.getpass('API token (not saved): ')
dest=ROOT/'runs/context-benchmark-final';source=dest/'training.sqlite';results=[]
for case in ['cold12-car2','cold8-car2-press']:
 for mode in ['off','read-write']:
  db=dest/f'compact-{case}-{mode}.sqlite'
  if mode!='off':
   with sqlite3.connect(source) as a,sqlite3.connect(db) as b:a.backup(b)
  results.append(run(f'compact-{case}-{mode}',case,db,model='qwen3.8-max',memory=mode,seconds=180,trials=60,target=1440,bootstrap=0,aux='qwen3.7-plus',rounds=2,thinking_budget=4096))
(dest/'compact-online.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
