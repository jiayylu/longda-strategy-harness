"""Independent witness construction for the unchanged two-car CP-SAT rules.

Both loaded and empty journeys are five minutes. One chosen car delivers and
immediately collects each forging job at its end; other cars can work in
parallel. Cold batches are a construction choice, never a physical constraint.
"""
import copy
import json
import random
import time
from collections import defaultdict
from pathlib import Path
from longda_cpsat.audit import audit
from longda_cpsat.model import flatten, ramp_ticks
from longda_cpsat.objectives import evaluate_schedule


def construct(sc, waves, seed=0, policy=0):
    rng=random.Random(seed);ops=flatten(sc);n=len(ops);tau=15
    by=defaultdict(list)
    for q in ops:by[q['order']].append(q['index'])
    s={k:[None]*n for k in ['heat_start','forge_start','furnace','heat_end','forge_end']}
    cars=[[0,0] for _ in range(2)]
    furn=[{'temp':sc['initial_temperature'],'exit':0,'res':set()} for _ in range(6)]
    tasks=[];frontier=0;press=7;finished=8
    def move(kind,i,src,dst,a,car):
        tasks.append(dict(kind=kind,order=ops[i]['order'],op=i,source=src,destination=dst,
                          start=a,end=a+tau,vehicle=car,pickup_rank=0,dropoff_rank=0))
    def ready(f,temp,res=None,exit_=None):
        row=furn[f];r=row['res'] if res is None else res
        if r:return 0 if row['temp']==temp and len(r)<5 else None
        return (row['exit'] if exit_ is None else exit_)+ramp_ticks(sc,row['temp'],temp)
    for wave in waves:
        active=set()
        # Spread arrivals as little as possible, batching each route on a
        # compatible furnace, while explicitly booking every car movement.
        for order in wave:
            i=by[order][0];q=ops[i];opts=[]
            for car,(at,pos) in enumerate(cars):
                for f in range(6):
                    r=ready(f,q['temp'])
                    if r is None:continue
                    arrival=max(frontier+tau,at+(0 if pos==0 else tau)+tau,q['release']+tau,r)
                    opts.append((arrival,not bool(furn[f]['res']),f,car))
            if not opts:return None,'cold_receiver'
            arrival,_,f,car=min(opts)
            move('cold_to_furnace',i,0,f+1,arrival-tau,car);cars[car]=[arrival,f+1]
            s['heat_start'][i]=arrival;s['furnace'][i]=f
            furn[f]['temp']=q['temp'];furn[f]['res'].add(i);active.add(i)
        steps=0
        while active:
            opts=[]
            for i in sorted(active):
                q=ops[i];f=s['furnace'][i];a=s['heat_start'][i]
                chain=by[q['order']];ix=chain.index(i);j=chain[ix+1] if ix+1<len(chain) else None
                for car,(at,pos) in enumerate(cars):
                    st=max(frontier,a+q['lower']+tau,at+(0 if pos==f+1 else tau)+tau)
                    destinations=range(6) if j is not None else [-1]
                    for dest in destinations:
                        cur=st
                        if dest>=0:
                            res=furn[dest]['res']-{i}
                            if res:
                                if furn[dest]['temp']!=ops[j]['temp'] or len(res)>=5:continue
                            elif dest==f:
                                # Source becomes empty at pickup=st-tau.
                                ramp=ramp_ticks(sc,q['temp'],ops[j]['temp'])
                                if ramp>q['duration']+2*tau:continue
                            else:
                                r=ready(dest,ops[j]['temp'])
                                if r is None:continue
                                cur=max(cur,r-q['duration']-tau)
                        if cur-tau>a+q['upper']:continue
                        end=cur+q['duration']
                        if any(end-tau>s['heat_start'][other]+ops[other]['upper'] for other in active if other!=i):continue
                        deadline=a+q['upper']+tau
                        idle=max(0,cur-frontier)
                        remaining=deadline-cur
                        # Different useful deterministic scores; seeded noise
                        # diversifies ties only, physical timing stays exact.
                        if policy==0:rank=cur+0.15*remaining
                        elif policy==1:rank=cur+0.5*remaining
                        elif policy==2:rank=deadline+0.1*idle
                        elif policy==3:rank=cur+0.05*remaining-0.5*q['duration']
                        elif policy==4:rank=cur+0.25*remaining-0.5*ix
                        else:rank=cur+0.3*remaining+rng.uniform(-60,60)
                        # Prefer retaining scarce empty furnaces where possible.
                        rank+=0 if dest<0 or furn[dest]['res']-{i} else 2
                        opts.append((rank,cur,i,car,dest,j))
            if not opts:return None,('deadend',steps,len(active),frontier/3)
            opts.sort(key=lambda x:x[0])
            if seed and policy<5:
                best=opts[0][0];near=[x for x in opts if x[0]<=best+18]
                picked=rng.choice(near[:8])
            else:picked=opts[0]
            _,st,i,car,dest,j=picked;q=ops[i];f=s['furnace'][i];end=st+q['duration']
            s['forge_start'][i]=st;s['forge_end'][i]=end;s['heat_end'][i]=st-tau
            move('furnace_to_press',i,f+1,press,st-tau,car)
            move('press_to_furnace' if j is not None else 'press_to_finished',i,press,dest+1 if j is not None else finished,end,car)
            cars[car]=[end+tau,dest+1 if j is not None else finished]
            furn[f]['res'].remove(i);furn[f]['exit']=max(furn[f]['exit'],st-tau)
            active.remove(i)
            if j is not None:
                s['heat_start'][j]=end+tau;s['furnace'][j]=dest
                furn[dest]['temp']=ops[j]['temp'];furn[dest]['res'].add(j);active.add(j)
            frontier=end;steps+=1
        frontier=max(frontier,max(c[0] for c in cars))
    # Original format orders transport tasks by material route, not time.
    kind_order={'cold_to_furnace':0,'furnace_to_press':1,'press_to_furnace':2,'press_to_finished':2}
    tasks.sort(key=lambda t:(t['op'],kind_order[t['kind']]))
    for k,t in enumerate(tasks):t['id']=k
    # Resolve zero-duration handovers by actual availability, so no full car
    # can pick up and no full press/furnace can receive an extra piece.
    events=defaultdict(list)
    for t in tasks:
        events[t['start']].append((True,t));events[t['end']].append((False,t))
    occupants={f:set() for f in range(1,9)};loaded=[None,None];rank=0
    for at,ev in sorted(events.items()):
        while ev:
            progressed=False
            for index,(pickup,t) in enumerate(ev):
                car=t['vehicle'];order=t['order'];loc=t['source'] if pickup else t['destination']
                if pickup:
                    ok=loaded[car] is None and (loc==0 or order in occupants[loc])
                else:
                    ok=loaded[car]==order and (loc==8 or len(occupants[loc])<(1 if loc==7 else 5))
                if not ok:continue
                t['pickup_rank' if pickup else 'dropoff_rank']=rank;rank+=1
                if pickup:
                    loaded[car]=order
                    if loc:occupants[loc].remove(order)
                else:
                    loaded[car]=None;occupants[loc].add(order)
                ev.pop(index);progressed=True;break
            if not progressed:return None,('handover_cycle',at)
    # Canonical same-route labels are symmetry only; relabel the actual paths
    # in first-forge order and permute operation arrays consistently.
    mapping={};routes=defaultdict(list)
    for o in sc['orders']:routes[(o['route'],o.get('release_minutes',0),o['due_minutes'])].append(o['id'])
    for names in routes.values():
        ranked=sorted(names,key=lambda name:s['forge_start'][by[name][0]])
        for old,new in zip(ranked,names):mapping[old]=new
    out={k:[None]*n for k in s};index_map={}
    for old,new in mapping.items():
        for i,j in zip(by[old],by[new]):
            index_map[i]=j
            for k in s:out[k][j]=s[k][i]
    for t in tasks:t['order']=mapping[t['order']];t['op']=index_map[t['op']]
    tasks.sort(key=lambda t:(t['op'],kind_order[t['kind']]))
    for k,t in enumerate(tasks):t['id']=k
    out['transport']=tasks
    return out,None


