"""Small command-line boundary around the unchanged scheduling model."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time

from ortools.sat.python import cp_model

from .audit import audit
from .model import build_model, extract_schedule
from .prefix_model import project_schedule
from .solver_flow import atomic_json, publish_incumbent, read_best, solve_interruptibly, solver_statistics
from .result_summary import build_summary, format_summary


def _load_json(path):
    return json.loads(Path(path).read_text())


def _load_schedule(path):
    value = _load_json(path)
    if isinstance(value, dict) and 'schedule' in value:
        return value['schedule']
    return value


def audit_schedule(scenario_path, schedule_path):
    """Recompute feasibility and score; ignore any externally supplied audit."""
    scenario = _load_json(scenario_path)
    schedule = _load_schedule(schedule_path)
    report = audit(scenario, schedule)
    result = {'status': 'valid' if report['valid'] else 'invalid', 'audit': report}
    if report['valid']:
        day_end = 1440 * scenario['ticks_per_minute']
        score = project_schedule(scenario, schedule, day_end)['day_score_ticks']
        result.update(score_ticks=score, day_end_ticks=day_end,
                      first_day_minutes=score / scenario['ticks_per_minute'],
                      first_day_utilization_percent=100 * score / day_end)
    return result


def find_initial_solution(scenario, threads=2, stop_event=None, on_stage=None, solver_log_path=None):
    """Find and independently audit the first full schedule, without a cutoff.

    Used automatically by ``search`` when no incumbent exists. Returning
    UNKNOWN after interruption never licenses an infeasibility conclusion.
    Publication and run-directory locking remain the caller's responsibility.
    """
    if threads < 1:
        raise ValueError('threads must be positive')
    began = time.monotonic()
    result = dict(origin='cp_sat_first_solution', imported_existing_schedule=False,
                  search_time_limit_s=None, first_day_score_threshold=None,
                  threads=threads, build_s=0.0, model_build_s=0.0, solver_wall_s=0.0, audit_s=0.0,
                  num_booleans=0, branches=0, conflicts=0)
    if stop_event is not None and stop_event.is_set():
        return dict(result, status='unknown', cp_status='UNKNOWN',
                    reason='interrupted before initial solve', elapsed_s=time.monotonic() - began)
    if on_stage is not None:on_stage('building_model')
    build_begin = time.monotonic()
    model, variables, _ = build_model(scenario)
    result['build_s'] = time.monotonic() - build_begin
    result['model_build_s'] = result['build_s']
    result['model_variables'] = len(model.proto.variables)
    result['model_constraints'] = len(model.proto.constraints)
    if on_stage is not None:on_stage('solving',model_build_s=result['model_build_s'],
                                    model_variables=result['model_variables'],model_constraints=result['model_constraints'])
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = threads
    solver.parameters.symmetry_level = 0
    solver.parameters.stop_after_first_solution = True
    status = solve_interruptibly(solver, model, stop_event, log_path=solver_log_path,log_label='initial_solution')
    try:
        result['solver_wall_s'] = solver.wall_time
    except RuntimeError:
        pass
    result['cp_status'] = solver.status_name(status)
    result.update(solver_statistics(solver))
    if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        return dict(result, status=('infeasible' if status == cp_model.INFEASIBLE else
                                    'invalid' if status == cp_model.MODEL_INVALID else 'unknown'),
                    elapsed_s=time.monotonic() - began)
    schedule = extract_schedule(solver, variables)
    if on_stage is not None:on_stage('auditing',solver_wall_s=result['solver_wall_s'],
                                    num_booleans=result['num_booleans'],branches=result['branches'],conflicts=result['conflicts'])
    audit_begin = time.monotonic()
    report = audit(scenario, schedule)
    result['audit_s'] = time.monotonic() - audit_begin
    if not report['valid']:
        return dict(result, status='invalid', audit=report, schedule=schedule,
                    elapsed_s=time.monotonic() - began)
    day_end = 1440 * scenario['ticks_per_minute']
    score = project_schedule(scenario, schedule, day_end)['day_score_ticks']
    return dict(result, status='feasible', schedule=schedule, audit=report,
                score_ticks=score, first_solution_score_ticks=score, day_end_ticks=day_end,
                first_day_minutes=score / scenario['ticks_per_minute'],
                first_day_utilization_percent=100 * score / day_end,
                elapsed_s=time.monotonic() - began)


def create_baseline(scenario_path, out, schedule_path=None, threads=2):
    """Initialize a new run from one independently audited complete schedule.

    Without an imported schedule, solve the original full feasibility model.
    There is no first-day score threshold, optimization objective, time limit,
    or reused incumbent. An import is explicitly recorded as an import.
    """
    if threads < 1:
        raise ValueError('threads must be positive')
    began = time.monotonic()
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Search uses this same lock. Initializing a baseline cannot race a search
    # process or a second invocation initializing the same output directory.
    with (out / 'farm.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for filename in ('best.json', 'launch.json', 'baseline.json'):
            if (out / filename).exists():
                raise ValueError(f'{filename} already exists; baseline requires a new output directory')
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        load_begin = time.monotonic()
        scenario = _load_json(scenario_path)
        schedule = _load_schedule(schedule_path) if schedule_path is not None else None
        load_s = time.monotonic() - load_begin
        imported = schedule_path is not None
        result = dict(origin='imported_schedule' if imported else 'cp_sat_first_solution',
                      imported_existing_schedule=imported, search_time_limit_s=None,
                      first_day_score_threshold=None, threads=threads,
                      load_s=load_s, build_s=0.0, solver_wall_s=0.0, audit_s=0.0,
                      published=False)
        if not imported:
            build_begin = time.monotonic()
            model, variables, _ = build_model(scenario)
            result['build_s'] = time.monotonic() - build_begin
            solver = cp_model.CpSolver()
            solver.parameters.num_search_workers = threads
            solver.parameters.symmetry_level = 0
            solver.parameters.stop_after_first_solution = True
            status = solver.solve(model)
            result.update(cp_status=solver.status_name(status),
                          solver_wall_s=solver.wall_time)
            if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
                result.update(status=('infeasible' if status == cp_model.INFEASIBLE else
                                      'invalid' if status == cp_model.MODEL_INVALID else 'unknown'),
                              elapsed_s=time.monotonic() - began)
                atomic_json(out / 'baseline_report.json', result)
                return result
            schedule = extract_schedule(solver, variables)
        audit_begin = time.monotonic()
        report = audit(scenario, schedule)
        result.update(audit=report, audit_s=time.monotonic() - audit_begin)
        if not report['valid']:
            result.update(status='invalid', elapsed_s=time.monotonic() - began)
            atomic_json(out / 'baseline_report.json', result)
            return result
        day_end = 1440 * scenario['ticks_per_minute']
        score = project_schedule(scenario, schedule, day_end)['day_score_ticks']
        candidate = dict(status='feasible', score_ticks=score, day_end_ticks=day_end,
                         schedule=schedule, audit=report,
                         solver_wall_s=result['solver_wall_s'], origin=result['origin'],
                         imported_existing_schedule=imported,
                         first_day_minutes=score / scenario['ticks_per_minute'],
                         first_day_utilization_percent=100 * score / day_end)
        source = {'kind': result['origin'], 'scenario': str(Path(scenario_path).resolve())}
        if imported:
            source['schedule'] = str(Path(schedule_path).resolve())
        published = publish_incumbent(out, candidate, source)
        if not published:
            raise RuntimeError('The new baseline was not published')
        baseline = read_best(out)
        atomic_json(out / 'baseline.json', baseline)
        atomic_json(out / 'best_schedule.json', schedule)
        atomic_json(out / 'best_audit.json', report)
        result.update(status='feasible', score_ticks=score, day_end_ticks=day_end,
                      first_day_minutes=candidate['first_day_minutes'],
                      first_day_utilization_percent=candidate['first_day_utilization_percent'],
                      published=True, elapsed_s=time.monotonic() - began)
        atomic_json(out / 'baseline_report.json', result)
        return result


def read_status(out):
    """Read the saved progress and incumbent without changing a running job."""
    out = Path(out)
    progress_path = out / 'progress.json'
    progress = _load_json(progress_path) if progress_path.exists() else None
    best = read_best(out)
    summary = None if best is None else {
        key: best.get(key) for key in
        ('score_ticks', 'day_end_ticks', 'published_unix', 'source')}
    if best is not None and best.get('day_end_ticks'):
        summary['first_day_utilization_percent'] = 100 * best['score_ticks'] / best['day_end_ticks']
    return {'output_directory': str(out.resolve()), 'progress': progress,
            'best': summary, 'stop_requested': (out / 'STOP').exists()}


def summarize_run(out):
    """Re-audit saved input and schedule without starting a solver or changing a run."""
    out = Path(out).resolve()
    scenario = _load_json(out / 'scenario.json')
    best = read_best(out)
    if best is None:
        raise ValueError('No complete saved schedule; summary does not start a search')
    report = audit(scenario, best['schedule'])
    if not report['valid']:
        raise ValueError('Saved schedule failed independent audit; summary not produced')
    summary = build_summary(scenario, report)
    summary['output_directory'] = str(out)
    return summary


def _show_saved_summary(out, *, save=False):
    summary = summarize_run(out)
    if save:
        atomic_json(Path(out) / 'result_summary.json', summary)
    print(format_summary(summary), file=sys.stderr, flush=True)
    return summary


def _parser():
    parser = argparse.ArgumentParser(prog='longda-cpsat',
                                     description='Longda full-feasibility and day-first CP-SAT scheduling')
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('search', help='Continue the no-time-limit first-day search')
    subparsers.add_parser('first', help='Find and audit one complete schedule, then stop')
    baseline = subparsers.add_parser('baseline', help='Solve or import the first audited full schedule')
    baseline.add_argument('--scenario', type=Path, required=True)
    baseline.add_argument('--out', type=Path, required=True)
    baseline.add_argument('--schedule', type=Path)
    baseline.add_argument('--threads', type=int, default=2)
    check = subparsers.add_parser('audit', help='Independently check a full schedule and first-day score')
    check.add_argument('--scenario', type=Path, required=True)
    check.add_argument('--schedule', type=Path, required=True)
    status = subparsers.add_parser('status', help='Read saved progress and incumbent')
    status.add_argument('--out', type=Path, required=True)
    summary = subparsers.add_parser('summary', help='Show completion time and daily press utilization without searching')
    summary.add_argument('--out', type=Path, required=True)
    html = subparsers.add_parser('export-html', help='Export both HTML views of an existing audited result without searching')
    html.add_argument('--out', type=Path, required=True)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] in ('search', 'first'):
            route = argparse.ArgumentParser(add_help=False)
            route.add_argument('--strategy', choices=('legacy', 'shared', 'dayfirst'))
            selection, remaining = route.parse_known_args(argv[1:])
            new_flags = ('--preference', '--first-time-limit', '--trial-seconds',
                         '--max-trial-seconds', '--max-trials', '--allocation',
                         '--directions', '--no-early-stop', '--no-gain-rounds',
                         '--small-gain-rounds', '--min-gain-minutes',
                         '--min-hold-gain-minutes', '--baseline', '--exploration', '--seed')
            new_flags += ('--gap-minutes',)
            requested_shared = any(a.split('=')[0] in new_flags for a in remaining)
            strategy = selection.strategy or ('shared' if requested_shared else 'legacy')
            if strategy == 'dayfirst':
                from .dayfirst_search import main as search_main
            elif strategy == 'shared':
                from .shared_search import main as search_main
            else:
                if requested_shared:
                    raise ValueError('New search options require --strategy shared; legacy remains unchanged')
                from .search import main as search_main
            code = search_main(remaining + (['--first-only'] if argv[0] == 'first' else []))
            # Keep search and constraints unchanged. Report only after a successful
            # return with an audited incumbent, including a user-paused search.
            if code == 0:
                output_parser = argparse.ArgumentParser(add_help=False)
                output_parser.add_argument('--out', type=Path, required=True)
                output_args, _ = output_parser.parse_known_args(argv[1:])
                if read_best(output_args.out) is not None:
                    _show_saved_summary(output_args.out, save=True)
                    if argv[0] == 'first':
                        # Presentation only, once after first has returned. Other
                        # search commands must not acquire automatic HTML work.
                        from .html_export import export_run_html
                        views = export_run_html(output_args.out)
                        print('[HTML] 订单视角：' + views['order_view'], file=sys.stderr, flush=True)
                        print('[HTML] 设备视角：' + views['equipment_view'], file=sys.stderr, flush=True)
            return code
        args = _parser().parse_args(argv)
        if args.command == 'baseline':
            result = create_baseline(args.scenario, args.out, args.schedule, args.threads)
            code = 0 if result['status'] == 'feasible' else 1
        elif args.command == 'audit':
            result = audit_schedule(args.scenario, args.schedule)
            code = 0 if result['audit']['valid'] else 1
        elif args.command == 'summary':
            result = summarize_run(args.out)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            print(format_summary(result), file=sys.stderr, flush=True)
            return 0
        elif args.command == 'export-html':
            from .html_export import export_run_html
            result = export_run_html(args.out)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            print('[HTML] 订单视角：' + result['order_view'], file=sys.stderr, flush=True)
            print('[HTML] 设备视角：' + result['equipment_view'], file=sys.stderr, flush=True)
            return 0
        else:
            result = read_status(args.out)
            code = 0
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return code
    except (ValueError, OSError) as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({'status': 'interrupted', 'message': 'No infeasibility conclusion is inferred.'}),
              file=sys.stderr)
        return 130
