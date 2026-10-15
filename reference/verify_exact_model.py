"""Evaluate a saved witness against every generated full CP-SAT constraint.

No Solve() call: auxiliary values are derived from the actual schedule. This
complements, rather than replaces, the original independent physical audit.
"""
import json
import re
from collections import Counter,defaultdict
from pathlib import Path
from longda_cpsat.model import build_model,flatten
from longda_cpsat.audit import audit


def verify(sc,s):
    checked=audit(sc,s)
    if not checked['valid']:raise ValueError('Original audit failed')
    m,v,ops=build_model(sc);tasks=sorted(s['transport'],key=lambda x:x['id']);base=2*len(tasks)+1
    incoming,outgoing,pin,pout={},{},{},{};by=defaultdict(list)
    for q in ops:by[q['order']].append(q['index'])
    for t in tasks:
        i=t['op'];t['_pe']=t['start']*base+t['pickup_rank'];t['_de']=t['end']*base+t['dropoff_rank']
        if t['kind']=='cold_to_furnace':incoming[i]=t
        if t['kind']=='furnace_to_press':outgoing[i]=pin[i]=t
        if t['kind'].startswith('press_to_'):
            pout[i]=t
            if t['kind']=='press_to_furnace':incoming[by[t['order']][by[t['order']].index(i)+1]]=t
    A,S,F,E,X=(s[k] for k in ['heat_start','forge_start','furnace','forge_end','heat_end'])
    def value(name):
        match=re.fullmatch(r'(same_car|car_before|same_furnace|before|in)(\d+)_(\d+)',name)
        if match:
            k,i,j=match.group(1),int(match.group(2)),int(match.group(3))
            return int({'same_car':lambda:tasks[i]['vehicle']==tasks[j]['vehicle'],
                        'car_before':lambda:tasks[i]['_pe']<tasks[j]['_pe'],
                        'same_furnace':lambda:F[i]==F[j],'before':lambda:A[i]<=A[j],'in':lambda:F[i]==j}[k]())
        match=re.fullmatch(r'(.+?)(\d+)',name)
        if not match:raise ValueError('Unknown variable '+name)
        k,i=match.group(1),int(match.group(2))
        if k in ['pickup_rank','dropoff_rank','vehicle']:return tasks[i][k]
        if k in ['pickup_event','dropoff_event']:return tasks[i]['_pe' if k=='pickup_event' else '_de']
        return {'a':lambda:A[i],'s':lambda:S[i],'f':lambda:F[i],'e':lambda:E[i],'x':lambda:X[i],
                'h':lambda:X[i]-A[i],'heat_event_duration':lambda:outgoing[i]['_pe']-incoming[i]['_de'],
                'press_event_duration':lambda:pout[i]['_pe']-pin[i]['_de'],
                'cold_pickup':lambda:A[i]-15,'finished_delivery':lambda:E[i]+15}[k]()
    values=[value(x.name) for x in m.proto.variables]
    def inside(x,domain):return any(lo<=x<=hi for lo,hi in zip(domain[::2],domain[1::2]))
    def integer(r):return values[r] if r>=0 else -values[-r-1]
    def lit(r):return values[r] if r>=0 else 1-values[-r-1]
    def expr(e):return e.offset+sum(c*integer(r) for r,c in zip(e.vars,e.coeffs))
    def active(c):return all(lit(x) for x in c.enforcement_literal)
    errors=[];kinds=Counter();cs=m.proto.constraints
    for i,x in enumerate(m.proto.variables):
        if not inside(values[i],list(x.domain)):errors.append(['domain',i,x.name,values[i]])
    for i,c in enumerate(cs):
        if not active(c):continue
        if c.has_linear():
            kinds['linear']+=1;ok=inside(sum(co*integer(r) for r,co in zip(c.linear.vars,c.linear.coeffs)),list(c.linear.domain))
        elif c.has_lin_max():
            kinds['lin_max']+=1;ok=expr(c.lin_max.target)==max(expr(e) for e in c.lin_max.exprs)
        elif c.has_interval():
            kinds['interval']+=1;ok=expr(c.interval.start)+expr(c.interval.size)==expr(c.interval.end) and expr(c.interval.size)>=0
        elif c.has_cumulative():
            kinds['cumulative']+=1;ev=[]
            for idx,demand in zip(c.cumulative.intervals,c.cumulative.demands):
                cc=cs[idx]
                if active(cc):
                    a,b=expr(cc.interval.start),expr(cc.interval.end)
                    if b>a:ev.extend([(a,expr(demand)),(b,-expr(demand))])
            cap=expr(c.cumulative.capacity);cur=0;ok=True
            for at,delta in sorted(ev):cur+=delta;ok=ok and cur<=cap
        elif c.has_no_overlap():
            kinds['no_overlap']+=1;iv=[]
            for idx in c.no_overlap.intervals:
                cc=cs[idx]
                if active(cc):
                    a,b=expr(cc.interval.start),expr(cc.interval.end)
                    if b>a:iv.append((a,b))
            iv.sort();ok=all(a2>=b for (a,b),(a2,b2) in zip(iv,iv[1:]))
        elif c.has_bool_or():kinds['bool_or']+=1;ok=any(lit(x) for x in c.bool_or.literals)
        elif c.has_bool_and():kinds['bool_and']+=1;ok=all(lit(x) for x in c.bool_and.literals)
        else:raise ValueError('Unhandled constraint '+str(i))
        if not ok:errors.append(['constraint',i,str(c)[:250]])
    return dict(valid=not errors,original_physical_audit_valid=checked['valid'],variables=len(values),
                constraints=len(cs),active_checked=dict(kinds),errors=errors,solver_called=False)


if __name__=='__main__':
    import sys
    p=Path(sys.argv[1]);sc=json.loads((p/'scenario.json').read_text());s=json.loads((p/'best_schedule.json').read_text())
    result=verify(sc,s);(p/'exact_model_verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
    if not result['valid']:raise SystemExit(1)