if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('--scenario',type=Path,required=True);ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--trials',type=int,default=120);args=ap.parse_args()
    sc=json.loads(args.scenario.read_text());sc['transport']['vehicle_count']=2
    args.out.mkdir(parents=True,exist_ok=True)
    groups=defaultdict(list)
    for o in sc['orders']:groups[o['route']].append(o['id'])
    strategies=[['C430','C280R'],['C430','C280I'],['C280R','C280I'],['C430','CFLAT'],['C280R','CFLAT'],['C280I','CFLAT']]
    best=-1;history=[];begin=time.monotonic()
    for trial in range(args.trials):
        selected=strategies[(trial//6)%len(strategies)]
        tail=sum((y for x,y in groups.items() if x not in selected),[])
        tail_size=[10,4,3,2,5][(trial//72)%5]
        waves=[sum((groups[x] for x in selected),[])]+[tail[k:k+tail_size] for k in range(0,len(tail),tail_size)]
        if (trial//36)%2:waves=[sorted(w,key=lambda x:(x[-2:],x)) for w in waves]
        plan,err=construct(sc,waves,seed=trial//360,policy=trial%6)
        row=dict(trial=trial,waves=waves,policy=trial%6,error=err)
        if plan is not None:
            checked=audit(sc,plan);values=evaluate_schedule(sc,plan)
            row.update(valid=checked['valid'],day_minutes=values['score_ticks']/3)
            if not checked['valid']:row['audit_errors']=checked.get('errors',[])[:3]
            if checked['valid'] and values['score_ticks']>best:
                best=values['score_ticks']
                for name,obj in [('scenario.json',sc),('best_schedule.json',plan),('best_audit.json',checked),('construction.json',row)]:
                    (args.out/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2))
                print('BEST',json.dumps(row),flush=True)
        history.append(row)
        if best>=2160:break
    (args.out/'trials.json').write_text(json.dumps(history,ensure_ascii=False,indent=2))
    print(json.dumps({'trials':len(history),'seconds':time.monotonic()-begin,'best_day_minutes':best/3,'complete':best>=2160,'errors':history[-2:]}),flush=True)
