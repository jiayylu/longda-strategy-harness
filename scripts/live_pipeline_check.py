"""Bounded actual production path: generic work + main model + two roles + CP."""
import argparse,getpass,json,os,sqlite3
from context_benchmark import ROOT,run

p=argparse.ArgumentParser();p.add_argument('--case');p.add_argument('--tag',default='pipeline');p.add_argument('--seconds',type=int,default=150);args=p.parse_args()
if not os.environ.get('ANTHROPIC_AUTH_TOKEN'):
    os.environ['ANTHROPIC_AUTH_TOKEN']=getpass.getpass('API token (not saved): ')
dest=ROOT/'runs/context-benchmark-final'
results=[]
for case in [args.case] if args.case else ['cold20-car2','cold8-car2-press']:
    db=dest/f'{args.tag}-{case}.sqlite'
    with sqlite3.connect(dest/'training.sqlite') as a,sqlite3.connect(db) as b:
        a.backup(b)
    results.append(run(f'{args.tag}-{case}',case,db,model='qwen3.8-max',memory='read-write',
        seconds=args.seconds,trials=70,target=1440,bootstrap=20,aux='qwen3.7-plus',rounds=1,
        thinking_budget=4096,cp_workers=2,solve_seconds=12))
(dest/f'{args.tag}.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
