"""Audited, bounded-attempt search shared by first feasibility and improvement.

Legacy prefix enumeration lives in search.py and remains independently usable.
This coordinator never relaxes the physical model. Its only persistent search
state is tasks, hints, verified schedules and strategy statistics; a CP-SAT
search tree is NOT serialized. No heuristic termination is an optimality proof.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import time
import traceback

from .audit import audit
from .model import flatten, ticks
from .objectives import evaluate_schedule, objective_key
from .search_tasks import DIRECTIONS, run_task
from .search_policy import AdaptivePolicy, NeighborhoodSizer, PlateauStop
from .search import upper_bound
from .solver_flow import atomic_json, read_best


def message(label, text):
    print(f'[{label}] {text}', file=sys.stderr, flush=True)


def load_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def publish_candidate(outdir, scenario, candidate, preference, source):
    """Recompute full feasibility and both objective values before atomic commit.

    Comparing with CURRENT best protects against workers finishing out of order.
    A forged audit or cached score is never trusted here.
    """
    if candidate.get('status') != 'feasible' or not candidate.get('schedule'):
        return False
    checked = audit(scenario, candidate['schedule'])
    if not checked['valid']:
        raise ValueError('Candidate failed independent publication audit')
    measured = evaluate_schedule(scenario, candidate['schedule'])
    for key in ('score_ticks', 'hold_excess_ticks'):
        if key in candidate and candidate[key] != measured[key]:
            raise ValueError(f'Candidate {key} disagrees with independent recomputation')
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'incumbent.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read_best(out)
        if previous is not None:
            previous = dict(previous, **evaluate_schedule(scenario, previous['schedule']))
            if objective_key(measured, preference) <= objective_key(previous, preference):
                return False
        record = dict(candidate, **measured)
        record.update(audit=checked, preference=preference, source=source,
                      day_end_ticks=1440 * scenario['ticks_per_minute'],
                      published_unix=time.time(),
                      previous_score_ticks=previous['score_ticks'] if previous else None,
                      generation=(previous.get('generation', 0) + 1) if previous else 0)
        atomic_json(out / 'improvements' / f'{time.time_ns()}-{record["score_ticks"]}.json', record)
        atomic_json(out / 'best.json', record)
    return True


def _worker(sc, task, incumbent, preference, threads, budget, stop, path, log_path):
    try:
        result = run_task(sc, task, incumbent, preference=preference,
                          threads=threads, budget_s=budget, stop_event=stop, log_path=log_path)
    except BaseException:
        result = dict(status='invalid', reason=traceback.format_exc(), elapsed_s=0)
    atomic_json(path, result)


def parser():
    p = argparse.ArgumentParser(prog='longda-cpsat first/search --strategy shared',
                                description='Shared full-model, audited adaptive search')
    p.add_argument('--scenario', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--processes', type=int, default=4)
    p.add_argument('--threads', type=int, default=2)
    p.add_argument('--transport-mode', choices=('none', 'vehicles'))
    p.add_argument('--vehicles', type=int)
    p.add_argument('--preference', choices=('none', 'hold-near-min'), default='none')
    p.add_argument('--first-time-limit', type=float, help='Total wall seconds to first solution, e.g. 600; timeout is UNKNOWN')
    p.add_argument('--trial-seconds', type=float, default=30, help='Initial per-attempt build+solve budget, not whole search limit')
    p.add_argument('--max-trial-seconds', type=float, default=240, help='Cap on growing per-attempt budget after UNKNOWN')
    p.add_argument('--max-trials', type=int, help='Optional launch-count limit per invocation; useful for short verification')
    p.add_argument('--min-free-gib', type=float, default=10)
    p.add_argument('--allocation', choices=('adaptive', 'round-robin'), default='adaptive')
    p.add_argument('--directions', default=','.join(DIRECTIONS))
    p.add_argument('--exploration', type=float, default=.2)
    p.add_argument('--seed', type=int, default=0)
    p.add_argument('--no-early-stop', action='store_true')
    p.add_argument('--no-gain-rounds', type=int, default=3)
    p.add_argument('--small-gain-rounds', type=int, default=3)
    p.add_argument('--min-gain-minutes', type=float, default=5)
    p.add_argument('--min-hold-gain-minutes', type=float, default=5)
    p.add_argument('--gap-minutes', type=float, help='Optional acceptable gap to reliable GLOBAL day-one upper bound; not a proof of exact optimality')
    p.add_argument('--baseline', type=Path, help='Import and independently audit a complete schedule into a new shared run')
    p.add_argument('--first-only', action='store_true')
    p.add_argument('--solver-log', action='store_true')
    return p


def _validate(a):
    a.directions = tuple(a.directions.split(','))
    if not a.directions or len(set(a.directions)) != len(a.directions) or set(a.directions) - set(DIRECTIONS):
        raise ValueError('directions must be a nonempty, non-repeating subset of ' + ','.join(DIRECTIONS))
    for name in ('processes', 'threads', 'no_gain_rounds', 'small_gain_rounds'):
        if getattr(a, name) < 1:
            raise ValueError(name + ' must be positive')
    for name in ('trial_seconds', 'max_trial_seconds', 'first_time_limit'):
        value = getattr(a, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError(name + ' must be finite and positive')
    if a.max_trial_seconds < a.trial_seconds:
        raise ValueError('max-trial-seconds must be >= trial-seconds')
    if a.max_trials is not None and a.max_trials < 1:
        raise ValueError('max-trials must be positive')
    for name in ('min_gain_minutes', 'min_hold_gain_minutes', 'min_free_gib'):
        if not math.isfinite(getattr(a, name)) or getattr(a, name) < 0:
            raise ValueError(name + ' must be finite and nonnegative')
    if not 0 < a.exploration <= 1:
        raise ValueError('exploration must be in (0,1]')
    if a.gap_minutes is not None and (not math.isfinite(a.gap_minutes) or a.gap_minutes < 0):
        raise ValueError('gap-minutes must be finite and nonnegative')
    if a.vehicles is not None and a.vehicles < 1:
        raise ValueError('vehicles must be positive')
    if a.transport_mode == 'none' and a.vehicles is not None:
        raise ValueError('--vehicles cannot be combined with --transport-mode none')


def effective_scenario(a):
    sc = json.loads(a.scenario.read_text())
    if a.transport_mode is not None or a.vehicles is not None:
        cfg = sc.setdefault('transport', {})
        cfg['enabled'] = a.transport_mode != 'none'
        cfg.setdefault('vehicle_count', 1)
        cfg.setdefault('travel_minutes', 5)
        cfg.setdefault('initial_location', 'cold')
        if a.vehicles is not None:
            cfg['vehicle_count'] = a.vehicles
    return sc


def _semantic_settings(a):
    return {key: getattr(a, key) for key in ('preference', 'allocation', 'exploration', 'seed',
            'no_early_stop', 'no_gain_rounds', 'small_gain_rounds', 'min_gain_minutes',
            'min_hold_gain_minutes', 'gap_minutes')} | {'directions': list(a.directions)}


def _resume_command(a, out):
    parts = [sys.executable, '-m', 'longda_cpsat', 'first' if a.first_only else 'search',
             '--strategy', 'shared', '--scenario', str(out / 'scenario.json'), '--out', str(out)]
    for key in ('processes', 'threads', 'preference', 'trial_seconds', 'max_trial_seconds',
                'min_free_gib', 'allocation', 'exploration', 'seed', 'no_gain_rounds',
                'small_gain_rounds', 'min_gain_minutes', 'min_hold_gain_minutes'):
        parts += ['--' + key.replace('_', '-'), str(getattr(a, key))]
    parts += ['--directions', ','.join(a.directions)]
    if a.first_time_limit is not None:
        parts += ['--first-time-limit', str(a.first_time_limit)]
    if a.max_trials is not None:
        parts += ['--max-trials', str(a.max_trials)]
    if a.gap_minutes is not None:
        parts += ['--gap-minutes', str(a.gap_minutes)]
    for key in ('solver_log', 'no_early_stop'):
        if getattr(a, key):
            parts += ['--' + key.replace('_', '-')]
    return shlex.join(parts)


def _snapshot_best(sc, record):
    if record is None:
        return None
    checked = audit(sc, record['schedule'])
    if not checked['valid']:
        raise ValueError('Existing schedule does not satisfy the complete model')
    values = evaluate_schedule(sc, record['schedule'])
    if record.get('score_ticks', values['score_ticks']) != values['score_ticks']:
        raise ValueError('Saved objective differs from independently recomputed score')
    result = dict(record, **values)
    result.update(status='feasible', audit=checked)
    result.setdefault('generation', 0)
    return result


def main(argv=None):
    a = parser().parse_args(argv)
    _validate(a)
    started = time.monotonic()
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'farm.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.seek(0); lock.truncate(); lock.write(str(os.getpid())); lock.flush()
        return _run(a, out, started)


def _run(a, out, started):
    sc = effective_scenario(a)
    scale = sc['ticks_per_minute']
    D = 1440 * scale
    UB = upper_bound(sc, D)
    settings = _semantic_settings(a)
    saved_launch = load_json(out / 'launch.json')
    digest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(Path(__file__).parent.glob('*.py'))}
    # Validate compatibility before overwriting any saved input/state.
    if saved_launch:
        if saved_launch.get('strategy') != 'shared':
            raise ValueError('Use a NEW output directory and --baseline to import a legacy run')
        if saved_launch.get('settings') != settings:
            raise ValueError('Search preference/settings changed; use a new output directory with --baseline')
        if saved_launch.get('source_sha256') != digest:
            raise ValueError('Frozen source changed; use a new output directory with --baseline')
    saved_sc = load_json(out / 'scenario.json')
    if saved_sc is not None and saved_sc != sc:
        raise ValueError('Effective scenario differs from saved run')
    best = _snapshot_best(sc, read_best(out))
    reused = best is not None
    if a.baseline is not None:
        if saved_launch or best:
            raise ValueError('--baseline requires a new output directory')
        raw = load_json(a.baseline)
        if raw is None:
            raise ValueError('Baseline file not found')
        source = raw.get('schedule', raw)
        imported = _snapshot_best(sc, {'schedule': source})
    else:
        imported = None
    prior = load_json(out / 'progress.json', {}).get('elapsed_s', 0)
    atomic_json(out / 'scenario.json', sc)
    atomic_json(out / 'launch.json', dict(strategy='shared', settings=settings,
                source_sha256=digest, pid=os.getpid(), processes=a.processes,
                threads_per_process=a.threads, first_search_time_limit_s=a.first_time_limit,
                search_time_limit_s=None, per_attempt_budget_s=a.trial_seconds,
                first_only=a.first_only, resume_command=_resume_command(a, out),
                started_unix=time.time(), upper_bound_ticks=UB))
    if imported:
        publish_candidate(out, sc, imported, a.preference, {'kind': 'import', 'path': str(a.baseline)})
        best = _snapshot_best(sc, read_best(out))
        reused = True
    checkpoint = load_json(out / 'checkpoint.json', {})
    policies = {phase: AdaptivePolicy.from_dict(checkpoint['policies'][phase])
                if phase in checkpoint.get('policies', {}) else
                AdaptivePolicy(a.directions, seed=a.seed + (1 if phase == 'improve' else 0), exploration=a.exploration)
                for phase in ('first', 'improve')}
    sizes = {d: NeighborhoodSizer.from_dict(checkpoint['sizes'][d])
             if d in checkpoint.get('sizes', {}) else NeighborhoodSizer()
             for d in a.directions}
    budgets = checkpoint.get('budgets', {d: a.trial_seconds for d in a.directions})
    budgets = {d: min(a.max_trial_seconds, max(a.trial_seconds, budgets.get(d, a.trial_seconds))) for d in a.directions}
    stopper = PlateauStop(a.directions, no_gain_rounds=a.no_gain_rounds,
                small_gain_rounds=a.small_gain_rounds,
                min_gain_ticks=ticks(a.min_gain_minutes, scale),
                hold_min_gain_ticks=ticks(a.min_hold_gain_minutes, scale),
                preference=a.preference, enabled=not a.no_early_stop)
    if checkpoint.get('stopper'):
        stopper = PlateauStop.from_dict(checkpoint['stopper'])
        # An explicit restart after heuristic stopping starts a fresh observation
        # window, while retaining allocation learning and the audited incumbent.
        if stopper.reason and best:
            stopper.start(best['score_ticks'], best['hold_excess_ticks'], best['generation'])
    elif best:
        stopper.start(best['score_ticks'], best['hold_excess_ticks'], best['generation'])
    pending = checkpoint.get('pending', [])
    seq = checkpoint.get('sequence', 0)
    rr = checkpoint.get('round_robin', 0)
    completed = checkpoint.get('completed_trials', 0)
    rewarded = set(checkpoint.get('rewarded_candidates', []))
    first_score = checkpoint.get('first_score', best['score_ticks'] if best else None)
    initial_status = 'feasible' if best else 'unknown'
    ctx = mp.get_context('spawn')
    active = {}
    launched = 0
    errors = []
    stop_reason = None
    proof = False
    force_stop = False
    user_stop = False
    old_signals = {}
    def interrupt(*_):
        nonlocal user_stop
        user_stop = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        old_signals[sig] = signal.signal(sig, interrupt)
    first_deadline = started + a.first_time_limit if a.first_time_limit is not None and best is None else None
    message('共享搜索', f'{len(sc["orders"])} 单 / {len(flatten(sc))} 火次；{a.processes} 个进程 × {a.threads} 线程；偏好 {a.preference}。')
    message('规则', '每次都使用完整炉、压机、车辆和四天后续约束；完整校验通过才更新方案。')
    if first_deadline is not None:
        message('首解上限', f'{a.first_time_limit:g} 秒，包含本次启动、建模及尝试；到时未找到是 UNKNOWN，不是无解。')
    message('额度', f'单次尝试从 {a.trial_seconds:g} 秒开始，可增长至 {a.max_trial_seconds:g} 秒；整场优化没有固定时间上限。')
    last_write = last_message = 0
    final_report = None

    def progress():
        elapsed = prior + time.monotonic() - started
        return dict(strategy='shared', phase='finding_initial_solution' if best is None else 'running',
                    preference=a.preference, optimal=proof,
                    primary_optimal=bool(best and best['score_ticks'] >= UB),
                    best_score_ticks=best['score_ticks'] if best else None,
                    best_hold_excess_ticks=best['hold_excess_ticks'] if best else None,
                    best_day_minutes=best['score_ticks'] / scale if best else None,
                    best_utilization_percent=100 * best['score_ticks'] / D if best else None,
                    best_audit_valid=bool(best and best['audit']['valid']),
                    first_solution_score_ticks=first_score, initial_solution_status=initial_status,
                    upper_bound_ticks=UB, active_processes=len(active), completed_trials=completed,
                    gap_minutes=(UB-best['score_ticks'])/scale if best else None,
                    launched_this_invocation=launched, pending_trials=sum(p['generation'] == (best['generation'] if best else -1) for p in pending),
                    superseded_unknown_trials=sum(p['generation'] != (best['generation'] if best else -1) for p in pending),
                    first_search_time_limit_s=a.first_time_limit, search_time_limit_s=None,
                    elapsed_s=elapsed, updated_unix=time.time(), errors=list(errors),
                    reused_existing_incumbent=reused, stop_reason=stop_reason,
                    allocation=a.allocation,
                    probabilities=(policies['first' if best is None else 'improve'].probabilities()
                                   if a.allocation == 'adaptive' else None),
                    next_round_robin_direction=a.directions[rr] if a.allocation == 'round-robin' else None,
                    active_tasks=[{'id': t['task']['id'], 'direction': t['task']['direction'],
                                   'generation': t['generation'], 'budget_s': t['budget'],
                                   'elapsed_s': time.monotonic()-t['started']} for t in active.values()])

    def save_checkpoint():
        atomic_json(out / 'checkpoint.json', dict(
            sequence=seq, round_robin=rr, completed_trials=completed, first_score=first_score,
            policies={k: v.to_dict() for k, v in policies.items()},
            sizes={k: v.to_dict() for k, v in sizes.items()}, budgets=budgets,
            stopper=stopper.to_dict(), pending=pending,
            rewarded_candidates=sorted(rewarded),
            interrupted_tasks=[{'task': t['task'], 'incumbent': t['incumbent'],
                                'budget': t['budget'], 'generation': t['generation']}
                               for t in active.values()],
            native_solver_tree_saved=False))

    # Restore descriptors of interrupted work; they are retried, not considered proofs.
    pending.extend(checkpoint.get('interrupted_tasks', []))
    try:
        while True:
            now = time.monotonic()
            if user_stop or (out / 'STOP').exists():
                stop_reason = 'user_stop'; force_stop = True
            elif shutil.disk_usage(out).free / 2**30 < a.min_free_gib:
                stop_reason = 'disk_guard'; force_stop = True
            elif best is None and first_deadline is not None and now >= first_deadline:
                stop_reason = 'first_deadline'; force_stop = True
            elif best is not None and a.first_only:
                stop_reason = 'first_solution'; force_stop = True
            elif best is not None and best['score_ticks'] >= UB and (a.preference == 'none' or best['hold_excess_ticks'] == 0):
                stop_reason = 'bound_reached'; proof = True; force_stop = True
            elif best is not None and a.gap_minutes is not None and (UB-best['score_ticks'])/scale <= a.gap_minutes:
                stop_reason = 'bound_gap'; force_stop = True
            if stop_reason and force_stop:
                for job in active.values():
                    job['stop'].set()
            # Read completed results before checking launch count, so the last result is kept.
            for ident, job in list(active.items()):
                proc = job['process']
                overdue = now - job['started'] > job['budget'] + 2
                if force_stop or overdue or 'stop_requested_at' in job:
                    job['stop'].set()
                    if now - job.setdefault('stop_requested_at', now) > .35 and proc.is_alive():
                        proc.terminate()
                if proc.is_alive():
                    continue
                proc.join()
                result = load_json(job['result_path'])
                active.pop(ident)
                if result is None:
                    if not (force_stop or overdue or 'stop_requested_at' in job):
                        errors.append(f'{ident}: worker exited {proc.exitcode} without result')
                        stop_reason = 'worker_error'; force_stop = True
                    result = dict(status='unknown', reason='interrupted worker; no infeasibility proof',
                                  elapsed_s=time.monotonic()-job['started'])
                cost = max(time.monotonic()-job['started'], 1e-6)
                status = result.get('status')
                direction = job['task']['direction']
                phase = 'first' if job['incumbent'] is None else 'improve'
                before = best
                accepted = False
                # A result obtained after the explicit first deadline is not a timely first solution.
                late = best is None and first_deadline is not None and time.monotonic() >= first_deadline
                duplicate_reward = False
                skip_first = a.first_only and best is not None
                if status == 'feasible' and not late and not skip_first:
                    accepted = publish_candidate(out, sc, result, a.preference,
                        {'kind': 'shared', 'direction': direction, 'task': ident, 'launch_generation': job['generation']})
                    if accepted:
                        best = _snapshot_best(sc, read_best(out))
                        initial_status = 'feasible'
                        if first_score is None:
                            first_score = best['score_ticks']
                            stopper.start(best['score_ticks'], best['hold_excess_ticks'], best['generation'])
                            atomic_json(out / 'initial_solution.json', dict(
                                status='feasible', origin='shared_cp_sat_first_solution',
                                first_solution_score_ticks=first_score, first_day_score_threshold=None,
                                search_time_limit_s=a.first_time_limit, elapsed_s=time.monotonic()-started,
                                direction=direction, preference=a.preference,
                                note='First complete feasible solution, not a proof of preference optimum'))
                        message('已校验方案', f'首日锻打 {best["score_ticks"]/scale:.3f} 分钟；额外有效保温 {best["hold_excess_ticks"]/scale:.3f} 分钟；方向 {direction}。')
                        if a.first_only:
                            stop_reason = 'first_solution'; force_stop = True
                if status == 'invalid':
                    errors.append(f'{ident}: '+str(result.get('reason', 'invalid model or audit')))
                    stop_reason = 'invalid_candidate'; force_stop = True
                reward = 0.0
                if status == 'feasible' and not late and not skip_first:
                    measured = evaluate_schedule(sc, result['schedule'])
                    fingerprint = hashlib.sha256(json.dumps(
                        {k: result['schedule'][k] for k in ('heat_start', 'forge_start', 'furnace')},
                        sort_keys=True).encode()).hexdigest()
                    reward_id = f'{phase}:{job["generation"]}:{fingerprint}'
                    duplicate_reward = reward_id in rewarded
                    if phase == 'first':
                        reward = 1.0
                    else:
                        base = job['incumbent']
                        if measured['score_ticks'] > base['score_ticks']:
                            reward = (measured['score_ticks']-base['score_ticks']) / scale
                        elif a.preference == 'hold-near-min' and measured['score_ticks'] == base['score_ticks']:
                            # Bounded secondary credit; never changes lex publication ordering.
                            reward = min(.99, max(0, base['hold_excess_ticks']-measured['hold_excess_ticks']) /
                                         max(1, base['hold_excess_ticks']))
                    if duplicate_reward:
                        reward = 0.0
                    elif reward > 0:
                        rewarded.add(reward_id)
                policies[phase].observe(direction, reward, cost, generation=job['generation'], status=status)
                sizes[direction].observe(reward > 0)
                if status == 'unknown':
                    budgets[direction] = min(a.max_trial_seconds, max(a.trial_seconds, job['budget'] * 2))
                    pending.append({'task': job['task'], 'incumbent': job['incumbent'],
                                    'generation': job['generation'], 'budget': budgets[direction]})
                elif status == 'feasible':
                    budgets[direction] = a.trial_seconds
                completed += 1
                row = {k: v for k, v in result.items() if k not in ('schedule', 'audit')}
                row.update(task=job['task'], phase=phase, launch_generation=job['generation'],
                           cost_s=cost, accepted=accepted, reward=reward, late_first_result=late,
                           duplicate_reward=duplicate_reward,
                           current_generation=best['generation'] if best else None)
                with (out / 'trials.jsonl').open('a') as journal:
                    journal.write(json.dumps(row, ensure_ascii=False)+'\n')
                if not force_stop:
                    kind = result.get('proof_kind')
                    if kind == 'original_infeasible' and best is None:
                        initial_status = 'infeasible'; stop_reason = 'global_proof'; proof = False; force_stop = True
                    elif kind == 'no_improvement_global' and best is not None:
                        stop_reason = 'global_proof'; proof = True; force_stop = True
                    elif phase == 'improve' and best:
                        reason = stopper.observe(direction, best['score_ticks'], best['hold_excess_ticks'],
                                                 job['generation'], improved=accepted)
                        if reason:
                            stop_reason = reason
                job['result_path'].unlink(missing_ok=True)
                # Feasibility workers become stale as soon as a first incumbent exists.
                if before is None and best is not None:
                    for stale in active.values():
                        if stale['incumbent'] is None:
                            stale['stop'].set()
                            stale['stop_requested_at'] = time.monotonic()
            # Gracefully stop accepting new attempts on heuristic stop; in-flight results may improve.
            if stop_reason and not active:
                break
            if a.max_trials is not None and launched >= a.max_trials:
                if not active:
                    stop_reason = stop_reason or 'max_trials'
                    break
            while not stop_reason and len(active) < a.processes and (a.max_trials is None or launched < a.max_trials):
                phase = 'first' if best is None else 'improve'
                counts = Counter(j['task']['direction'] for j in active.values())
                cap = max(1, math.ceil(a.processes / len(a.directions)))
                excluded = [d for d, n in counts.items() if n >= cap]
                if len(excluded) == len(a.directions):
                    break
                if a.allocation == 'adaptive':
                    direction = policies[phase].choose(excluded=excluded)
                else:
                    choices = [a.directions[(rr+j) % len(a.directions)] for j in range(len(a.directions))]
                    direction = next(d for d in choices if d not in excluded)
                    rr = (a.directions.index(direction)+1) % len(a.directions)
                gen = best['generation'] if best else -1
                seq += 1
                ident = f'trial-{seq:07d}'
                task = {'id': ident, 'direction': direction, 'seed': a.seed+seq,
                        'fraction': sizes[direction].current_fraction, 'generation': gen}
                budget = budgets[direction]
                # Every fifth launch retries a saved UNKNOWN for this phase/direction/current incumbent.
                if seq % 5 == 0:
                    for index, unresolved in enumerate(pending):
                        if unresolved['generation'] == gen and unresolved['task']['direction'] == direction:
                            old_task = unresolved['task']
                            task.update(seed=old_task['seed'], fraction=old_task['fraction'], retry_of=old_task['id'])
                            budget = unresolved['budget']; pending.pop(index)
                            break
                if best is None and first_deadline is not None:
                    budget = min(budget, first_deadline-time.monotonic())
                    if budget <= 0:
                        stop_reason = 'first_deadline'; force_stop = True
                        break
                event = ctx.Event()
                result_path = out / 'worker_results' / (ident+'.json')
                result_path.parent.mkdir(exist_ok=True)
                proc = ctx.Process(target=_worker, args=(sc, task, best, a.preference, a.threads,
                                   budget, event, str(result_path), str(out / 'solver.log') if a.solver_log else None))
                job = dict(process=proc, stop=event, task=task, incumbent=best,
                           generation=gen, budget=budget, result_path=result_path, started=time.monotonic())
                active[ident] = job
                proc.start(); launched += 1
            now = time.monotonic()
            if now-last_write >= 1:
                atomic_json(out / 'progress.json', progress()); save_checkpoint(); last_write = now
            if now-last_message >= 5:
                label = '找首解' if best is None else f'最好首日 {best["score_ticks"]/scale:.3f} 分钟'
                message('运行', f'{label}；累计 {prior+now-started:.1f} 秒；完成 {completed} 次尝试，运行中 {len(active)} 次；暂未找到不等于无解。')
                last_message = now
            time.sleep(.03)
    finally:
        for job in active.values():
            job['stop'].set()
        for job in active.values():
            job['process'].join(timeout=.4)
            if job['process'].is_alive():
                job['process'].terminate(); job['process'].join(timeout=2)
        save_checkpoint()
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)
    if best:
        best = _snapshot_best(sc, read_best(out))
        atomic_json(out / 'best_schedule.json', best['schedule'])
        atomic_json(out / 'best_audit.json', best['audit'])
        # The final allowed attempt can reach the bound immediately before the
        # launch-count branch exits. Recheck instead of reporting a false gap.
        if not a.first_only and not errors and best['score_ticks'] >= UB and (a.preference == 'none' or best['hold_excess_ticks'] == 0):
            proof = True
            if stop_reason in ('max_trials', 'stagnation_no_gain', 'stagnation_small_gain', 'bound_gap'):
                stop_reason = 'bound_reached'
    final_report = progress()
    if errors:
        phase = 'failed'
    elif initial_status == 'infeasible' and best is None:
        phase = 'proven_infeasible'
    elif a.first_only and best:
        phase = 'first_solution_found'; proof = False
    elif proof:
        phase = 'completed'
    elif stop_reason and (stop_reason.startswith('stagnation') or stop_reason == 'bound_gap'):
        phase = 'stopped'
    else:
        phase = 'paused'
    final_report.update(phase=phase, optimal=proof, active_processes=0, active_tasks=[],
                        stop_reason=stop_reason, initial_solution_status=initial_status)
    atomic_json(out / 'progress.json', final_report)
    atomic_json(out / 'report.json', final_report)
    if best is None:
        atomic_json(out / 'initial_solution.json', dict(status=initial_status,
                    search_time_limit_s=a.first_time_limit, elapsed_s=time.monotonic()-started,
                    reason=stop_reason, native_solver_tree_saved=False))
    message('结束', f'{stop_reason}；'+('完整方案已保存并校验。' if best else
            '当前完整模型已证明无解。' if initial_status == 'infeasible' else '暂未找到完整方案，不能据此断言无解。'))
    print(json.dumps(final_report, ensure_ascii=False), flush=True)
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
