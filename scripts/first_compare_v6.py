"""Actual CLI first-valid timing. Fixed offline controls, no LLM calls or long search."""
import json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];old=ROOT.parent/'longda-strategy-harness-v5-20261016';out=ROOT/'runs/first-comparison';out.mkdir(parents=True,exist_ok=True)
rows=[]
for version,root in [('v5',old),('v6',ROOT)]:
 dest=out/version;begin=time.monotonic()
 cmd=[str(root/'.venv/bin/python'),str(root/'start.py'),'--orders','configs/orders20.json','--vehicles','1','--out',str(dest),
 '--model','off','--aux-model','off','--memory','off','--cp-workers','0','--construct-workers','1',
 '--rounds','0','--bootstrap-limit','4','--construct-seconds','8','--wall-seconds','25','--target','1']
 with (out/f'{version}.log').open('w') as log:r=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,timeout=40)
 report=json.loads((dest/'report.json').read_text());row=dict(version=version,process_wall_s=time.monotonic()-begin,
 first_valid_s=report.get('first_valid_s'),day_minutes=(report.get('best') or {}).get('day_minutes'),
 valid=(report.get('best') or {}).get('audit_valid',False),trials=report['completed_trials'],model_calls=report['api_calls'])
 rows.append(row);print(json.dumps(row),flush=True)
(out/'report.json').write_text(json.dumps(rows,indent=2))
