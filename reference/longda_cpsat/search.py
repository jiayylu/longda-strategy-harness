"""No-time-limit parallel day-first improvement with exact full continuation."""
import argparse
import fcntl
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import signal
import shlex
import sys
import threading
import time
import traceback
from ortools.sat.python import cp_model
from .model import flatten, ramp_ticks, ticks
from .prefix_model import build_prefix_model, make_prefix_shards, extract_prefix, exclude_prefix, project_schedule
from .solver_flow import atomic_json, read_best, complete_prefix, publish_incumbent, solve_interruptibly, solver_statistics
from .audit import audit
from .transport import transport_enabled

CORE_FILES=('model.py','prefix_model.py','solver_flow.py','search.py','audit.py',
            'transport.py','transport_audit.py','cli.py')


def _message(label, text):
    print(f'[{label}] {text}',file=sys.stderr,flush=True)


class InitialProgress:
    """One writer lock for stage updates and timed progress during native solve."""
    def __init__(self,path,started,prior_elapsed=0,threads=1,heartbeat_s=5,write_s=1):
        self.path=Path(path);self.started=started;self.prior_elapsed=prior_elapsed
        self.threads=threads;self.heartbeat_s=heartbeat_s;self.write_s=write_s
        self.lock=threading.RLock();self.stage='initializing';self.stage_started=time.monotonic()
        self.stage_started_unix=time.time();self.last_write=0;self.last_message=time.monotonic()
        self.fields={'model_build_s':0.0}

    def _record(self,now):
        record=dict(phase='finding_initial_solution',stage=self.stage,
                    stage_started_unix=self.stage_started_unix,stage_elapsed_s=now-self.stage_started,
                    elapsed_s=self.prior_elapsed+now-self.started,updated_unix=time.time(),
                    threads=self.threads,first_solution_score_ticks=None,best_score_ticks=None,
                    optimal=False,search_time_limit_s=None,errors=[],**self.fields)
        if self.stage=='building_model':record['model_build_s']=now-self.stage_started
        record['model_build_complete']=self.stage in ('solving','auditing')
        return record

    def update(self,stage,**fields):
        with self.lock:
            now=time.monotonic();self.stage=stage;self.stage_started=now;self.stage_started_unix=time.time()
            self.fields.update(fields)
            atomic_json(self.path,self._record(now));self.last_write=now
            if stage=='building_model':_message('建模','正在构建订单、炉子、压机及转运约束；尚未开始 CP-SAT 求解。')
            elif stage=='solving':_message('CP-SAT求解',f"建模 {self.fields['model_build_s']:.2f} 秒；{self.fields.get('model_variables',0):,} 个变量，{self.fields.get('model_constraints',0):,} 个约束；使用 {self.threads} 线程，开始求解（包含预处理与搜索）。")
            elif stage=='auditing':_message('独立校验','求解器已返回完整候选，正在检查全部工艺、设备与转运约束；通过后才保存为有效方案。')

    def pulse(self):
        with self.lock:
            now=time.monotonic()
            if now-self.last_write>=self.write_s:
                atomic_json(self.path,self._record(now));self.last_write=now
            if now-self.last_message>=self.heartbeat_s:
                labels={'initializing':'初始化','building_model':'建模','solving':'CP-SAT求解（含预处理）','auditing':'独立校验'}
                detail='候选正在校验' if self.stage=='auditing' else '尚未找到完整合法排程，程序仍在运行'
                _message('运行',f"累计 {self.prior_elapsed+now-self.started:.1f} 秒；当前 {labels[self.stage]} 已运行 {now-self.stage_started:.1f} 秒；{detail}。")
                self.last_message=now

def upper_bound(sc,D):
    ops=flatten(sc)
    first={}
    for q in ops:
        first.setdefault(q['order'],q)
    tau=ticks(sc['transport'].get('travel_minutes',5),sc['ticks_per_minute']) if transport_enabled(sc) else 0
    earliest=min(max(q['release']+tau,ramp_ticks(sc,sc['initial_temperature'],q['temp']))+q['lower']+tau for q in first.values())
    return min(D,max(0,D-earliest),sum(q['duration'] for q in ops))

