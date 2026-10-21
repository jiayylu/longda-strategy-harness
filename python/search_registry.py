"""Atomic run-local deduplication of effective CP inputs, never of feasibility states."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@contextlib.contextmanager
def registry(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = json.loads(p.read_text()) if p.exists() else {}
        yield rows
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(rows, sort_keys=True))
        tmp.replace(p)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def claim_search(path, sc, req, incumbent, hint, selected, owner):
    if not path:
        return None, None
    # Names, ranking code, grouping labels and direction labels do not alter this CP model.
    # Selected order identities, exact hints, baseline and solver configuration do.
    effective = dict(scenario=sc, rules=req.get('rule_hash'), incumbent=incumbent,
                     hint=hint, released_orders=sorted(selected),
                     partial=(json.loads(Path(req['partial_path']).read_text())
                              if not hint and req.get('partial_path') else None),
                     solver={k:req.get(k) for k in ('mode','seed','seconds','threads','probing_level',
                             'presolve','preference','stop_at_target','target_minutes')})
    key = digest(effective)
    with registry(path) as rows:
        previous = rows.get(key)
        if previous and previous['status'] not in ('INTERRUPTED','CANCELLED'):
            if previous['status'] != 'RUNNING' or alive(previous['pid']):
                return key, previous
        rows[key] = dict(status='RUNNING',pid=os.getpid(),owner=owner)
    return key, None


def finish_search(path, key, owner, status, cancelled=False, only_running=False):
    if not path or not key:
        return
    with registry(path) as rows:
        row = rows.get(key)
        if not row or row['owner'] != owner or (only_running and row['status'] != 'RUNNING'):
            return
        row['status'] = 'INTERRUPTED' if cancelled else status
