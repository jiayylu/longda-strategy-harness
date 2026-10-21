"""Calendar-day scoring for cold and observed-state starts; physical rules unchanged."""
from longda_cpsat.objectives import evaluate_schedule as original_evaluate, add_objective as original_objective, objective_key
from longda_cpsat.model import flatten


def window(sc):
    day=1440*sc['ticks_per_minute']
    now=sc.get('production_state',{}).get('now_ticks',0)
    start=sc.get('score_window_start_ticks',(now//day)*day)
    if type(start) is not int or start<0 or start%day:
        raise ValueError('Score window must start at a fixed calendar-day boundary')
    if not start<=now<start+day:
        raise ValueError('Scoring must use the current observed day')
    return start,start+day


def evaluate_schedule(sc,s):
    metric=original_evaluate(sc,s);L,R=window(sc)
    metric['score_ticks']=sum(max(0,min(s['forge_start'][i]+q['duration'],R)-max(s['forge_start'][i],L)) for i,q in enumerate(flatten(sc)))
    metric.update(day_start_ticks=L,day_end_ticks=R)
    return metric


def add_objective(model,variables,ops,scenario,**kwargs):
    L,R=window(scenario)
    if L==0:return original_objective(model,variables,ops,scenario,**kwargs)
    terms=[]
    for q,s in zip(ops,variables['forge_start']):
        i=q['index'];clips=[]
        for label,t in [('start',s),('end',s+q['duration'])]:
            upper=model.new_int_var(0,R,f'window_{label}_upper_{i}');model.add_min_equality(upper,[R,t])
            clamped=model.new_int_var(L,R,f'window_{label}_clamp_{i}');model.add_max_equality(clamped,[L,upper]);clips.append(clamped)
        terms.append(clips[1]-clips[0])
    maximum=min(R-L,sum(q['duration'] for q in ops));score=model.new_int_var(0,maximum,'shared_day_score');model.add(score==sum(terms))
    excess_max=sum(q['upper']-q['lower'] for q in ops);excess=model.new_int_var(0,excess_max,'shared_hold_excess');model.add(excess==sum(h-q['lower'] for h,q in zip(variables['heat_duration'],ops)))
    weight=excess_max+1
    if weight*maximum+excess_max>=1<<62:raise ValueError('Objective exceeds integer safety range')
    return dict(score=score,hold_excess=excess,lex_weight=weight,day_start_ticks=L,day_end_ticks=R,score_upper_bound=maximum,hold_excess_upper_bound=excess_max)
