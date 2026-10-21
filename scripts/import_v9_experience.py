"""Read source runs; write immutable evidence index and reviewed card proposals in v9 only."""
import argparse,json,hashlib,sqlite3
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--source-root',action='append',required=True);a=p.parse_args()
root=Path(__file__).resolve().parents[1];run=Path(a.run).resolve();d=json.loads((run/'report.json').read_text());rid=d['run_id'];h=d['history']
entries={};roots=[str(Path(r).resolve()) for r in a.source_root]
for base in map(Path,roots):
 for report in (base/'runs').glob('*/report.json'):
  try:r=json.loads(report.read_text())
  except (ValueError,OSError):continue
  for row in r.get('history',[]):
   f=Path(row.get('result','')).resolve()
   if not f.is_file() or not any(f.is_relative_to(Path(v)) for v in roots):continue
   entries[row['id']]={'path':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'summary':{k:row.get(k) for k in ['status','valid','audit_valid','day_minutes','gain_minutes','baseline_before_minutes','effective_fraction','effective_direction','reused_evidence_ids','proof_scope']}}
(root/'data').mkdir(exist_ok=True)
(root/'data/evidence-index.json').write_text(json.dumps({'roots':roots,'entries':entries},ensure_ascii=False,indent=2))
conditions=['Observed on 20 cold orders, four routes x five, six furnaces x five slots, two cars, five-minute transfers, four-day horizon.','One run with history reuse, 6 CP workers x 8 threads, 5000 seconds. Comparisons are observational.']
def ids(rows):return [x['id'] for x in rows][:20]
def card(name,rec,exceptions,rows):
 return dict(op='add',id='review-v9-'+name,expected_version=0,conditions=conditions,recommendation=rec,exceptions=exceptions,evidence_ids=ids(rows))
press=[x for x in h if x.get('effective_direction')=='press_gap' and x.get('gain_minutes',0)>0]
last=[x for x in h if x['id'].endswith('/task-595/1')]
partial=[x for x in h if x.get('purpose')=='partial_complete']
first=[h[0]]
cards=[
card('press-local','Prefer testing press-gap local neighborhoods with about 8/20 orders free: eight published improvements totaled 325 minutes in this run. Keep actual released-order identities, baseline and seeds in evidence.', ['Not a universal success probability or proof that this fraction is optimal. Outcomes depended on an evolving shared incumbent.'],press),
card('full-search','Keep a limited full-model improvement lane. The final 854 to 858 improvement used furnace_batch with fraction=1: all twenty orders were released. Group performance by actual scope, not strategy label.', ['Other full-model attempts returned UNKNOWN; this does not prove full search useless or guarantee reaching target 870.'],last),
card('partial-completion','39 partial completion calls at 15 seconds all returned UNKNOWN in this run. Rank hints by consistency, time-window slack, novelty and observed completion yield; use a bounded exploration share.', ['UNKNOWN is not infeasible. More seconds may help but has not been demonstrated. Low coverage alone must not cause mathematical pruning.'],partial[:10]+partial[-10:]),
card('reuse-accounting','The first audited schedule was recomputed from an eligible historical strategy in 5.24 seconds and scored 519. Retrieve compatible strategies and reasons, never inject historical schedules as newly solved answers.', ['Eight retrieved candidates are not eight demonstrated improvements. Memory influence needs matched cold/warm tests.'],first)
]
# Keep validated source-derived counts with unambiguous accounting.
review=dict(source_run=str(run),source_run_id=rid,final_minutes=d['best']['day_minutes'],first_minutes=h[0]['day_minutes'],first_valid_s=d['first_valid_s'],completed_attempts=len(h),cp_solver_calls=d['cp_solver_calls'],completion_calls=d['completion_attempts'],complete_valid_results=sum(bool(x.get('valid') and x.get('audit_valid')) for x in h),
publication_count=sum(json.loads(l).get('kind')=='incumbent_updated' for l in (run/'events.jsonl').read_text().splitlines()),cards=cards,causal_status='observational, not controlled proof',no_timetables_imported=True)
(root/'data/reviewed-experience.json').write_text(json.dumps(review,ensure_ascii=False,indent=2))
print(json.dumps({'indexed_evidence':len(entries),'cards':len(cards),'review':{k:v for k,v in review.items() if k not in ['cards','source_run']}},ensure_ascii=False))
