"""Exact day-prefix screening, full continuation and audited incumbent commit."""
import fcntl
import json
import os
from pathlib import Path
import threading
import time
from ortools.sat.python import cp_model
from .model import build_model, extract_schedule, flatten
from .prefix_model import fix_prefix, project_schedule
from .audit import audit


def atomic_json(path, value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    os.replace(tmp,path)


def read_best(outdir):
    path=Path(outdir)/'best.json'
    return json.loads(path.read_text()) if path.exists() else None


def solver_statistics(solver):
    """Final native-search counters; zeros mean the solver never started."""
    result={}
    for key,attr in (('num_booleans','num_booleans'),('branches','num_branches'),
                     ('conflicts','num_conflicts'),('solver_wall_s','wall_time')):
        try:result[key]=getattr(solver,attr)
        except RuntimeError:result[key]=0.0 if key=='solver_wall_s' else 0
    return result


def solve_interruptibly(solver, model, stop_event=None, callback=None,
                        log_path=None, log_label='solve'):
    done=threading.Event()
    def watch():
        while not done.wait(.25):
            if stop_event is not None and stop_event.is_set():
                solver.stop_search()
                return
    monitor=threading.Thread(target=watch,daemon=True)
    if stop_event is not None and stop_event.is_set():
        return cp_model.UNKNOWN
    log=None
    previous_callback=solver.log_callback
    previous_progress=solver.parameters.log_search_progress
    previous_stdout=solver.parameters.log_to_stdout
    if log_path is not None:
        path=Path(log_path);path.parent.mkdir(parents=True,exist_ok=True)
        log=path.open('a',encoding='utf-8')
        log_lock=threading.Lock()
        def native_log(message):
            with log_lock:
                fcntl.flock(log,fcntl.LOCK_EX)
                try:
                    log.write(f'[pid={os.getpid()} {log_label}] '+str(message).rstrip()+'\n')
                    log.flush()
                finally:fcntl.flock(log,fcntl.LOCK_UN)
        solver.log_callback=native_log
        solver.parameters.log_search_progress=True
        solver.parameters.log_to_stdout=False
    monitor.start()
    try:
        return solver.solve(model, callback) if callback is not None else solver.solve(model)
    finally:
        done.set()
        monitor.join()
        if log is not None:
            solver.log_callback=previous_callback
            solver.parameters.log_search_progress=previous_progress
            solver.parameters.log_to_stdout=previous_stdout
            log.close()


def complete_prefix(scenario, prefix, incumbent_ticks, day_end_ticks=None,
                    workers=1, stop_event=None, hint=None, solver_log_path=None):
    D=int(day_end_ticks if day_end_ticks is not None else prefix['day_end_ticks'])
    ops=flatten(scenario)
    if any(len(prefix.get(k,[]))!=len(ops) for k in ('heat_start','forge_start','furnace')):
        return {'status':'invalid','reason':'prefix size differs','solver_wall_s':0.0}
    starts=prefix['forge_start']
    if any(not 0<=s<=D for s in starts):
        return {'status':'invalid','reason':'prefix start out of range','solver_wall_s':0.0}
    score=sum(min(q['duration'],D-s) for q,s in zip(ops,starts))
    if score != prefix.get('day_score_ticks',score):
        return {'status':'invalid','reason':'prefix score mismatch','solver_wall_s':0.0}
    base={'score_ticks':int(score),'day_end_ticks':D,'solver_wall_s':0.0}
    # This gate happens before constructing a full model or solving later days.
    if score <= incumbent_ticks:
        return dict(base,status='filtered')
    if stop_event is not None and stop_event.is_set():
        return dict(base,status='unknown',reason='interrupted before suffix')
    begin=time.monotonic()
    m,v,_=build_model(scenario)
    base['model_build_s']=time.monotonic()-begin
    try:
        fix_prefix(m,v,prefix,D)
    except ValueError as exc:
        return dict(base,status='invalid',reason=str(exc))
    if hint is not None:
        for k in ('heat_start','forge_start','furnace'):
            for variable,value in zip(v[k],hint[k]):
                m.add_hint(variable,int(value))
    solver=cp_model.CpSolver()
    solver.parameters.num_search_workers=workers
    solver.parameters.symmetry_level=0
    result=solve_interruptibly(solver,m,stop_event,log_path=solver_log_path,log_label='continuation')
    try:
        wall_s=solver.wall_time
    except RuntimeError:
        wall_s=0.0  # Stop requested before the native solver started.
    base.update(solver_wall_s=wall_s,elapsed_s=time.monotonic()-begin,
                cp_status=solver.status_name(result),solver_stats=solver_statistics(solver))
    if result == cp_model.INFEASIBLE:
        return dict(base,status='suffix_infeasible')
    if result not in (cp_model.FEASIBLE,cp_model.OPTIMAL):
        return dict(base,status='invalid' if result==cp_model.MODEL_INVALID else 'unknown')
    schedule=extract_schedule(solver,v)
    report=audit(scenario,schedule)
    projection=project_schedule(ops,schedule,D)
    matches=all(projection[k]==prefix[k] for k in ('heat_start','forge_start','furnace'))
    if not report['valid'] or not matches or projection['day_score_ticks'] != score:
        return dict(base,status='invalid',schedule=schedule,audit=report,
                    reason='full audit or exact prefix equality failed')
    return dict(base,status='feasible',schedule=schedule,audit=report)


def publish_incumbent(outdir, candidate, source):
    """Only audited complete improvements may replace the shared incumbent."""
    outdir=Path(outdir)
    outdir.mkdir(parents=True,exist_ok=True)
    if candidate.get('status')!='feasible' or not candidate.get('audit',{}).get('valid') or not candidate.get('schedule'):
        return False
    with (outdir/'incumbent.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        previous=read_best(outdir)
        if previous is not None and candidate['score_ticks']<=previous['score_ticks']:
            return False
        record=dict(candidate,source=source,published_unix=time.time(),
                    previous_score_ticks=None if previous is None else previous['score_ticks'])
        stamp=time.time_ns()
        atomic_json(outdir/'improvements'/f'{stamp}-{record["score_ticks"]}.json',record)
        atomic_json(outdir/'best.json',record)
        return True
