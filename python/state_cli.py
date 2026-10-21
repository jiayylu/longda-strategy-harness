"""Validate/describe observed state, or export a past-only test snapshot."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'reference')]
from production_state import features,snapshot

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['describe','snapshot']);p.add_argument('--scenario');p.add_argument('--schedule');p.add_argument('--now-minutes',type=float);p.add_argument('--out');a=p.parse_args()
    sc=json.loads(Path(a.scenario).read_text()) if a.scenario else json.load(sys.stdin)
    if a.action=='describe':print(json.dumps(features(sc)))
    else:
        at=a.now_minutes*sc['ticks_per_minute']
        if not at.is_integer():raise ValueError('Snapshot time must be on physical tick lattice')
        s=json.loads(Path(a.schedule).read_text());value=snapshot(sc,s,int(at));Path(a.out).write_text(json.dumps(value,indent=2))
