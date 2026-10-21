"""Generated executable construction algorithms over immutable environment APIs.

The model owns its loops, frontier, candidate scope, branching and backtracking.
It cannot import, access files/network, mutate states or call the auditor.
This is a restricted Python language, not arbitrary Python or a mere rank hook.
"""
import ast
import copy
import hashlib
import inspect as pyinspect
import json
import sys
from pathlib import Path
from program import ALLOWED, BUILTINS
from calendar_engine import Calendar, SearchBudget

API_NAMES = {'root','inspect','candidates','advance','complete','probe','batches',
             'remaining','diagnostics','branch_status','checkpoint','restore'}
METHODS={'get','append','extend','pop','items','keys','values','copy','sort','add','discard'}
ALGORITHM_BUILTINS={**BUILTINS,'set':set,'tuple':tuple,'bool':bool}
DEFAULT_SOURCE = Path(__file__).with_name("default_algorithm.py").read_text()


def bounded_range(*args):
    r=range(*args)
    if len(r)>100000:raise ValueError('range exceeds per-call resource bound')
    return r


class AlgorithmProgram:
    def __init__(self, source):
        if not isinstance(source,str) or len(source)>20000:raise ValueError('algorithm_source must be <=20000 characters')
        tree=ast.parse(source)
        if not tree.body or any(not isinstance(n,ast.FunctionDef) for n in tree.body):raise ValueError('Algorithm must contain function definitions only')
        top_names={n.name for n in tree.body}
        if 'construct' not in top_names or len(top_names)!=len(tree.body):raise ValueError('Exactly one construct() entry required')
        names={n.name for n in ast.walk(tree) if isinstance(n,ast.FunctionDef)}
        entry=next(n for n in tree.body if n.name=='construct')
        if entry.args.args or entry.args.vararg or entry.args.kwarg:raise ValueError('Entry signature is construct()')
        allowed=ALLOWED | {ast.While,ast.Lambda,ast.Is,ast.IsNot,ast.Attribute,ast.Set,ast.SetComp}
        nodes=list(ast.walk(tree))
        if len(nodes)>3500:raise ValueError('Algorithm AST too large')
        for n in nodes:
            if type(n) not in allowed:raise ValueError('Forbidden algorithm syntax: '+type(n).__name__)
            if isinstance(n,ast.Name) and n.id.startswith('_'):raise ValueError('Private names forbidden')
            if isinstance(n,ast.Constant) and isinstance(n.value,str) and '__' in n.value:raise ValueError('Private keys forbidden')
            if isinstance(n,ast.FunctionDef):
                if n.decorator_list:raise ValueError('Decorated functions forbidden')
                if n.name.startswith('_') or n.name in API_NAMES or n.name in ALGORITHM_BUILTINS:raise ValueError('Cannot replace environment API or use private function names')
                if n not in tree.body and n.name=='construct':raise ValueError('Cannot shadow construct entry')
            if isinstance(n,ast.arg) and (n.arg.startswith('_') or n.arg in API_NAMES or n.arg in ALGORITHM_BUILTINS):raise ValueError('Cannot shadow environment API in function arguments')
            if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store) and (n.id in API_NAMES or n.id in ALGORITHM_BUILTINS):raise ValueError('Cannot shadow environment API')
            if isinstance(n,ast.Attribute) and n.attr not in METHODS:raise ValueError('Forbidden algorithm attribute '+n.attr)
            if isinstance(n,ast.Call):
                name_ok=isinstance(n.func,ast.Name) and n.func.id in (names|API_NAMES|set(ALGORITHM_BUILTINS))
                method_ok=isinstance(n.func,ast.Attribute) and n.func.attr in METHODS
                if not (name_ok or method_ok):raise ValueError('Unsupported algorithm call '+ast.unparse(n.func)+' at line '+str(n.lineno)+'; use the documented API or allowed Python helpers')
                if isinstance(n.func,ast.Name) and n.func.id in API_NAMES-{'checkpoint','restore'}:
                    try:
                        if any(k.arg is None for k in n.keywords):raise TypeError('expanded keyword arguments are unsupported')
                        pyinspect.signature(getattr(Calendar,n.func.id)).bind(None,*([None]*len(n.args)),**{k.arg:None for k in n.keywords})
                    except TypeError as e:
                        raise ValueError(f'Invalid API call {n.func.id} at line {n.lineno}: {e}') from e
        self.code=compile(tree,'<generated-algorithm>','exec')
        self.hash=hashlib.sha256(source.encode()).hexdigest()
        self.source=source

    def execute(self, env, checkpoint_path=None):
        path=Path(checkpoint_path) if checkpoint_path else None
        identity=hashlib.sha256(json.dumps(dict(sc=env.sc,source=self.hash,waves=env.pending_order,
            environment=hashlib.sha256(b''.join(Path(__file__).with_name(n).read_bytes() for n in ['calendar_engine.py','state_store.py','algorithm.py','time_windows.py','default_algorithm.py'])).hexdigest(),incumbent=env.incumbent,preference=env.preference),sort_keys=True).encode()).hexdigest()
        restored=None
        if path and path.exists():
            data=json.loads(path.read_text())
            if data['identity']!=identity:raise ValueError('Checkpoint algorithm/scenario mismatch')
            env.states.load(data['state_records']);env.next_id=data['next_id']
            env.action_sets={int(k):(v[0],v[1]) for k,v in data['actions'].items()};env.next_action=data['next_action']
            env.action_index={(sid,json.dumps({k:v for k,v in action.items() if k!='id'},sort_keys=True)):ident for ident,(sid,action) in env.action_sets.items()}
            env.last_advanced_sid=data.get('last_advanced_sid',0)
            restored=data['controller']
        saved=None;calls=0
        def checkpoint(controller):
            nonlocal saved,calls
            # Explicit serializable controller state, not an undocumented Python frame snapshot.
            checkpoint_start=__import__('time').perf_counter()
            encoded=json.dumps(controller)
            if len(encoded)>2000000:raise ValueError('Controller checkpoint too large')
            saved=json.loads(encoded);calls+=1
            if path and calls%16==0:flush()
            env.stats['checkpoint_controller_s']=env.stats.get('checkpoint_controller_s',0)+__import__('time').perf_counter()-checkpoint_start
            return True
        def flush():
            if path and saved is not None:
                path.parent.mkdir(parents=True,exist_ok=True)
                began=__import__('time').perf_counter()
                data=dict(identity=identity,algorithm_hash=self.hash,controller=saved,state_records=env.states.dump(),
                          actions=env.action_sets,next_id=env.next_id,next_action=env.next_action,
                          last_advanced_sid=env.last_advanced_sid,
                          semantics='planned reservations; never an observed production state')
                tmp=path.with_suffix('.tmp');blob=json.dumps(data);tmp.write_text(blob);tmp.replace(path)
                env.stats['checkpoint_bytes']=len(blob.encode())
                env.stats['checkpoint_write_s']=env.stats.get('checkpoint_write_s',0)+__import__('time').perf_counter()-began
        env.stats['algorithm_api_calls']={}
        def counted(name,fn):
            def call(*args,**kwargs):
                counts=env.stats['algorithm_api_calls'];counts[name]=counts.get(name,0)+1
                return fn(*args,**kwargs)
            return call
        api={n:counted(n,getattr(env,n)) for n in API_NAMES-{'checkpoint','restore'}}
        scope={'__builtins__':{**ALGORITHM_BUILTINS,'range':bounded_range},**api,
               'checkpoint':checkpoint,'restore':lambda:copy.deepcopy(restored)}
        exec(self.code,scope)
        steps=0
        import os, signal
        import resource
        # Linux virtual-memory allowance above the already imported runtime. No global machine setting.
        old_memory=resource.getrlimit(resource.RLIMIT_AS)
        memory_applied=False
        if sys.platform=='linux':
            current_vm=int(Path('/proc/self/statm').read_text().split()[0])*os.sysconf('SC_PAGE_SIZE')
            extra_mb=max(128,int(os.environ.get('LONGDA_ALGORITHM_MEMORY_MB','2048')))
            bound=current_vm+extra_mb*1024*1024
            if old_memory[0]!=resource.RLIM_INFINITY:bound=min(bound,old_memory[0])
            if old_memory[1]!=resource.RLIM_INFINITY:bound=min(bound,old_memory[1])
            resource.setrlimit(resource.RLIMIT_AS,(bound,old_memory[1]));memory_applied=True
        trace_mode=os.environ.get("LONGDA_ALGORITHM_TRACE","off")
        previous_alarm=signal.getsignal(signal.SIGALRM) if hasattr(signal,"SIGALRM") else None
        def timed_out(signum,frame):raise SearchBudget("construction_budget")
        def trace(frame,event,arg):
            nonlocal steps
            if frame.f_code.co_filename=='<generated-algorithm>':
                frame.f_trace_opcodes=True
                if event=='opcode':
                    steps+=1
                    if steps%2048==0:env.budget()
                    if steps>5000000:raise SearchBudget('algorithm_instruction_budget')
                return trace
            return None
        previous=sys.gettrace()
        try:
            if previous_alarm is not None:
                signal.signal(signal.SIGALRM,timed_out);signal.setitimer(signal.ITIMER_REAL,max(.001,env.remaining()))
            if trace_mode=="opcode":sys.settrace(trace)
            result=scope['construct']()
            if result is not None and (type(result)!=int or result not in env.states):raise ValueError('construct() must return a state handle or None')
            return result
        finally:
            if previous_alarm is not None:
                signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous_alarm)
            sys.settrace(previous)
            if memory_applied:resource.setrlimit(resource.RLIMIT_AS,old_memory)
            flush()
            env.stats["trace_mode"]=trace_mode


def construct_algorithm(sc,waves,algorithm_source=None,seconds=3,checkpoint_path=None,**kwargs):
    env=Calendar(sc,waves,seconds,kwargs.get('incumbent'),kwargs.get('preference','none'))
    program=AlgorithmProgram(algorithm_source or DEFAULT_SOURCE)
    error=None;sid=None
    try:
        sid=program.execute(env,checkpoint_path)
        if sid is None:error='day1_bound' if env.branch_results.get(0,{}).get('kind')=='BOUND_PRUNED' else 'algorithm_returned_no_complete_state'
        elif not env.complete(sid):error='algorithm_returned_partial_state'
    except SearchBudget as e:error=str(e)
    except MemoryError:error='memory_budget'
    result=env.result(sid) if error is None else None
    stats=env.diagnostics()
    stats['outcome_kind']='COMPLETE_CANDIDATE' if result is not None else ('BUDGET_EXHAUSTED' if error and 'budget' in error else ('BOUND_PRUNED' if error=='day1_bound' else 'NO_COMPLETE_CANDIDATE'))
    stats.update(engine='calendar',algorithm_hash=program.hash,
                 checkpoint_path=checkpoint_path,algorithm_source=program.source)
    return result,error,stats