class FirstPrefix(cp_model.CpSolverSolutionCallback):
    def __init__(self,v,D):
        super().__init__()
        self.v=v; self.D=D; self.prefix=None
    def on_solution_callback(self):
        self.prefix=extract_prefix(self,self.v,self.D)
        self.stop_search()


def worker(sc,shard,outstr,threads,stop,solver_log=False):
    out=Path(outstr); here=out/shard['id']; here.mkdir(parents=True,exist_ok=True)
    path=here/'progress.json'
    state=json.loads(path.read_text()) if path.exists() else {}
    if state.get('phase')=='exhausted':
        return
    for key in ('prefix_candidates','screened_before_suffix','suffix_calls','suffix_infeasible','suffix_unknown','improvements','prefix_solver_s','suffix_solver_s'):
        state.setdefault(key,0)
    state.update(shard=shard,pid=os.getpid(),phase='building',stage_started_unix=time.time(),error=None)
    done=threading.Event()
    def heartbeat():
        while not done.wait(2):
            atomic_json(path,dict(state,updated_unix=time.time()))
    pulse=threading.Thread(target=heartbeat,daemon=True);pulse.start()
    try:
        D=1440*sc['ticks_per_minute']; best=read_best(out)
        build_begin=time.monotonic()
        m,v,ops=build_prefix_model(sc,D,best['score_ticks'],shard)
        state['prefix_model_build_s']=time.monotonic()-build_begin
        m.maximize(v['day_total'])
        active_threshold=best['score_ticks']
        journal=here/'infeasible_prefixes.jsonl'
        if journal.exists():
            for line in journal.read_text().splitlines():
                exclude_prefix(m,v,json.loads(line))
        pendingfile=here/'pending_prefix.json'
        pending=json.loads(pendingfile.read_text()) if pendingfile.exists() else None
        iteration=0
        while not stop.is_set():
            best=read_best(out)
            if best['score_ticks']>=upper_bound(sc,D):
                state['phase']='bound_reached';break
            if best['score_ticks']>active_threshold:
                m.add(v['day_total']>best['score_ticks'])
                active_threshold=best['score_ticks']
            if pending is None:
                m.clear_hints()
                hint=project_schedule(ops,best['schedule'],D)
                for k in ('heat_start','forge_start','furnace'):
                    for variable,value in zip(v[k],hint[k]):
                        m.add_hint(variable,value)
                state.update(phase='searching_first_day',threshold_ticks=best['score_ticks'],stage_started_unix=time.time())
                solver=cp_model.CpSolver()
                solver.parameters.num_search_workers=threads
                solver.parameters.symmetry_level=0
                solver.parameters.random_seed=int(shard['id'].split('-')[-1])+iteration*97
                cb=FirstPrefix(v,D)
                status=solve_interruptibly(solver,m,stop,cb,
                        log_path=out/'solver.log' if solver_log else None,log_label=shard['id']+' first_day')
                try: state['prefix_solver_s']+=solver.wall_time
                except RuntimeError: pass
                state['prefix_cp_status']=solver.status_name(status)
                state['prefix_solver_stats']=solver_statistics(solver)
                if status==cp_model.INFEASIBLE:
                    state.update(phase='exhausted',exhausted_above_ticks=best['score_ticks']);break
                if status==cp_model.MODEL_INVALID:
                    raise RuntimeError(solver.response_stats())
                if cb.prefix is None:
                    if stop.is_set():break
                    raise RuntimeError('Prefix solver returned UNKNOWN without an external interruption')
                pending=cb.prefix
                state['prefix_candidates']+=1
                atomic_json(pendingfile,pending)
            latest=read_best(out)
            state.update(phase='checking_continuation',candidate_score_ticks=pending['day_score_ticks'],stage_started_unix=time.time())
            # A later incumbent from another process can make this prefix stale.
            if pending['day_score_ticks']>latest['score_ticks']:
                state['suffix_calls']+=1
            result=complete_prefix(sc,pending,latest['score_ticks'],workers=threads,stop_event=stop,hint=latest['schedule'],
                                   solver_log_path=out/'solver.log' if solver_log else None)
            state['suffix_solver_s']+=result['solver_wall_s']
            state['last_suffix_status']=result['status']
            state['last_suffix_model_build_s']=result.get('model_build_s',0.0)
            state['last_suffix_solver_stats']=result.get('solver_stats',{})
            if result['status']=='filtered':
                state['screened_before_suffix']+=1
            elif result['status']=='suffix_infeasible':
                state['suffix_infeasible']+=1
                # Only a proof of no full extension licenses this exclusion.
                with journal.open('a') as file:
                    file.write(json.dumps(pending,separators=(',',':'))+'\n')
                    file.flush();os.fsync(file.fileno())
                exclude_prefix(m,v,pending)
            elif result['status']=='feasible':
                if publish_incumbent(out,result,shard['id']):
                    state['improvements']+=1
            elif result['status']=='unknown':
                state['suffix_unknown']+=1
                # Keep pending on disk; resume it instead of incorrectly pruning.
                if stop.is_set():break
                raise RuntimeError('Suffix returned UNKNOWN without an external interruption')
            else:
                atomic_json(here/'invalid_candidate.json',result)
                raise RuntimeError('Invalid continuation result: '+str(result.get('reason',result['status'])))
            pendingfile.unlink(missing_ok=True)
            pending=None
            iteration+=1
        if state['phase'] not in ('exhausted','bound_reached'):
            state['phase']='paused'
    except Exception:
        state.update(phase='error',error=traceback.format_exc())
        stop.set()
    finally:
        done.set();pulse.join()
        atomic_json(path,dict(state,updated_unix=time.time()))


