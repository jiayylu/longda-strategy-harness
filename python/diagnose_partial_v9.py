"""Read-only priority features; use frozen time-window code, add no constraints."""
import sys,json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reference"))
from time_windows import route_windows,contradictions
from longda_cpsat.model import flatten
sc=json.loads(Path(sys.argv[1]).read_text());s=json.loads(Path(sys.argv[2]).read_text());ops=flatten(sc)
b=route_windows(sc,s,ops);c=contradictions(sc,s,ops)
intervals=sorted((s['forge_start'][i],s['forge_end'][i]) for i in range(len(ops)) if s['forge_start'][i] is not None and s['forge_end'][i] is not None)
conflicts=sum(a[1]>b[0] for a,b in zip(intervals,intervals[1:]))
print(json.dumps(dict(total_operations=len(ops),scheduled_forges=len(intervals),window_conflicts=len(c),press_conflicts=conflicts,slack_min_minutes=min((v['latest_forge']-v['earliest_forge'])/sc['ticks_per_minute'] for v in b),proof_scope='hypothetical_fixed_hints_only',can_prune_mutable_completion=False)))
