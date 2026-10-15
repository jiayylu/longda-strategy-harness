"""One continuous CP-SAT solve at a time; day-prefix then full continuation.

No time-sliced restarts, learned direction allocation, or neighborhood fixing.
Heuristic stopping watches VERIFIED full-schedule gains over wall-time windows.
Saved UNKNOWN prefixes remain pending; native solver trees are not serialized.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import threading
import time

from . import dayfirst_tasks
from .dayfirst_stopping import DayfirstStop
from .search import upper_bound
from .shared_search import effective_scenario, load_json, publish_candidate, _snapshot_best
from .solver_flow import atomic_json, read_best


def parser():
    p = argparse.ArgumentParser(prog='longda-cpsat first/search --strategy dayfirst',
        description='One continuous CP-SAT with day-prefix/complete-continuation search')
    p.add_argument('--scenario', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--threads', type=int, default=48, help='Internal CP-SAT workers; one solver at a time')
    p.add_argument('--processes', type=int, default=1, help='Must be 1 in this strategy')
    p.add_argument('--transport-mode', choices=('none', 'vehicles'))
    p.add_argument('--vehicles', type=int)
    p.add_argument('--preference', choices=('none', 'hold-near-min'), default='none')
    p.add_argument('--first-time-limit', type=float, help='Optional total first-solution wall limit, e.g. 600; UNKNOWN at timeout')
    p.add_argument('--observation-seconds', type=float, default=300, help='Improvement observation window; does NOT restart a solver')
    p.add_argument('--no-gain-rounds', type=int, default=3)
    p.add_argument('--small-gain-rounds', type=int, default=3)
    p.add_argument('--min-gain-minutes', type=float, default=5)
    p.add_argument('--min-hold-gain-minutes', type=float, default=5)
    p.add_argument('--no-early-stop', action='store_true')
    p.add_argument('--min-free-gib', type=float, default=10)
    p.add_argument('--baseline', type=Path, help='Import an independently audited full schedule into a NEW run')
    p.add_argument('--solver-log', action='store_true')
    p.add_argument('--first-only', action='store_true')
    p.add_argument('--max-solves', type=int, help='Optional count cap for short verification; no time slices')
    return p


def _validate(a):
    if a.processes != 1:
        raise ValueError('dayfirst runs ONE CP-SAT: use --processes 1 --threads 48, or omit --processes')
    for key in ('threads', 'no_gain_rounds', 'small_gain_rounds'):
        if getattr(a, key) < 1:
            raise ValueError(key + ' must be positive')
    for key in ('first_time_limit', 'observation_seconds'):
        value = getattr(a, key)
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError(key + ' must be finite and positive')
    for key in ('min_gain_minutes', 'min_hold_gain_minutes', 'min_free_gib'):
        value = getattr(a, key)
        if not math.isfinite(value) or value < 0:
            raise ValueError(key + ' must be finite and nonnegative')
    if a.max_solves is not None and a.max_solves < 1:
        raise ValueError('max-solves must be positive')
    if a.vehicles is not None and a.vehicles < 1:
        raise ValueError('vehicles must be positive')
    if a.transport_mode == 'none' and a.vehicles is not None:
        raise ValueError('--vehicles cannot be combined with --transport-mode none')


def _resume(a, out):
    parts = [sys.executable, '-m', 'longda_cpsat', 'first' if a.first_only else 'search',
             '--strategy', 'dayfirst', '--scenario', str(out/'scenario.json'), '--out', str(out)]
    for key in ('threads', 'preference', 'observation_seconds', 'no_gain_rounds',
                'small_gain_rounds', 'min_gain_minutes', 'min_hold_gain_minutes', 'min_free_gib'):
        parts += ['--'+key.replace('_', '-'), str(getattr(a, key))]
    for key in ('first_time_limit', 'max_solves'):
        if getattr(a, key) is not None:
            parts += ['--'+key.replace('_', '-'), str(getattr(a, key))]
    for key in ('no_early_stop', 'solver_log'):
        if getattr(a, key):
            parts.append('--'+key.replace('_', '-'))
    return shlex.join(parts)


def _message(label, content):
    print(f'[{label}] {content}', file=sys.stderr, flush=True)


def main(argv=None):
    a = parser().parse_args(argv)
    _validate(a)
    started = time.monotonic()
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with (out/'farm.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.seek(0); lock.truncate(); lock.write(str(os.getpid())); lock.flush()
        return _run(a, out, started)


def _run(a, out, started):
    sc = effective_scenario(a)
    scale = sc['ticks_per_minute']
    bound = upper_bound(sc, 1440*scale)
    digest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(Path(__file__).parent.glob('*.py'))}
    old = load_json(out/'launch.json')
    if old:
        if old.get('strategy') != 'dayfirst':
            raise ValueError('Use a NEW output directory with --baseline to import an older strategy')
        if old.get('preference') != a.preference:
            raise ValueError('Preference changed; use a NEW output directory with --baseline')
        if old.get('source_sha256') != digest:
            raise ValueError('Frozen source changed; use a NEW output directory with --baseline')
    saved = load_json(out/'scenario.json')
    if saved is not None and saved != sc:
        raise ValueError('Effective scenario differs from saved run')
    best = _snapshot_best(sc, read_best(out))
    reused = best is not None
    imported = None
    if a.baseline:
        if best or old:
            raise ValueError('--baseline requires a new output directory')
        raw = load_json(a.baseline)
        if raw is None:
            raise ValueError('Baseline file does not exist')
        imported = _snapshot_best(sc, {'schedule': raw.get('schedule', raw)})
    checkpoint = load_json(out/'checkpoint.json', {})
    pending = checkpoint.get('pending_prefix')
    excluded = checkpoint.get('excluded_prefixes', [])
    completed = checkpoint.get('completed_solves', 0)
    prior = load_json(out/'progress.json', {}).get('elapsed_s', 0.)
    atomic_json(out/'scenario.json', sc)
    atomic_json(out/'launch.json', dict(strategy='dayfirst', preference=a.preference,
        source_sha256=digest, pid=os.getpid(), processes=1, threads=a.threads,
        first_time_limit_s=a.first_time_limit, per_attempt_budget_s=None,
        search_time_limit_s=None, started_unix=time.time(), resume_command=_resume(a, out),
        first_only=a.first_only, observation_seconds=a.observation_seconds,
        no_early_stop=a.no_early_stop, no_gain_rounds=a.no_gain_rounds,
        small_gain_rounds=a.small_gain_rounds, min_gain_minutes=a.min_gain_minutes,
        min_hold_gain_minutes=a.min_hold_gain_minutes))
    if imported:
        publish_candidate(out, sc, imported, a.preference, {'kind':'import','path':str(a.baseline)})
        best = _snapshot_best(sc, read_best(out)); reused = True
    stopper = DayfirstStop(observation_seconds=a.observation_seconds,
        no_gain_rounds=a.no_gain_rounds, small_gain_rounds=a.small_gain_rounds,
        min_gain_minutes=a.min_gain_minutes, min_hold_gain_minutes=a.min_hold_gain_minutes,
        scale=scale, preference=a.preference, enabled=not a.no_early_stop)
    # On explicit resume, start a fresh observation window; retain unresolved
    # prefixes and proved exclusions, never pretend to restore a native tree.
    stop = threading.Event()
    done = threading.Event()
    guard = threading.RLock()
    state = dict(phase='starting', stage=None, stage_started=started,
                 stop_reason=None, errors=[], active=False, proof=False)
    launched = 0
    first_status = 'feasible' if best else 'unknown'
    deadline = started+a.first_time_limit if a.first_time_limit is not None and best is None else None
    if best:
        stopper.tick(time.monotonic(), best['score_ticks'], best['hold_excess_ticks'])

    def request_stop(reason):
        with guard:
            if state['stop_reason'] is None:
                state['stop_reason'] = reason
            stop.set()

    def progress():
        with guard:
            return dict(strategy='dayfirst', phase=state['phase'], stage=state['stage'],
                preference=a.preference, threads=a.threads, active_processes=int(state['active']),
                active_solvers=int(state['active']), completed_solves=completed,
                launched_this_invocation=launched, elapsed_s=prior+time.monotonic()-started,
                stage_elapsed_s=time.monotonic()-state['stage_started'],
                best_score_ticks=best['score_ticks'] if best else None,
                best_hold_excess_ticks=best['hold_excess_ticks'] if best else None,
                best_day_minutes=best['score_ticks']/scale if best else None,
                best_utilization_percent=100*best['score_ticks']/(1440*scale) if best else None,
                best_audit_valid=bool(best), upper_bound_ticks=bound,
                primary_optimal=bool(best and best['score_ticks']>=bound),
                optimal=bool(state['proof'] and not a.first_only),
                first_solution_score_ticks=checkpoint.get('first_score'),
                initial_solution_status=first_status, stop_reason=state['stop_reason'],
                errors=list(state['errors']), updated_unix=time.time(),
                first_search_time_limit_s=a.first_time_limit, search_time_limit_s=None,
                per_attempt_budget_s=None, reused_existing_incumbent=reused,
                pending_prefix=pending is not None, excluded_prefixes=len(excluded),
                stopping=stopper.snapshot(), native_solver_tree_saved=False)

    def save():
        with guard:
            atomic_json(out/'checkpoint.json', dict(pending_prefix=pending,
                excluded_prefixes=excluded, completed_solves=completed,
                first_score=checkpoint.get('first_score'), native_solver_tree_saved=False))
            atomic_json(out/'progress.json', progress())

    def on_stage(stage, details=None, **kw):
        with guard:
            state.update(stage=stage, stage_started=time.monotonic())
        _message('阶段', f'{stage}；同一求解持续运行，不按短预算重启。')

    def monitor():
        last_message = time.monotonic()
        try:
            while not done.wait(.2):
                now = time.monotonic()
                with guard:
                    if (out/'STOP').exists():
                        request_stop('user_stop')
                    elif best is None and deadline is not None and now >= deadline:
                        request_stop('first_deadline')
                    elif shutil.disk_usage(out).free < a.min_free_gib*1024**3:
                        request_stop('disk_guard')
                    elif best and not a.first_only:
                        reason = stopper.tick(now, best['score_ticks'], best['hold_excess_ticks'])
                        if reason:
                            request_stop(reason)
                    if now-last_message >= 5:
                        save()
                        _message('运行', f'{state["phase"]}；累计 {prior+now-started:.1f} 秒；'
                            f'本阶段连续 {now-state["stage_started"]:.1f} 秒；1 个 CP-SAT × {a.threads} 线程。')
                        last_message = now
        except Exception as exc:
            with guard:
                state['errors'].append(str(exc))
            request_stop('monitor_error')

    signals = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signals[sig] = signal.signal(sig, lambda *_: request_stop('user_stop'))
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    _message('连续求解', f'一个 CP-SAT，{a.threads} 个内部线程；不使用联合改排和方向学习。')
    _message('保温偏好', '关闭：首解为纯可行性搜索。' if a.preference=='none' else
             '开启：首解以少额外保温引导但首份即停；后续按首日优先、保温次优比较。')
    _message('早停', '关闭启发式早停。' if a.no_early_stop else
             f'有基线后每 {a.observation_seconds:g} 秒观察一次；观察不重启求解器。')
    if (out/'STOP').exists():
        request_stop('user_stop')
    if shutil.disk_usage(out).free < a.min_free_gib*1024**3:
        request_stop('disk_guard')
    try:
        while not stop.is_set():
            with guard:
                if best and a.first_only:
                    state.update(phase='first_solution_found', stop_reason='first_solution'); break
                if best and best['score_ticks']>=bound and (a.preference=='none' or best['hold_excess_ticks']==0):
                    state.update(phase='completed', stop_reason='bound_reached', proof=True); break
                if a.max_solves is not None and launched>=a.max_solves:
                    state.update(stop_reason='max_solves'); break
                if best is None:
                    phase='first'
                elif pending is not None:
                    phase='continuation'
                else:
                    phase='prefix'
                state.update(phase=phase, stage='starting', stage_started=time.monotonic(), active=True)
                launched += 1
                solve_id=completed+1
                save()
            kwargs=dict(threads=a.threads, stop_event=stop, on_stage=on_stage,
                        log_path=out/'solver.log' if a.solver_log else None,
                        preference=a.preference)
            if phase=='first':
                result=dayfirst_tasks.first(sc, deadline=deadline, **kwargs)
            elif phase=='prefix':
                result=dayfirst_tasks.prefix(sc, best, excluded=excluded, **kwargs)
            else:
                result=dayfirst_tasks.continuation(sc, pending, best, **kwargs)
            with guard:
                state['active']=False
                completed += 1
                row={k:v for k,v in result.items() if k not in ('schedule','audit')}
                row.update(solve_id=solve_id, phase=phase)
                with (out/'solves.jsonl').open('a') as stream:
                    stream.write(json.dumps(row,ensure_ascii=False)+'\n')
                status=result['status']
                if phase=='first':
                    first_status=status
                if status=='feasible' and phase=='prefix':
                    pending=result['prefix']
                elif status=='feasible':
                    if phase=='first' and deadline is not None and time.monotonic()>=deadline:
                        first_status='unknown';request_stop('first_deadline')
                    else:
                        accepted=publish_candidate(out,sc,result,a.preference,{'kind':'dayfirst','phase':phase,'solve_id':solve_id})
                        if accepted:
                            best=_snapshot_best(sc,read_best(out))
                            if checkpoint.get('first_score') is None:
                                checkpoint['first_score']=best['score_ticks']
                            if phase=='first':
                                atomic_json(out/'initial_solution.json',{k:v for k,v in result.items() if k not in ('schedule','audit')})
                            _message('已校验方案',f'首日锻打 {best["score_ticks"]/scale:g} 分钟；额外有效保温 {best["hold_excess_ticks"]/scale:g} 分钟。')
                            reason=stopper.tick(time.monotonic(),best['score_ticks'],best['hold_excess_ticks'])
                            if reason and not a.first_only:
                                request_stop(reason)
                        pending=None
                elif status=='infeasible':
                    if phase=='first':
                        state.update(phase='proven_infeasible',stop_reason='original_infeasible');break
                    if phase=='prefix':
                        state.update(phase='completed',stop_reason='global_proof',proof=True);break
                    # Only a PROVED unsuccessful complete continuation licenses
                    # this exact prefix exclusion. It stays sound as best improves.
                    excluded.append(pending);pending=None
                elif status=='filtered':
                    pending=None
                elif status=='invalid':
                    state['errors'].append(result.get('reason','invalid model or schedule'))
                    request_stop('invalid_result')
                elif status=='unknown':
                    if phase=='first' and deadline is not None and time.monotonic()>=deadline:
                        request_stop('first_deadline')
                    else:
                        request_stop(state['stop_reason'] or 'solver_unknown')
                save()
    except Exception as exc:
        with guard:
            state['errors'].append(f'{type(exc).__name__}: {exc}')
        request_stop('runtime_error')
    finally:
        done.set();stop.set();watcher.join(timeout=2)
        for sig,old_handler in signals.items():
            signal.signal(sig,old_handler)
        with guard:
            state['active']=False
            if state['errors']:
                state['phase']='failed'
            elif state['phase'] not in ('completed','proven_infeasible','first_solution_found'):
                if a.first_only and best:
                    state.update(phase='first_solution_found',stop_reason='first_solution')
                else:
                    state['phase']='stopped' if str(state['stop_reason']).startswith('stagnation') else 'paused'
            if best:
                atomic_json(out/'best_schedule.json',best['schedule'])
                atomic_json(out/'best_audit.json',best['audit'])
            save()
            final=progress()
            atomic_json(out/'report.json',final)
    _message('结束',f'{state["stop_reason"]}；'+('已保留完整合法方案。' if best else '暂未找到完整方案；除明确证明外不能判为无解。'))
    print(json.dumps(final,ensure_ascii=False),flush=True)
    return 2 if state['errors'] else 0