def main(argv=None):
    ap=argparse.ArgumentParser(prog='longda-cpsat search')
    ap.add_argument('--scenario',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--processes',type=int,default=24)
    ap.add_argument('--threads',type=int,default=2)
    ap.add_argument('--min-free-gib',type=float,default=10)
    ap.add_argument('--transport-mode',choices=('none','vehicles'))
    ap.add_argument('--vehicles',type=int)
    ap.add_argument('--first-only',action='store_true',help='Stop after the first independently audited complete schedule')
    ap.add_argument('--solver-log',action='store_true',help='Write detailed native CP-SAT diagnostics to OUT/solver.log')
    args=ap.parse_args(argv)
    if args.processes<1 or args.threads<1:raise ValueError('parallelism must be positive')
    started=time.monotonic()
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    lock=(out/'farm.lock').open('a+')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    lock.seek(0);lock.truncate();lock.write(str(os.getpid()));lock.flush()
    oldprogress=json.loads((out/'progress.json').read_text()) if (out/'progress.json').exists() else {}
    prior_elapsed=oldprogress.get('elapsed_s',0)
    sc=json.loads(args.scenario.read_text())
    if args.vehicles is not None and args.vehicles<1:raise ValueError('vehicles must be positive')
    if args.transport_mode=='none' and args.vehicles is not None:
        raise ValueError('--vehicles cannot be combined with --transport-mode none')
    if args.transport_mode is not None or args.vehicles is not None:
        config=sc.setdefault('transport',{})
        config['enabled']=args.transport_mode!='none'
        config.setdefault('vehicle_count',1)
        config.setdefault('travel_minutes',5)
        config.setdefault('initial_location','cold')
        if args.vehicles is not None:config['vehicle_count']=args.vehicles
    enabled=transport_enabled(sc)
    movement=f"{sc['transport'].get('vehicle_count',1)} 辆转运车，异地行驶 {sc['transport'].get('travel_minutes',5)} 分钟" if enabled else '零运输模式'
    _message('输入',f"{len(sc['orders'])} 个订单、{len(flatten(sc))} 火次；{sc['furnace_count']} 台炉，每炉 {sc['capacity']} 支；1 台压机；{movement}。")
    later='只求首个合法方案后退出' if args.first_only else f'后续 {args.processes} 个进程 × 每进程 {args.threads} 线程优化'
    _message('资源',f'首解使用 {args.processes*args.threads} 个 CPU 线程；{later}；搜索不设时限。')
    if args.solver_log:_message('日志',f'详细 CP-SAT 日志写入 {out / "solver.log"}，不会逐行刷屏。')
    if isinstance(sc.get('scope'),dict):
        sc['scope']['transport']=(f"{sc['transport'].get('vehicle_count',1)} finite single-piece vehicles; "
                                 f"uniform {sc['transport'].get('travel_minutes',5)}-minute loaded/empty travel") if enabled else 'zero-duration transport; unconstrained vehicles (legacy mode)'
        sc['scope']['same_time_capacity']='ranked handovers; unload before pickup; capacity checked between events' if enabled else 'physical half-open resource intervals; exits before entries'
    D=1440*sc['ticks_per_minute'];UB=upper_bound(sc,D)
    baseline=read_best(out)
    reused_existing_incumbent=baseline is not None
    if baseline is not None:
        _message('独立校验','发现已有完整排程，正在重新校验；这次不算从零找到首解。')
        audit_result=audit(sc,baseline['schedule'])
        if not audit_result['valid'] or project_schedule(sc,baseline['schedule'],D)['day_score_ticks']!=baseline['score_ticks']:
            raise ValueError('Existing incumbent fails independent full audit or score')
    effective_path=out/'scenario.json'
    if effective_path.exists():
        if json.loads(effective_path.read_text())!=sc:
            raise ValueError('Effective scenario differs from the saved run; use a new output directory')
    else:
        atomic_json(effective_path,sc)
    args.scenario=effective_path
    digest={p:hashlib.sha256((Path(__file__).parent/p).read_bytes()).hexdigest() for p in CORE_FILES}
    digest['scenario.json']=hashlib.sha256(args.scenario.read_bytes()).hexdigest()
    launchfile=out/'launch.json'
    previous=json.loads(launchfile.read_text()) if launchfile.exists() else None
    if previous and previous['source_sha256']!=digest:raise ValueError('Frozen source changed; use a new output directory')
    resume_args=[sys.executable,'-m','longda_cpsat','search','--scenario',args.scenario.resolve(),
                 '--out',out,'--processes',args.processes,'--threads',args.threads,'--min-free-gib',args.min_free_gib]
    if args.first_only:resume_args.append('--first-only')
    if args.solver_log:resume_args.append('--solver-log')
    launch=dict(source_sha256=digest,started_unix=previous['started_unix'] if previous else time.time(),
                attempts=(previous.get('attempts',0) if previous else 0)+1,pid=os.getpid(),
                processes=args.processes,threads_per_process=args.threads,search_time_limit_s=None,
                first_only=args.first_only,solver_log=args.solver_log,day_end_ticks=D,upper_bound_ticks=UB,
                resume_command=' '.join(shlex.quote(str(arg)) for arg in resume_args))
    atomic_json(launchfile,launch)
    ctx=mp.get_context('spawn');stop=ctx.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    if baseline is None:
        from .cli import find_initial_solution
        initial_errors=[]
        initial_done=threading.Event()
        initial_progress=InitialProgress(out/'progress.json',started,prior_elapsed,args.processes*args.threads)
        def initial_stop_check():
            while not initial_done.is_set():
                if (out/'STOP').exists():stop.set()
                if shutil.disk_usage(out).free/2**30<args.min_free_gib:
                    if 'disk_guard' not in initial_errors:initial_errors.append('disk_guard')
                    stop.set()
                initial_progress.pulse()
                if initial_done.wait(.25):break
        watcher=threading.Thread(target=initial_stop_check,daemon=True)
        if (out/'STOP').exists():stop.set()
        watcher.start()
        try:
            initial=find_initial_solution(sc,threads=args.processes*args.threads,stop_event=stop,
                     on_stage=initial_progress.update,solver_log_path=out/'solver.log' if args.solver_log else None)
        finally:
            initial_done.set();watcher.join()
        initial_report={key:value for key,value in initial.items() if key!='schedule'}
        atomic_json(out/'initial_solution.json',initial_report)
        if initial['status']!='feasible':
            phase='paused' if initial['status']=='unknown' else 'infeasible' if initial['status']=='infeasible' else 'failed'
            report=dict(phase=phase,initial_solution_status=initial['status'],best_score_ticks=None,
                        first_solution_score_ticks=None,optimal=False,search_time_limit_s=None,
                        model_build_s=initial.get('model_build_s',0.0),solver_wall_s=initial.get('solver_wall_s',0.0),
                        num_booleans=initial.get('num_booleans',0),branches=initial.get('branches',0),conflicts=initial.get('conflicts',0),
                        elapsed_s=prior_elapsed+time.monotonic()-started,
                        stop_requested=(out/'STOP').exists(),errors=initial_errors,updated_unix=time.time())
            atomic_json(out/'progress.json',report);atomic_json(out/'report.json',report)
            if phase=='paused':_message('暂停','尚未找到完整合法排程；已响应停止请求，UNKNOWN 不代表无解。')
            elif phase=='infeasible':_message('结束','CP-SAT 已证明当前完整模型无可行排程。')
            else:_message('错误','求解或独立校验未通过，请查看 report.json。')
            print(json.dumps(report,ensure_ascii=False),flush=True)
            return 0 if initial['status']=='unknown' and not initial_errors else 1
        if not publish_incumbent(out,initial,{'kind':'cp_sat_first_solution','scenario':str(args.scenario)}):
            raise RuntimeError('Initial independently audited solution could not be published')
        baseline=read_best(out)
        _message('完成',f"首个完整排程已通过独立校验并保存；建模 {initial['model_build_s']:.2f} 秒，求解 {initial['solver_wall_s']:.2f} 秒，首日锻打 {initial['first_day_minutes']:.2f} 分钟。")
    audit_result=audit(sc,baseline['schedule'])
    if not audit_result['valid'] or project_schedule(sc,baseline['schedule'],D)['day_score_ticks']!=baseline['score_ticks']:
        raise ValueError('Existing incumbent fails independent full audit or score')
    initial_path=out/'initial_solution.json'
    if not initial_path.exists():
        atomic_json(initial_path,dict(status='feasible',origin='existing_audited_incumbent',
                    first_solution_score_ticks=baseline['score_ticks'],audit=audit_result,
                    note='Existing schedule loaded; its original solve time is not measured here.'))
    first_record=json.loads(initial_path.read_text())
    first_score=first_record.get('first_solution_score_ticks',baseline['score_ticks'])
    initial_metrics={key:first_record.get(key) for key in ('model_build_s','solver_wall_s','num_booleans','branches','conflicts')}
    if args.first_only:
        report=dict(phase='first_solution_found',optimal=False,first_only=True,
                    reused_existing_incumbent=reused_existing_incumbent,
                    first_solution_score_ticks=first_score,best_score_ticks=baseline['score_ticks'],
                    best_day_minutes=baseline['score_ticks']/sc['ticks_per_minute'],
                    best_utilization_percent=100*baseline['score_ticks']/D,
                    best_audit_valid=True,active_processes=0,search_time_limit_s=None,
                    first_day_score_threshold=None,elapsed_s=prior_elapsed+time.monotonic()-started,
                    stop_requested=(out/'STOP').exists(),updated_unix=time.time(),errors=[],**initial_metrics)
        atomic_json(out/'progress.json',report);atomic_json(out/'report.json',report)
        atomic_json(out/'best_schedule.json',baseline['schedule']);atomic_json(out/'best_audit.json',audit_result)
        if reused_existing_incumbent:_message('完成','已有完整排程重新校验通过，已复用保存；未重新搜索，也未声称最优。')
        print(json.dumps(report,ensure_ascii=False),flush=True)
        return 0
    shards=make_prefix_shards(sc)
    for shard in shards:
        saved=out/shard['id']/'progress.json'
        if saved.exists():
            record=json.loads(saved.read_text())
            if record.get('phase')=='exhausted' and record['exhausted_above_ticks']>baseline['score_ticks']:
                raise ValueError('Incumbent was rolled back below a saved exhaustion proof; use a fresh output directory')
    todo=[s for s in shards if not (out/s['id']/'progress.json').exists() or json.loads((out/s['id']/'progress.json').read_text()).get('phase')!='exhausted']
    active={};errors=[];last_progress=None
    last_human_update=time.monotonic()
    _message('搜索','继续尝试提高首日锻打时间；每个改进都需要完整后续排程通过独立校验。')
    while True:
        best=read_best(out)
        if (out/'STOP').exists():stop.set()
        if best['score_ticks']>=UB:stop.set()
        free=shutil.disk_usage(out).free/2**30
        if free<args.min_free_gib:
            if 'disk_guard' not in errors:errors.append('disk_guard')
            stop.set()
        while todo and len(active)<args.processes and not stop.is_set():
            s=todo.pop(0);p=ctx.Process(target=worker,args=(sc,s,str(out),args.threads,stop,args.solver_log));p.start();active[s['id']]=p
        for name,p in list(active.items()):
            if not p.is_alive():
                p.join();active.pop(name)
                if p.exitcode!=0:errors.append(f'{name} process exit {p.exitcode}');stop.set()
        states=[]
        for s in shards:
            p=out/s['id']/'progress.json'
            if p.exists():states.append(json.loads(p.read_text()))
        for s in states:
            if s.get('error') and s['error'] not in errors:errors.append(s['error']);stop.set()
        exhausted=sum(s.get('phase')=='exhausted' for s in states)
        proven=best['score_ticks']>=UB or exhausted==len(shards)
        metrics={k:sum(s.get(k,0) for s in states) for k in ('prefix_candidates','screened_before_suffix','suffix_calls','suffix_infeasible','suffix_unknown','improvements','prefix_solver_s','suffix_solver_s')}
        stage_counts={name:sum(s.get('phase')==name for s in states) for name in ('building','searching_first_day','checking_continuation')}
        last_progress=dict(phase='running',optimal=proven,all_shards_exhausted=exhausted==len(shards),
            first_solution_score_ticks=first_score,
            best_score_ticks=best['score_ticks'],best_day_minutes=best['score_ticks']/sc['ticks_per_minute'],
            best_utilization_percent=100*best['score_ticks']/D,
            upper_bound_ticks=best['score_ticks'] if exhausted==len(shards) else UB,
            gap_minutes=0 if proven else (UB-best['score_ticks'])/sc['ticks_per_minute'],
            active_processes=len(active),shards_total=len(shards),shards_exhausted=exhausted,pending_shards=len(todo),
            elapsed_s=prior_elapsed+time.monotonic()-started,updated_unix=time.time(),
            search_time_limit_s=None,stop_requested=(out/'STOP').exists(),disk_free_gib=free,errors=errors,
            stage_counts=stage_counts,model_build_s=first_record.get('model_build_s'),**metrics)
        last_progress['initial_solver_stats']=initial_metrics
        atomic_json(out/'progress.json',last_progress)
        if time.monotonic()-last_human_update>=5:
            _message('运行',f"累计 {last_progress['elapsed_s']:.1f} 秒；建模 {stage_counts['building']} 个分片，首日搜索 {stage_counts['searching_first_day']} 个，完整后续校验 {stage_counts['checking_continuation']} 个；当前最好首日锻打 {last_progress['best_day_minutes']:.2f} 分钟。")
            last_human_update=time.monotonic()
        if not active and (not todo or stop.is_set()):break
        time.sleep(1)
    finalbest=read_best(out);finalaudit=audit(sc,finalbest['schedule'])
    if not finalaudit['valid']:errors.append('final incumbent audit failed')
    finaloptimal=finalbest['score_ticks']>=UB or last_progress['all_shards_exhausted']
    last_progress.update(best_score_ticks=finalbest['score_ticks'],
        best_day_minutes=finalbest['score_ticks']/sc['ticks_per_minute'],
        best_utilization_percent=100*finalbest['score_ticks']/D,optimal=finaloptimal,
        upper_bound_ticks=finalbest['score_ticks'] if last_progress['all_shards_exhausted'] else UB,
        gap_minutes=0 if finaloptimal else (UB-finalbest['score_ticks'])/sc['ticks_per_minute'],
        phase='failed' if errors else ('completed' if finaloptimal else 'paused'),
        best_audit_valid=finalaudit['valid'],errors=errors)
    atomic_json(out/'progress.json',last_progress);atomic_json(out/'report.json',last_progress)
    atomic_json(out/'best_schedule.json',finalbest['schedule']);atomic_json(out/'best_audit.json',finalaudit)
    _message('完成' if finaloptimal else '暂停',f"完整方案已保存，首日锻打 {last_progress['best_day_minutes']:.2f} 分钟；累计 {last_progress['elapsed_s']:.1f} 秒。")
    print(json.dumps(last_progress,ensure_ascii=False),flush=True)
    return 1 if errors else 0

if __name__=='__main__':raise SystemExit(main())
